import json

from click.testing import CliRunner

from xdr_cli import xdr_cli
from xdr_cli.errors import XdrNotFoundError
from xdr_cli.xdr_cli import cli

AUTH = ["--base-url", "x", "--api-key-id", "1", "--api-key-hash", "h"]


def test_dry_run_isolate_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not hit the network on --dry-run")

    monkeypatch.setattr(xdr_cli, "_run_with_xdr_client", boom)
    result = CliRunner().invoke(cli, AUTH + ["endpoint", "isolate", "--hostname", "H", "--dry-run"])
    assert result.exit_code == 0
    assert '"dry_run": true' in result.output


def test_json_envelope_success(monkeypatch):
    monkeypatch.setattr(xdr_cli, "_run_with_xdr_client", lambda ctx, op: [{"endpoint_name": "h1"}])
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["endpoint", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"] == [{"endpoint_name": "h1"}]


def test_json_envelope_not_found_exit_code(monkeypatch):
    def raise_not_found(ctx, op):
        raise XdrNotFoundError("No endpoint matched hostname H", resource="endpoint")

    monkeypatch.setattr(xdr_cli, "_run_with_xdr_client", raise_not_found)
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["endpoint", "get", "H"])
    # Exit code 4 (NOT_FOUND), not the old wrapped 404 -> 148.
    assert result.exit_code == 4
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ENDPOINT_NOT_FOUND"


def test_fields_projection(monkeypatch):
    rows = [{"endpoint_name": "h1", "ip": ["1.2.3.4"], "os_type": "LINUX"}]
    monkeypatch.setattr(xdr_cli, "_run_with_xdr_client", lambda ctx, op: rows)
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["endpoint", "list", "--fields", "endpoint_name,ip"])
    payload = json.loads(result.output)
    assert payload["data"] == [{"endpoint_name": "h1", "ip": ["1.2.3.4"]}]


def test_yara_scan_usage_error_exit_code():
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["script", "yara-memory-scan", __file__])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "VALIDATION"
