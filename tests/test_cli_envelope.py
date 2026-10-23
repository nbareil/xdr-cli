import json

import click
from click.testing import CliRunner

from xdr_cli.models import ListPage
from xdr_cli.xdr_cli import _parse_fields, _project, _render_page, cli

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


def test_render_page_json_includes_paging_meta():
    page = ListPage(
        items=[{"id": 1, "x": 2}], offset=10, total_count=50, next_offset=11,
        sort_field="creation_time", sort_order="desc",
    )

    @click.command()
    @click.pass_context
    def show(ctx):
        ctx.obj = {"json": True}
        _render_page(ctx, page, fields=["id"])

    payload = json.loads(CliRunner().invoke(show).output)
    assert payload["data"] == [{"id": 1}]
    assert payload["meta"] == {
        "offset": 10,
        "total_count": 50,
        "next_offset": 11,
        "sort": {"field": "creation_time", "order": "desc"},
    }


def test_render_page_text_hints_next_offset_on_stderr():
    page = ListPage(
        items=[{"id": 1}], offset=0, total_count=5, next_offset=1,
        sort_field="creation_time", sort_order="desc",
    )

    @click.command()
    @click.pass_context
    def show(ctx):
        ctx.obj = {}
        _render_page(ctx, page)

    result = CliRunner(mix_stderr=False).invoke(show)
    assert json.loads(result.stdout) == {"id": 1}
    assert "--offset 1" in result.stderr


def test_list_rejects_unsupported_sort_field():
    # alert_id is not sortable server-side (get_alerts returns 500).
    result = CliRunner().invoke(cli, AUTH + ["alert", "list", "--sort", "alert_id"])
    assert result.exit_code == 2
