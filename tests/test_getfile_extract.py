import io
import json
import logging
import shutil
import stat
import subprocess
import zipfile

import click
import pytest

from xdr_cli import xdr_cli as cli_module
from xdr_cli.xdr_cli import (
    _external_unzip_command,
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


SEVEN_ZIP = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
NATIVE = SEVEN_ZIP or shutil.which("unzip")


@pytest.fixture(params=["native", "python"])
def extractor(request, monkeypatch):
    """Run a test once with the native extractor and once with zipfile."""
    if request.param == "native":
        if not NATIVE:
            pytest.skip("neither 7-Zip nor unzip is installed")
    else:
        monkeypatch.setattr(cli_module.shutil, "which", lambda name: None)
    return request.param


def _write_outer(tmp_path, data: bytes):
    path = tmp_path / "retrieved_file.zip"
    path.write_bytes(data)
    return path


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

def test_safe_extract_writes_members(tmp_path, extractor):
    zip_path = tmp_path / "in.zip"
    zip_path.write_bytes(_make_zip({"a.txt": b"hello", "sub/b.txt": b"world"}))
    out = tmp_path / "out"
    out.mkdir()
    _safe_extract_zip(zip_path, out, PASSWORD)
    assert (out / "a.txt").read_bytes() == b"hello"
    assert (out / "sub" / "b.txt").read_bytes() == b"world"


def test_safe_extract_rejects_path_traversal(tmp_path, extractor, monkeypatch):
    zip_path = tmp_path / "evil.zip"
    # Craft an entry that escapes the output directory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", b"pwned")
    zip_path.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    ran = []
    monkeypatch.setattr(cli_module.subprocess, "run", lambda *a, **kw: ran.append(a))
    with pytest.raises(click.ClickException, match="Unsafe zip entry path"):
        _safe_extract_zip(zip_path, out, PASSWORD)
    assert not (tmp_path / "escape.txt").exists()
    assert ran == []  # refused before any extractor runs


def test_safe_extract_rejects_symlinks(tmp_path, extractor):
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, "/etc")
        zf.writestr("link/passwd", b"pwned")
    zip_path = tmp_path / "evil.zip"
    zip_path.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(click.ClickException, match="symlink"):
        _safe_extract_zip(zip_path, out, PASSWORD)
    assert list(out.iterdir()) == []


@pytest.mark.skipif(not SEVEN_ZIP, reason="7-Zip is needed to build a ZipCrypto archive")
def test_safe_extract_decrypts_zipcrypto(tmp_path, extractor):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"MZ" + bytes(range(256)) * 512)
    zip_path = tmp_path / "enc.zip"
    subprocess.run(
        [SEVEN_ZIP, "a", "-tzip", "-mem=ZipCrypto", "-psuspicious", str(zip_path), str(payload)],
        check=True, capture_output=True,
    )
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.infolist()[0].flag_bits & 0x1  # really encrypted
    out = tmp_path / "out"
    out.mkdir()
    _safe_extract_zip(zip_path, out, PASSWORD)
    assert (out / "payload.bin").read_bytes() == payload.read_bytes()


def test_native_failure_falls_back_to_python(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "/usr/bin/unzip" if name == "unzip" else None)
    monkeypatch.setattr(
        cli_module.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 2, stdout="", stderr="unsupported method"),
    )
    zip_path = tmp_path / "in.zip"
    zip_path.write_bytes(_make_zip({"a.txt": b"hello"}))
    out = tmp_path / "out"
    out.mkdir()
    with caplog.at_level(logging.WARNING):
        _safe_extract_zip(zip_path, out, PASSWORD)
    assert (out / "a.txt").read_bytes() == b"hello"
    assert "unsupported method" in caplog.text


@pytest.mark.parametrize(
    "installed,expected",
    [
        ({"7zz", "7z", "unzip"}, "7zz"),
        ({"7z", "7za", "unzip"}, "7z"),
        ({"7za", "unzip"}, "7za"),
        ({"unzip"}, "unzip"),
        (set(), None),
    ],
)
def test_external_unzip_command_preference(tmp_path, monkeypatch, installed, expected):
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "/bin/" + name if name in installed else None)
    cmd = _external_unzip_command(tmp_path / "a.zip", tmp_path / "out", PASSWORD)
    if expected is None:
        assert cmd is None
    else:
        assert cmd[0] == "/bin/" + expected
        assert any("suspicious" in arg for arg in cmd)


# --- full retrieved-file pipeline ------------------------------------------ #

def test_extract_retrieved_file_with_manifest(tmp_path, extractor):
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
    _extract_retrieved_file_zip(_write_outer(tmp_path, outer), out)

    # Payload renamed from manifest, scaffolding zips removed.
    assert (out / "c_users_victim_evil.exe").read_bytes() == b"MZ payload"
    assert not (out / "script_output.zip").exists()
    assert not (tmp_path / "retrieved_file.zip").exists()
    # entry_* scaffolding directory cleaned up.
    assert not (out / "entry_1").exists()


def test_extract_retrieved_file_without_inner_zip(tmp_path, extractor):
    out = tmp_path / "collected"
    out.mkdir()
    # get-file downloads the archive into the output directory itself.
    outer = out / "retrieved_file.zip"
    outer.write_bytes(_make_zip({"plain.txt": b"data"}))
    _extract_retrieved_file_zip(outer, out)
    assert (out / "plain.txt").read_bytes() == b"data"
    assert not outer.exists()
