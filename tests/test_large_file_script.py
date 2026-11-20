from __future__ import annotations

import gzip
import hashlib
import io
import itertools
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from xdr_cli.endpoint_scripts import large_file as es


def config(**overrides: Any) -> dict[str, Any]:
    return dict(es.CONFIG, **overrides)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "src.bin"
    path.write_bytes(os.urandom(3 * es.BLOCK + 123))
    return path


def test_stat(source: Path) -> None:
    data = es.main(config(action="stat", path=str(source)))["data"]
    assert data["size"] == source.stat().st_size
    assert data["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_stat_without_hash(source: Path) -> None:
    assert "sha256" not in es.main(config(action="stat", path=str(source), hash=False))["data"]


def test_unknown_action() -> None:
    result = es.main(config(action="nope"))
    assert result["status"] == "error"
    assert "unknown action" in result["errors"][0]


def test_missing_file(tmp_path: Path) -> None:
    result = es.main(config(action="stat", path=str(tmp_path / "missing")))
    assert result["status"] == "error"
    assert result["errors"][0].startswith("FileNotFoundError")


def test_staging_dir_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert es.staging_dir(config(job_id="j1")) == str(tmp_path / "xdrlf-j1")
    monkeypatch.delenv("TEMP")
    monkeypatch.delenv("TMP", raising=False)
    assert es.staging_dir(config(job_id="j1")) == "/tmp/xdrlf-j1"  # noqa: S108
    assert es.staging_dir(config(staging_dir="/x")) == "/x"


def stage_all(source: Path, staging: Path, **overrides: Any) -> bytes:
    offset, out = 0, b""
    while True:
        result = es.main(
            config(
                action="stage",
                path=str(source),
                staging_dir=str(staging),
                offset=offset,
                **overrides,
            )
        )
        assert result["status"] == "ok", result
        data = result["data"]
        assert sorted(os.listdir(staging)) == [Path(data["chunk_path"]).name]
        raw = gzip.decompress(Path(data["chunk_path"]).read_bytes())
        assert len(raw) == data["raw_length"]
        assert hashlib.sha256(raw).hexdigest() == data["sha256"]
        out += raw
        offset += data["raw_length"]
        if data["eof"]:
            return out


def test_stage_by_compressed_size(source: Path, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    assert stage_all(source, staging, chunk_bytes=es.BLOCK) == source.read_bytes()


def test_stage_by_raw_size(tmp_path: Path) -> None:
    source = tmp_path / "zeros.bin"
    source.write_bytes(b"\0" * (5 * es.BLOCK))
    staging = tmp_path / "staging"
    assert stage_all(source, staging, max_raw_bytes=2 * es.BLOCK) == source.read_bytes()


def test_stage_empty_file(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.write_bytes(b"")
    assert stage_all(source, tmp_path / "staging") == b""


def test_stage_rejects_changed_file(source: Path, tmp_path: Path) -> None:
    common = {"action": "stage", "path": str(source), "staging_dir": str(tmp_path / "s")}
    result = es.main(config(**common, expect_size=1))
    assert "size changed" in result["errors"][0]
    result = es.main(config(**common, expect_size=source.stat().st_size, expect_mtime=1))
    assert "mtime changed" in result["errors"][0]


def test_stage_rejects_offset_past_end(source: Path, tmp_path: Path) -> None:
    result = es.main(
        config(action="stage", path=str(source), staging_dir=str(tmp_path / "s"), offset=10**9)
    )
    assert "beyond end of file" in result["errors"][0]


def test_stage_needs_free_space(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(es.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=10))
    result = es.main(config(action="stage", path=str(source), staging_dir=str(tmp_path / "s")))
    assert "bytes free" in result["errors"][0]


def test_stage_removes_partial_chunk(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(es.gzip, "GzipFile", boom)
    staging = tmp_path / "s"
    result = es.main(config(action="stage", path=str(source), staging_dir=str(staging)))
    assert result["errors"] == ["OSError: disk full"]
    assert os.listdir(staging) == []


def test_stage_partial_chunk_already_gone(
    source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        for name in os.listdir(tmp_path / "s"):
            os.remove(tmp_path / "s" / name)
        raise OSError("gone")

    monkeypatch.setattr(es.gzip, "GzipFile", boom)
    result = es.main(config(action="stage", path=str(source), staging_dir=str(tmp_path / "s")))
    assert result["errors"] == ["OSError: gone"]


def test_cleanup(source: Path, tmp_path: Path) -> None:
    staging = tmp_path / "s"
    es.main(config(action="stage", path=str(source), staging_dir=str(staging)))
    (staging / "other").write_text("not ours")
    data = es.main(config(action="cleanup", staging_dir=str(staging)))
    assert data["status"] == "error"
    (staging / "other").unlink()
    data = es.main(config(action="cleanup", staging_dir=str(staging)))["data"]
    assert data["removed"] == []
    assert not staging.exists()
    assert es.main(config(action="cleanup", staging_dir=str(staging)))["status"] == "ok"


class FakeRemoteFile(io.BytesIO):
    def __init__(self, store: dict[str, bytes], name: str, mode: str) -> None:
        super().__init__(b"" if mode == "wb" else store[name])
        self.store, self.name = store, name

    def set_pipelined(self, _: bool) -> None:  # noqa: FBT001
        pass

    def close(self) -> None:
        self.store[self.name] = self.getvalue()
        super().close()


class FakeSftpClient:
    def __init__(self, store: dict[str, bytes], *, posix_rename: bool = True) -> None:
        self.store = store
        self.has_posix_rename = posix_rename

    def stat(self, name: str) -> types.SimpleNamespace:
        if name not in self.store:
            raise FileNotFoundError(name)
        return types.SimpleNamespace(st_size=len(self.store[name]))

    def open(self, name: str, mode: str) -> FakeRemoteFile:
        return FakeRemoteFile(self.store, name, mode)

    def posix_rename(self, old: str, new: str) -> None:
        if not self.has_posix_rename:
            raise OSError("unsupported")
        self.store[new] = self.store.pop(old)

    def remove(self, name: str) -> None:
        if name not in self.store:
            raise FileNotFoundError(name)
        del self.store[name]

    def rename(self, old: str, new: str) -> None:
        self.store[new] = self.store.pop(old)


class FakePysftp(types.ModuleType):
    def __init__(self, client: FakeSftpClient) -> None:
        super().__init__("pysftp")
        self.client = client
        self.connections: list[dict[str, Any]] = []
        self.known_hosts: list[str] = []

    def CnOpts(self, knownhosts: str) -> types.SimpleNamespace:  # noqa: N802
        self.known_hosts.append(Path(knownhosts).read_text())
        return types.SimpleNamespace(compression=False)

    def Connection(self, host: str, **kwargs: Any) -> types.SimpleNamespace:  # noqa: N802
        self.connections.append({"host": host, **kwargs})
        return types.SimpleNamespace(sftp_client=self.client, close=lambda: None)


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch) -> FakePysftp:
    fake = FakePysftp(FakeSftpClient({}))
    monkeypatch.setitem(sys.modules, "pysftp", fake)
    monkeypatch.setattr(es.socket, "create_connection", lambda *_, **__: io.BytesIO())
    return fake


def sftp_config(source: Path, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    return config(
        action="sftp",
        path=str(source),
        staging_dir=str(tmp_path / "s"),
        sftp_host="10.0.0.1",
        sftp_host_key="ssh-ed25519 AAAAkey",
        remote_path="incoming/f.bin",
        **overrides,
    )


def test_sftp_in_one_action(source: Path, tmp_path: Path, remote: FakePysftp) -> None:
    result = es.main(sftp_config(source, tmp_path, sftp_compression=True))
    assert result["status"] == "ok", result
    assert result["data"]["eof"]
    assert remote.client.store == {"incoming/f.bin": source.read_bytes()}
    assert remote.known_hosts == ["10.0.0.1 ssh-ed25519 AAAAkey\n"]
    assert remote.connections[0]["cnopts"].compression is True
    assert not (tmp_path / "s").exists()


def test_sftp_resumes_across_actions(
    source: Path, tmp_path: Path, remote: FakePysftp, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One block per action: the clock is past the deadline after each block.
    clock = itertools.cycle([0, 5])
    monkeypatch.setattr(es.time, "time", lambda: next(clock))
    remote.client.has_posix_rename = False
    remote.client.store["incoming/f.bin"] = b"old"
    offset, actions = 0, 0
    while True:
        data = es.main(sftp_config(source, tmp_path, offset=offset, time_budget=1))["data"]
        actions += 1
        offset = data["next_offset"]
        if data["eof"]:
            break
        assert data["remote_path"] == "incoming/f.bin.part"
    assert actions == 4
    assert remote.client.store == {"incoming/f.bin": source.read_bytes()}


def test_sftp_rename_fallback_without_existing_file(
    source: Path, tmp_path: Path, remote: FakePysftp
) -> None:
    remote.client.has_posix_rename = False
    assert es.main(sftp_config(source, tmp_path))["status"] == "ok"
    assert remote.client.store == {"incoming/f.bin": source.read_bytes()}


def test_sftp_resume_truncates_longer_part(
    source: Path, tmp_path: Path, remote: FakePysftp
) -> None:
    remote.client.store["incoming/f.bin.part"] = source.read_bytes()[:10] + b"garbage" * 1000
    assert es.main(sftp_config(source, tmp_path, offset=10))["status"] == "ok"
    assert remote.client.store == {"incoming/f.bin": source.read_bytes()}


def test_sftp_resume_needs_enough_data(source: Path, tmp_path: Path, remote: FakePysftp) -> None:
    remote.client.store["incoming/f.bin.part"] = b"short"
    result = es.main(sftp_config(source, tmp_path, offset=10))
    assert "cannot resume" in result["errors"][0]


def test_sftp_detects_short_write(
    source: Path, tmp_path: Path, remote: FakePysftp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeRemoteFile, "write", lambda self, _: 0)
    result = es.main(sftp_config(source, tmp_path))
    assert "expected" in result["errors"][0]


def test_sftp_checks_file(source: Path, tmp_path: Path, remote: FakePysftp) -> None:
    assert "size changed" in es.main(sftp_config(source, tmp_path, expect_size=1))["errors"][0]
    result = es.main(sftp_config(source, tmp_path, offset=10**9))
    assert "beyond end of file" in result["errors"][0]
    assert remote.connections == []


def test_sftp_keeps_existing_staging_dir(source: Path, tmp_path: Path, remote: FakePysftp) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "keep").write_text("x")
    assert es.main(sftp_config(source, tmp_path))["status"] == "ok"
    assert (tmp_path / "s" / "keep").exists()
    assert remote.client.store


def test_sftp_unreachable(
    source: Path, tmp_path: Path, remote: FakePysftp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(address: tuple[str, int], timeout: float) -> None:
        assert timeout == es.CONNECT_TIMEOUT
        raise TimeoutError("timed out")

    monkeypatch.setattr(es.socket, "create_connection", refuse)
    result = es.main(sftp_config(source, tmp_path))
    assert result["errors"] == [
        "ScriptError: cannot reach 10.0.0.1:22 from the endpoint: timed out"
    ]
    assert remote.connections == []
    assert not (tmp_path / "s").exists()


def test_sftp_auth_failure_leaves_nothing(
    source: Path, tmp_path: Path, remote: FakePysftp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_: Any, **__: Any) -> None:
        raise OSError("Authentication failed.")

    monkeypatch.setattr(remote, "Connection", deny)
    result = es.main(sftp_config(source, tmp_path))
    assert result["errors"] == ["OSError: Authentication failed."]
    assert not (tmp_path / "s").exists()


# Modules XDR lets endpoint snippets import; it rejects any other import
# ("Contains import of unsupported module ...") before running the script.
XDR_ALLOWED_MODULES = {
    "argparse", "asyncio", "base64", "browserhistory", "certifi", "collections", "contextlib",
    "csv", "ctypes", "datetime", "enum", "fnmatch", "functools", "glob", "globmatch", "gzip",
    "hashlib", "icmplib", "importlib", "io", "json", "LnkParse3", "locale", "logging",
    "multiprocessing", "netifaces", "os", "pathlib", "pefile", "platform", "pprint", "protobuf",
    "psutil", "pysftp", "python_hosts", "pythoncom", "pytsk3", "pywintypes", "queue", "random",
    "re", "Registry", "requests", "runpy", "setuptools", "shlex", "shutil", "signal", "socket",
    "sqlite_utils", "sqlite3", "ssl", "stat", "struct", "subprocess", "sys", "threading", "time",
    "traceback", "types", "unicodecsv", "uuid", "websocket", "win32api", "win32com", "win32con",
    "win32evtlog", "win32evtlogutil", "win32file", "win32net", "win32netcon", "win32process",
    "win32security", "win32service", "win32serviceutil", "win32timezone", "winapps", "winerror",
    "winpty", "winreg", "wmi", "xml", "xmljson", "yara", "zipfile", "zlib",
}


def test_endpoint_script_imports_only_allowed_modules():
    import ast

    tree = ast.parse(Path(es.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "pysftp" in imported  # the lazy import inside sftp_connect is seen too
    assert imported <= XDR_ALLOWED_MODULES, imported - XDR_ALLOWED_MODULES
