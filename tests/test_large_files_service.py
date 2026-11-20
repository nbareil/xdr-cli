from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import types
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from xdr_cli.endpoint_scripts import large_file as endpoint_script
from xdr_cli.errors import XdrFileTooLargeError, XdrValidationError
from xdr_cli.services import large_files as lf
from xdr_cli.services.large_files import (
    LargeFilesService,
    SftpTarget,
    part_path,
    render_script,
    state_path,
)

MIB = 1024 * 1024


def run(coro):
    return asyncio.run(coro)


def exec_config(code: str) -> Dict[str, Any]:
    namespace: Dict[str, Any] = {}
    exec(code.replace('if __name__ == "__main__":', "if False:"), namespace)
    return namespace["CONFIG"]


# --- render_script ---------------------------------------------------------- #

def test_render_script_sets_config_and_runs():
    code = render_script(action="stat", path="C:\\x.bin", offset=7)
    config = exec_config(code)
    assert config["action"] == "stat"
    assert config["path"] == "C:\\x.bin"
    assert config["offset"] == 7
    assert config["chunk_bytes"] == endpoint_script.CONFIG["chunk_bytes"]
    assert code.count("CONFIG = ") == 1


def test_render_script_rejects_unknown_keys():
    with pytest.raises(XdrValidationError, match="bogus"):
        render_script(bogus=1)


# --- chunks, end to end against the real endpoint script ------------------- #

