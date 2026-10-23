import asyncio
import json
from types import SimpleNamespace

from click.testing import CliRunner

from xdr_cli import xdr_cli as cli_module
from xdr_cli.errors import XdrApiError
from xdr_cli.services.alerts import AlertsService
from xdr_cli.xdr_cli import _parse_fields, _project, cli

AUTH = ["--base-url", "x", "--api-key-id", "1", "--api-key-hash", "h"]


# --- pure helpers ---------------------------------------------------------- #

def test_parse_fields_splits_and_trims():
    assert _parse_fields("a, b ,c") == ["a", "b", "c"]
    assert _parse_fields("") is None
    assert _parse_fields(None) is None


def test_project_keeps_only_requested_fields_with_na_default():
    item = {"endpoint_name": "h1", "ip": ["1.2.3.4"], "os_type": "LINUX"}
    assert _project(item, ["endpoint_name", "ip"]) == {"endpoint_name": "h1", "ip": ["1.2.3.4"]}
    assert _project(item, ["missing"]) == {"missing": "N/A"}
    assert _project(item, None) is item


# --- real CLI invocations (no mocks) --------------------------------------- #

def test_dry_run_success_envelope():
    # --dry-run short-circuits before any network call and routes through the
    # same success-envelope path, so this exercises the real --json output.
    result = CliRunner().invoke(
        cli, ["--json"] + AUTH + ["endpoint", "isolate", "--hostname", "H", "--dry-run"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["action"] == "isolate"


def test_yara_scan_usage_error_exit_code():
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["script", "yara-memory-scan", __file__])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION"


def test_readonly_blocks_mutation_end_to_end():
    # Exercises the whole stack: CLI -> XdrClient -> service guard. The guard
    # fires before any HTTP request, so no network/mock is needed.
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--json", "--read-only"] + AUTH + ["incident", "close", "42"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "READ_ONLY"


def test_xql_non_interactive_without_query_fails_fast():
    result = CliRunner().invoke(cli, ["--json"] + AUTH + ["xql"], input="")
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "VALIDATION"


def test_list_rejects_negative_offset():
    result = CliRunner().invoke(cli, AUTH + ["alert", "list", "--offset", "-1"])
    assert result.exit_code == 2


class FlakyAlerts:
    """Alert pages from a canned list; raises once ``fail_at`` pages served."""

    def __init__(self, pages, fail_at=None):
        self.pages = pages
        self.fail_at = fail_at
        self.calls = []

    async def post_json(self, uri, params, retry=False):
        self.calls.append(params["request_data"])
        if len(self.calls) - 1 == self.fail_at:
            raise XdrApiError(uri, 503, "Service Unavailable", "")
        index = len(self.calls) - 1
        rows = self.pages[index] if index < len(self.pages) else []
        return {"reply": {"alerts": rows, "total_count": sum(len(p) for p in self.pages)}}


def alert_pages(*sizes):
    ts, pages = 10_000, []
    for size in sizes:
        page = []
        for _ in range(size):
            page.append({"alert_id": str(ts), "detection_timestamp": ts})
            ts -= 1
        pages.append(page)
    return pages


def invoke_with(monkeypatch, transport, args, json_mode=False):
    client = SimpleNamespace(alerts=AlertsService(SimpleNamespace(transport=transport)))
    monkeypatch.setattr(cli_module, "_run_with_xdr_client", lambda ctx, op: asyncio.run(op(client)))
    flags = ["--json"] if json_mode else []
    return CliRunner(mix_stderr=False).invoke(cli, flags + AUTH + ["alert", "list"] + args)


def test_text_listing_streams_and_hints_cursor(monkeypatch):
    transport = FlakyAlerts(alert_pages(100, 100, 100))
    result = invoke_with(monkeypatch, transport, ["--fields", "alert_id", "--limit", "150"])
    assert result.exit_code == 0
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(lines) == 150 and lines[0] == {"alert_id": "10000"}
    assert "next page: --cursor " in result.stderr


def test_json_listing_meta(monkeypatch):
    transport = FlakyAlerts(alert_pages(100, 20))
    result = invoke_with(monkeypatch, transport, ["--fields", "alert_id"], json_mode=True)
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert len(payload["data"]) == 120
    assert payload["meta"] == {
        "count": 120,
        "total_count": 120,
        "next_cursor": None,
        "sort": {"field": "creation_time", "order": "desc"},
    }


def test_text_partial_failure_keeps_rows_and_reports_resume_cursor(monkeypatch):
    transport = FlakyAlerts(alert_pages(100, 100, 100), fail_at=2)
    result = invoke_with(monkeypatch, transport, ["--fields", "alert_id"])
    assert result.exit_code == 3
    assert len(result.stdout.splitlines()) == 200
    assert "Listing stopped after 200 rows" in result.stderr
    assert "Resume with --cursor " in result.stderr


def test_json_partial_failure_returns_partial_data_and_cursor(monkeypatch):
    transport = FlakyAlerts(alert_pages(100, 100, 100), fail_at=1)
    result = invoke_with(monkeypatch, transport, ["--fields", "alert_id"], json_mode=True)
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "API_ERROR"
    assert len(payload["data"]) == 100
    cursor = payload["meta"]["next_cursor"]

    # Resuming from that cursor picks up exactly where the listing stopped.
    resumed = FlakyAlerts(alert_pages(100, 100, 100)[1:])
    result = invoke_with(monkeypatch, resumed, ["--fields", "alert_id", "--cursor", cursor], json_mode=True)
    assert resumed.calls[0]["filters"] == [{"field": "creation_time", "operator": "lte", "value": 9901}]
    assert resumed.calls[0]["search_from"] == 1


def test_failure_on_first_page_is_a_plain_error(monkeypatch):
    transport = FlakyAlerts(alert_pages(100), fail_at=0)
    result = invoke_with(monkeypatch, transport, [], json_mode=True)
    assert result.exit_code == 1
    assert "data" not in json.loads(result.stdout)


def test_cursor_with_sort_is_a_usage_error(monkeypatch):
    transport = FlakyAlerts(alert_pages(1))
    result = invoke_with(monkeypatch, transport, ["--cursor", "abc", "--order", "asc"], json_mode=True)
    assert result.exit_code == 2
    assert transport.calls == []


def test_list_rejects_unsupported_sort_field():
    # alert_id is not sortable server-side (get_alerts returns 500).
    result = CliRunner().invoke(cli, AUTH + ["alert", "list", "--sort", "alert_id"])
    assert result.exit_code == 2
