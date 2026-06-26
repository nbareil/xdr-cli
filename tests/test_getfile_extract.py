import io
import json
import zipfile

import click
import pytest

from xdr_cli.xdr_cli import (
    _extract_retrieved_file_zip,
    _flatten_endpoint_path,
    _safe_extract_zip,
)


PASSWORD = b"suspicious"


# The real archives from the endpoint are encrypted with this password, but
# stdlib zipfile cannot write encrypted entries. We build plaintext zips here:
# _safe_extract_zip passes pwd= which is simply ignored for unencrypted members,
# so the path-handling and manifest logic under test is exercised unchanged.
def _make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --- _flatten_endpoint_path (pure) ----------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("C:\\Temp\\Evil.EXE", "c_temp_evil.exe"),
        ("/root/.bash_history", "root_.bash_history"),
        ("/a//b///c", "a_b_c"),
        ("", "file"),
        ("///", "file"),
    ],
)
def test_flatten_endpoint_path(raw, expected):
    assert _flatten_endpoint_path(raw) == expected


# --- _safe_extract_zip (zip-slip guard) ------------------------------------ #

def test_safe_extract_writes_members(tmp_path):
    zip_path = tmp_path / "in.zip"
    zip_path.write_bytes(_make_zip({"a.txt": b"hello", "sub/b.txt": b"world"}))
    out = tmp_path / "out"
    out.mkdir()
    _safe_extract_zip(zip_path, out, PASSWORD)
    assert (out / "a.txt").read_bytes() == b"hello"
    assert (out / "sub" / "b.txt").read_bytes() == b"world"


def test_safe_extract_rejects_path_traversal(tmp_path):
    zip_path = tmp_path / "evil.zip"
    # Craft an entry that escapes the output directory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    zip_path.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(click.ClickException):
        _safe_extract_zip(zip_path, out, PASSWORD)
    assert not (tmp_path / "escape.txt").exists()


# --- full retrieved-file pipeline ------------------------------------------ #

def test_extract_retrieved_file_with_manifest(tmp_path):
    manifest = {
        "entries": [
            {
                "files": [
                    {"archivedPath": "entry_1/0", "path": "C:\\Users\\victim\\evil.exe"},
                ]
            }
        ]
    }
    inner = _make_zip(
        {
            "manifest.json": json.dumps(manifest),
            "entry_1/0": b"MZ payload",
        }
    )
    outer = _make_zip({"script_output.zip": inner})

    out = tmp_path / "collected"
    _extract_retrieved_file_zip(outer, out)

    # Payload renamed from manifest, scaffolding zips removed.
    assert (out / "c_users_victim_evil.exe").read_bytes() == b"MZ payload"
    assert not (out / "script_output.zip").exists()
    assert not (out / "retrieved_file.zip").exists()
    # entry_* scaffolding directory cleaned up.
    assert not (out / "entry_1").exists()


def test_extract_retrieved_file_without_inner_zip(tmp_path):
    outer = _make_zip({"plain.txt": b"data"})
    out = tmp_path / "collected"
    _extract_retrieved_file_zip(outer, out)
    assert (out / "plain.txt").read_bytes() == b"data"
    assert not (out / "retrieved_file.zip").exists()