class LocalService(LargeFilesService):
    """Runs endpoint scripts in-process and serves file retrievals from disk."""

    def __init__(self, staging: Path, size_limit: Optional[int] = None):
        self.staging = staging
        self.size_limit = size_limit
        self.actions: List[str] = []
        self.fail_get_file_at: Optional[int] = None
        self.fail_cleanup = False

    async def run_script(self, endpoint_id, code, timeout):
        assert endpoint_id == "e1"
        config = exec_config(code)
        config["staging_dir"] = str(self.staging)
        output = endpoint_script.main(config)
        self.actions.append(output["action"])
        if output["action"] == "cleanup" and self.fail_cleanup:
            raise XdrValidationError("agent disconnected")
        if output["status"] != "ok":
            raise XdrValidationError(str(output["errors"]))
        return output["data"]

    async def get_file(self, endpoint_id, path, destination):
        self.actions.append("get-file")
        if self.fail_get_file_at is not None and self.actions.count("get-file") == self.fail_get_file_at:
            raise XdrValidationError("network down")
        if self.size_limit and Path(path).stat().st_size > self.size_limit:
            raise XdrFileTooLargeError("File retrieval ended in state FAILED: Maximum file size reached")
        shutil.copy(path, str(destination))


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "evidence.bin"
    with path.open("wb") as fh:
        fh.write(os.urandom(3 * MIB))
        fh.write(b"\0" * (8 * MIB))
        fh.write(os.urandom(MIB // 2))
    return path


@pytest.fixture
def out(tmp_path):
    (tmp_path / "out").mkdir()
    return tmp_path / "out" / "evidence.bin"


def fetch(service, source, out, chunk_bytes=MIB, **kwargs):
    messages: List[str] = []
    state = run(service.fetch_chunked("e1", str(source), out, chunk_bytes=chunk_bytes,
                                      max_raw_bytes=4 * MIB, progress=messages.append, **kwargs))
    return state, messages


def test_fetch_chunked(source, out, tmp_path):
    service = LocalService(tmp_path / "staging")
    state, messages = fetch(service, source, out)
    assert out.read_bytes() == source.read_bytes()
    assert state.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(state.chunks) >= 3
    assert max(c["raw_length"] for c in state.chunks) == 4 * MIB  # zeros, capped by max_raw
    assert service.actions[0] == "stat"
    assert service.actions[-1] == "cleanup"
    assert not (tmp_path / "staging").exists()
    assert sorted(p.name for p in out.parent.iterdir()) == ["evidence.bin"]
    assert any(m.startswith("chunk 1:") for m in messages)


def test_fetch_chunked_resumes_after_failure(source, out, tmp_path):
    service = LocalService(tmp_path / "staging")
    service.fail_get_file_at = 3
    with pytest.raises(XdrValidationError, match="network down"):
        fetch(service, source, out)
    assert service.actions[-1] == "cleanup"
    saved = json.loads(state_path(out).read_text())
    assert len(saved["chunks"]) == 2
    assert part_path(out).stat().st_size == saved["next_offset"]

    service.fail_get_file_at = None
    service.actions.clear()
    state, messages = fetch(service, source, out)
    assert "stat" not in service.actions
    assert state.job_id == saved["job_id"]
    assert messages[0].startswith("resuming")
    assert out.read_bytes() == source.read_bytes()
    assert not state_path(out).exists()


def test_fetch_chunked_ignores_state_of_another_file(source, out, tmp_path):
    state_path(out).write_text(json.dumps({"endpoint_id": "e1", "path": "/other"}))
    service = LocalService(tmp_path / "staging")
    fetch(service, source, out, verify_hash=False)
    assert service.actions[0] == "stat"
    assert out.read_bytes() == source.read_bytes()


def test_load_state_checks_partial_file(out):
    saved = lf.ChunkState("e1", "/f", 10, 1, None, "job", next_offset=5)
    lf.save_state(out, saved)
    assert lf.load_state(out, "e1", "/f") is None
    part_path(out).write_bytes(b"1234")
    assert lf.load_state(out, "e1", "/f") is None
    part_path(out).write_bytes(b"12345")
    assert lf.load_state(out, "e1", "/f") == saved
    state_path(out).write_text("not json")
    assert lf.load_state(out, "e1", "/f") is None


def test_fetch_chunked_halves_chunks_over_the_limit(source, out, tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "MIN_CHUNK_BYTES", 64 * 1024)
    limit = MIB + 256 * 1024
    service = LocalService(tmp_path / "staging", size_limit=limit)
    state, messages = fetch(service, source, out, chunk_bytes=8 * MIB)
    assert out.read_bytes() == source.read_bytes()
    assert max(c["chunk_size"] for c in state.chunks) <= limit
    assert any("refused by the tenant size limit" in m for m in messages)


def test_fetch_chunked_gives_up_below_minimum(source, out, tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "MIN_CHUNK_BYTES", MIB)
    service = LocalService(tmp_path / "staging", size_limit=MIB // 4)
    with pytest.raises(XdrFileTooLargeError):
        fetch(service, source, out, chunk_bytes=2 * MIB)


def test_fetch_chunked_detects_corrupt_chunk(source, out, tmp_path):
    service = LocalService(tmp_path / "staging")
    real_get_file = service.get_file

    async def corrupt(endpoint_id, path, destination):
        await real_get_file(endpoint_id, path, destination)
        destination.write_bytes(gzip.compress(b"tampered"))

    service.get_file = corrupt
    with pytest.raises(XdrValidationError, match="Chunk at offset 0 is corrupt"):
        fetch(service, source, out)


def test_fetch_chunked_detects_hash_mismatch(source, out, tmp_path):
    service = LocalService(tmp_path / "staging")
    real_run = service.run_script

    async def wrong_hash(endpoint_id, code, timeout):
        data = await real_run(endpoint_id, code, timeout)
        if "sha256" in data and "chunk_path" not in data:
            data["sha256"] = "0" * 64
        return data

    service.run_script = wrong_hash
    with pytest.raises(XdrValidationError, match="SHA-256 mismatch"):
        fetch(service, source, out)
    assert part_path(out).exists()
    assert not out.exists()


def test_fetch_chunked_survives_cleanup_failure(source, out, tmp_path):
    service = LocalService(tmp_path / "staging")
    service.fail_cleanup = True
    _, messages = fetch(service, source, out)
    assert out.read_bytes() == source.read_bytes()
    assert any("could not clean" in m for m in messages)


# --- service plumbing against fake client services ------------------------- #

class FakeScripts:
    def __init__(self, reply):
        self.reply = reply
        self.calls: List[Dict[str, Any]] = []

    async def run_snippet_and_wait(self, **kwargs):
        self.calls.append(kwargs)
        return self.reply


class FakeEndpoints:
    def __init__(self, endpoints):
        self.endpoints = endpoints

    async def get_by_hostname(self, hostname):
        return self.endpoints


class FakeFiles:
    def __init__(self, archive_bytes):
        self.archive_bytes = archive_bytes

    async def retrieve_to_file(self, endpoint_id, paths, destination, poll_interval_seconds):
        Path(destination).write_bytes(self.archive_bytes)
        return len(self.archive_bytes)


def service_with(**services):
    client = types.SimpleNamespace(transport=None, **services)
    service = LargeFilesService(client)
    service.poll_interval_seconds = 0
    return service


def script_reply(stdout, error_message=""):
    return {
        "action_id": 42,
        "results": {"error_message": error_message,
                    "results": [{"execution_status": "COMPLETED_SUCCESSFULLY", "standard_output": stdout}]},
    }


def test_run_script_returns_data():
    scripts = FakeScripts(script_reply(json.dumps({"status": "ok", "data": {"size": 3}})))
    service = service_with(scripts=scripts)
    assert run(service.run_script("e1", "print(1)", 60)) == {"size": 3}
    assert scripts.calls[0]["endpoint_ids"] == ["e1"]
    assert scripts.calls[0]["timeout"] == 60


def test_run_script_reports_script_errors():
    scripts = FakeScripts(script_reply(json.dumps({"status": "error", "errors": ["OSError: boom"]})))
    with pytest.raises(XdrValidationError, match="Script action 42 failed: OSError: boom"):
        run(service_with(scripts=scripts).run_script("e1", "", 60))


def test_run_script_reports_unparsable_output():
    scripts = FakeScripts(script_reply("Traceback ...", "killed"))
    with pytest.raises(XdrValidationError, match="output 'Traceback ...' killed"):
        run(service_with(scripts=scripts).run_script("e1", "", 60))


def test_resolve_endpoint():
    endpoints = FakeEndpoints([{"endpoint_id": "lost", "endpoint_status": "LOST"},
                               {"endpoint_id": "e1", "endpoint_status": "CONNECTED"}])
    service = service_with(endpoints=endpoints)
    assert run(service.resolve_endpoint("given", None)) == "given"
    assert run(service.resolve_endpoint(None, "h")) == "e1"
    endpoints.endpoints = endpoints.endpoints[:1]
    with pytest.raises(XdrValidationError, match="found 0"):
        run(service.resolve_endpoint(None, "h"))
    with pytest.raises(XdrValidationError, match="required"):
        run(service.resolve_endpoint(None, None))


def make_zip(members: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_get_file_handles_nested_windows_archive(tmp_path):
    manifest = {"entries": [{"files": [{"path": "C:\\c.gz", "archivedPath": "entry_1_0\\c.gz"}]}]}
    inner = make_zip({"manifest.json": json.dumps(manifest).encode(), "stdout.txt": b"noise",
                      "entry_1_0/c.gz": b"payload"})
    service = service_with(files=FakeFiles(make_zip({"script_output.zip": inner})))
    destination = tmp_path / "chunk.gz"
    run(service.get_file("e1", "C:\\c.gz", destination))
    assert destination.read_bytes() == b"payload"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chunk.gz"]


# --- sftp -------------------------------------------------------------------- #

KEY_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519fakekey").decode()
HOST_KEY = "ssh-ed25519 " + KEY_BLOB


class FakeKey:
    def get_name(self):
        return "ssh-ed25519"

    def get_base64(self):
        return KEY_BLOB


class FakeTransport:
    instances: List["FakeTransport"] = []

    def __init__(self, address):
        self.address = address
        self.auth = None
        self.closed = False
        FakeTransport.instances.append(self)

    def start_client(self):
        pass

    def get_remote_server_key(self):
        return FakeKey()

    def auth_password(self, username, password):
        self.auth = (username, password)

    def close(self):
        self.closed = True


class FakeRemote(io.BytesIO):
    def prefetch(self):
        pass


class FakeSftp:
    files: Dict[str, bytes] = {}

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(st_size=len(self.files[path]))

    def open(self, path, mode):
        return FakeRemote(self.files[path])

    def remove(self, path):
        del self.files[path]


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch):
    FakeTransport.instances = []
    FakeSftp.files = {}
    module = types.ModuleType("paramiko")
    module.Transport = FakeTransport
    module.SFTPClient = types.SimpleNamespace(from_transport=lambda transport: FakeSftp())
    monkeypatch.setitem(sys.modules, "paramiko", module)


def target(**overrides):
    values = dict(host="sftp.example.com", port=22, username="u", password="p")
    values.update(overrides)
    return SftpTarget(**values)


def test_fingerprint():
    digest = base64.b64encode(hashlib.sha256(base64.b64decode(KEY_BLOB)).digest()).decode()
    assert lf.fingerprint(HOST_KEY) == "SHA256:" + digest.rstrip("=")


def test_sftp_session_checks_pinned_key():
    with pytest.raises(XdrValidationError, match="Host key mismatch"):
        lf.SftpSession(target(host_key="ssh-ed25519 AAAAother"))
    assert FakeTransport.instances[0].closed
    assert FakeTransport.instances[0].auth is None
    session = lf.SftpSession(target(host_key=HOST_KEY + " comment"))
    assert session.host_key == HOST_KEY
    assert FakeTransport.instances[1].auth == ("u", "p")


def test_sftp_session_without_paramiko(monkeypatch):
    monkeypatch.setitem(sys.modules, "paramiko", None)
    with pytest.raises(XdrValidationError, match=r"xdr-cli\[sftp\]"):
        lf.SftpSession(target())


class UploadService(LargeFilesService):
    """Pretend endpoint: stat, then sftp actions advancing by ``step`` bytes."""

    def __init__(self, size, step):
        self.size, self.step = size, step
        self.configs: List[Dict[str, Any]] = []

    async def run_script(self, endpoint_id, code, timeout):
        config = exec_config(code)
        self.configs.append(config)
        assert timeout == 600
        if config["action"] == "stat":
            return {"size": self.size, "mtime": 1, "sha256": "abc"}
        assert config["sftp_password"] == "p"
        assert config["sftp_host_key"] == HOST_KEY
        next_offset = min(config["offset"] + self.step, self.size)
        return {"next_offset": next_offset, "eof": next_offset == self.size}

    def offsets(self):
        return [c["offset"] for c in self.configs if c["action"] == "sftp"]


def upload(service, tgt, **kwargs):
    messages: List[str] = []
    info = run(service.upload_sftp("e1", "/f", tgt, "incoming/f", progress=messages.append, **kwargs))
    return info, messages


def test_upload_sftp_chains_actions():
    service = UploadService(size=25, step=10)
    info, messages = upload(service, target(endpoint_host="10.0.0.1"))
    assert info["sha256"] == "abc"
    assert service.offsets() == [0, 10, 20]
    sftp_configs = [c for c in service.configs if c["action"] == "sftp"]
    assert {c["sftp_host"] for c in sftp_configs} == {"10.0.0.1"}
    assert {c["expect_size"] for c in sftp_configs} == {25}
    assert any("on first use" in m for m in messages)


def test_upload_sftp_resumes_from_part():
    FakeSftp.files["incoming/f.part"] = b"x" * 20
    service = UploadService(size=25, step=10)
    upload(service, target(host_key=HOST_KEY), resume=True)
    assert service.offsets() == [20]


def test_upload_sftp_restarts_when_part_is_too_big():
    FakeSftp.files["incoming/f.part"] = b"x" * 99
    service = UploadService(size=25, step=30)
    upload(service, target(), resume=True)
    assert service.offsets() == [0]


def test_upload_sftp_stops_without_progress():
    with pytest.raises(XdrValidationError, match="No progress at offset 0"):
        upload(UploadService(size=25, step=0), target())


def test_fetch_from_sftp_verifies_and_deletes(tmp_path):
    FakeSftp.files["incoming/f"] = b"data"
    out = tmp_path / "f"
    lf.fetch_from_sftp(target(), "incoming/f", out, hashlib.sha256(b"data").hexdigest(), True, lambda m: None)
    assert out.read_bytes() == b"data"
    assert FakeSftp.files == {}


def test_fetch_from_sftp_detects_mismatch(tmp_path):
    FakeSftp.files["incoming/f"] = b"data"
    with pytest.raises(XdrValidationError, match="SHA-256 mismatch"):
        lf.fetch_from_sftp(target(), "incoming/f", tmp_path / "f", "0" * 64, True, lambda m: None)
    assert "incoming/f" in FakeSftp.files
    assert FakeTransport.instances[-1].closed
