import json

import pytest
from click.testing import CliRunner

from xdr_cli.services import large_files as lf
from xdr_cli.services.large_files import ChunkState, LargeFilesService
from xdr_cli.xdr_cli import ByteSize, _remote_basename, cli

AUTH = ["--base-url", "tenant.example.com", "--api-key-id", "1", "--api-key-hash", "h", "--json"]


def invoke(*args):
    return CliRunner(mix_stderr=False).invoke(cli, [*AUTH, "get-large-file", *args])


@pytest.mark.parametrize(
    "text,value",
    [("100", 100), ("100M", 100 * 2 ** 20), ("2g", 2 * 2 ** 30), ("64KiB", 64 * 2 ** 10), (7, 7)],
)
def test_byte_size(text, value):
    assert ByteSize().convert(text, None, None) == value


def test_byte_size_rejects_garbage():
    result = invoke("/f", "--endpoint-id", "e", "-o", "x", "--chunk-size", "lots")
    assert result.exit_code == 2
    assert "is not a size" in result.stderr


def test_remote_basename():
    assert _remote_basename("C:\\Windows\\MEMORY.DMP") == "MEMORY.DMP"
    assert _remote_basename("/var/log/syslog") == "syslog"


def test_print_script_needs_nothing_else():
    result = invoke("--print-script")
    assert result.exit_code == 0
    assert "XDRLF-CONFIG-BEGIN" in result.stdout
    compile(result.stdout, "large_file.py", "exec")


@pytest.mark.parametrize(
    "args,message",
    [
        (["--endpoint-id", "e", "-o", "x"], "PATH is required"),
        (["/f", "-o", "x"], "--endpoint-id or --hostname"),
        (["/f", "--endpoint-id", "e"], "--via chunks needs -o/--output"),
        (["/f", "--endpoint-id", "e", "--via", "sftp"], "--via sftp needs"),
        (["/f", "--endpoint-id", "e", "--via", "sftp", "--sftp-host", "h", "--sftp-user", "u",
          "--sftp-password-file", "/dev/null", "--delete-remote"], "--delete-remote needs"),
    ],
)
def test_validation(args, message):
    result = invoke(*args)
    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "VALIDATION"
    assert message in error["message"]


@pytest.fixture
def calls(monkeypatch):
    seen = {}

    async def resolve(self, endpoint_id, hostname):
        seen["resolve"] = (endpoint_id, hostname)
        return "eid"

    async def fetch_chunked(self, endpoint_id, path, destination, **kwargs):
        seen["fetch_chunked"] = (endpoint_id, path, destination, kwargs)
        kwargs["progress"]("chunk 1: done")
        return ChunkState(endpoint_id, path, 10, 1, "abc", "job", 10, [{}, {}])

    async def upload_sftp(self, endpoint_id, path, target, remote_path, **kwargs):
        seen["upload_sftp"] = (endpoint_id, path, target, remote_path, kwargs)
        return {"size": 5, "sha256": "def"}

    def fetch_from_sftp(target, remote_path, destination, sha256, delete, progress):
        seen["fetch_from_sftp"] = (remote_path, destination, sha256, delete)

    monkeypatch.setattr(LargeFilesService, "resolve_endpoint", resolve)
    monkeypatch.setattr(LargeFilesService, "fetch_chunked", fetch_chunked)
    monkeypatch.setattr(LargeFilesService, "upload_sftp", upload_sftp)
    monkeypatch.setattr("xdr_cli.xdr_cli.fetch_from_sftp", fetch_from_sftp)
    return seen


def test_chunks(calls, tmp_path):
    out = tmp_path / "big.bin"
    result = invoke("--hostname", "h1", "/var/big.bin", "-o", str(out), "--chunk-size", "50M", "--no-hash")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {"output": str(out), "size": 10, "sha256": "abc", "chunks": 2}
    assert "chunk 1: done" in result.stderr
    assert calls["resolve"] == (None, "h1")
    endpoint_id, path, destination, kwargs = calls["fetch_chunked"]
    assert (endpoint_id, path, destination) == ("eid", "/var/big.bin", out)
    assert kwargs["chunk_bytes"] == 50 * 2 ** 20
    assert kwargs["verify_hash"] is False


def test_sftp(calls, tmp_path):
    password = tmp_path / "pw.txt"
    password.write_text("s3cret\n")
    out = tmp_path / "MEMORY.DMP"
    result = invoke(
        "--hostname", "h1", "C:\\Windows\\MEMORY.DMP", "--via", "sftp",
        "--sftp-host", "sftp.example.com", "--sftp-user", "u", "--sftp-password-file", str(password),
        "--endpoint-sftp-host", "10.0.0.1", "-o", str(out), "--delete-remote", "--time-budget", "60",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == {
        "remote_path": "incoming/h1_MEMORY.DMP", "size": 5, "sha256": "def", "output": str(out),
    }
    endpoint_id, path, target, remote_path, kwargs = calls["upload_sftp"]
    assert target.password == "s3cret"
    assert target.endpoint_host == "10.0.0.1"
    assert kwargs["time_budget"] == 60
    assert calls["fetch_from_sftp"] == ("incoming/h1_MEMORY.DMP", out, "def", True)


def test_sftp_without_output_skips_download(calls, tmp_path):
    password = tmp_path / "pw.txt"
    password.write_text("s3cret")
    result = invoke(
        "--endpoint-id", "e1", "/var/big", "--via", "sftp", "--sftp-host", "s", "--sftp-user", "u",
        "--sftp-password-file", str(password), "--remote-path", "incoming/x",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["remote_path"] == "incoming/x"
    assert "fetch_from_sftp" not in calls


def test_service_errors_are_reported(monkeypatch, tmp_path):
    async def boom(self, endpoint_id, hostname):
        raise lf.XdrValidationError("Expected one connected endpoint named ghost, found 0")

    monkeypatch.setattr(LargeFilesService, "resolve_endpoint", boom)
    result = invoke("--hostname", "ghost", "/f", "-o", str(tmp_path / "o"))
    assert result.exit_code != 0
    assert "found 0" in json.loads(result.stdout)["error"]["message"]
