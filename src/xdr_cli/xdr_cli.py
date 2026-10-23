import asyncio
import io
import json
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
import subprocess
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

import click

from .client import XdrClient
from .errors import XdrError
from .models import XdrClientOptions, XdrCredentials, XdrTenant
from .output.envelope import (
    EXIT_NOT_FOUND,
    EXIT_PARTIAL,
    EXIT_USAGE,
    emit,
    emit_error,
    error_code_for,
    exit_code_for,
)
from .output.excel import write_xql_results_excel
from .services.alerts import AlertsService
from .services.endpoints import EndpointsService
from .services.incidents import IncidentsService

# API Documentation: https://cortex-panw.stoplight.io/docs/cortex-xdr/branches/main/axpm6b98x4p18-cortex-xdr-api-overview

AUTH_FILE = os.path.join(os.path.expanduser("~"), ".xdr-cli-auth.json")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)


def _set_log_level(level: int) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level)
        return
    root.setLevel(level)


# --------------------------------------------------------------------------- #
# Auth file helpers (secure, atomic)
# --------------------------------------------------------------------------- #
def _load_auth(auth_file: str = AUTH_FILE) -> Dict[str, dict]:
    with open(auth_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_auth(tenants: Dict[str, dict], auth_file: str = AUTH_FILE) -> None:
    directory = os.path.dirname(os.path.abspath(auth_file)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".xdr-cli-auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(tenants, fh)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, auth_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _tenant_from_ctx(ctx: click.Context) -> XdrTenant:
    base_url = (ctx.obj or {}).get("base_url", "")
    api_key_id = (ctx.obj or {}).get("api_key_id", "")
    api_key_hash = (ctx.obj or {}).get("api_key_hash", "")
    if not (base_url and api_key_id and api_key_hash):
        raise click.ClickException("Missing authentication bits")
    return XdrTenant(
        tenant_base_url=base_url,
        credentials=XdrCredentials(
            api_key_id=str(api_key_id),
            api_key_hash=str(api_key_hash),
        ),
    )


def _client_options(ctx: click.Context) -> XdrClientOptions:
    read_only = bool((ctx.obj or {}).get("read_only")) or bool(
        os.environ.get("XDR_CLI_READONLY")
    )
    return XdrClientOptions(read_only=read_only)


def _run_with_xdr_client(ctx: click.Context, op: Callable[[XdrClient], Any]) -> Any:
    async def _runner():
        async with XdrClient(_tenant_from_ctx(ctx), options=_client_options(ctx)) as client:
            return await op(client)

    return asyncio.run(_runner())


def _execute(ctx: click.Context, op: Callable[[XdrClient], Any]) -> Any:
    """Run an async op against the XDR client, mapping library errors to a
    structured error + stable exit code. Never returns on error."""
    try:
        return _run_with_xdr_client(ctx, op)
    except XdrError as exc:
        emit_error(ctx, error_code_for(exc), str(exc), exit_code_for(exc))
        raise  # unreachable: emit_error raises SystemExit


def _json_mode(ctx: click.Context) -> bool:
    return bool((ctx.obj or {}).get("json"))


def _project(item: Dict, fields: Optional[List[str]]) -> Dict:
    if not fields:
        return item
    return {f: item.get(f, "N/A") for f in fields}


def _parse_fields(fields: Optional[str]) -> Optional[List[str]]:
    if not fields:
        return None
    return [f.strip() for f in fields.split(",") if f.strip()]


def _render_list(ctx: click.Context, items: List[Dict], fields: Optional[List[str]] = None, indent: Optional[int] = None) -> None:
    projected = [_project(i, fields) for i in items] if fields else items
    if _json_mode(ctx):
        emit(ctx, projected)
        return
    for item in projected:
        click.echo(json.dumps(item, indent=indent))


def _list_options(service_cls, noun: str) -> Callable:
    """Shared paging/sorting options for the paginated list commands."""
    options = [
        click.option("--fields", "-f", help="Comma-separated keys to keep", type=str),
        click.option("--limit", "-l", help="Stop after N %s" % noun, type=click.IntRange(min=1), default=None),
        click.option("--offset", "-o", help="Skip the first N %s" % noun, type=click.IntRange(min=0), default=0),
        click.option("--cursor", "-c", help="Resume a listing from a previous next_cursor", type=str, default=None),
        click.option(
            "--sort",
            "sort_field",
            type=click.Choice(list(service_cls.SORT_FIELDS)),
            default=None,
            help="Sort field (default: %s)" % service_cls.SORT_FIELDS[0],
        ),
        click.option(
            "--order",
            "sort_order",
            type=click.Choice(["asc", "desc"]),
            default=None,
            help="Sort order (default: %s)" % service_cls.DEFAULT_SORT_ORDER,
        ),
    ]

    def decorator(f: Callable) -> Callable:
        for option in reversed(options):
            f = option(f)
        return f

    return decorator


def _stream_list(ctx: click.Context, service_name: str, fields: Optional[str], indent: Optional[int] = None, **kwargs: Any) -> None:
    """Run a paginated listing, printing each page as it arrives.

    Text mode prints rows as soon as their page lands, so memory stays flat
    and output survives a failure. ``--json`` buffers into one envelope. If a
    page fails after others succeeded, the rows so far are kept, the cursor
    to resume from is reported, and the exit code is EXIT_PARTIAL.
    """
    keep = _parse_fields(fields)
    json_mode = _json_mode(ctx)
    state: Dict[str, Any] = {"items": [], "count": 0, "page": None}

    async def op(client: XdrClient):
        try:
            async for page in getattr(client, service_name).iter_pages(**kwargs):
                rows = [_project(i, keep) for i in page.items] if keep else page.items
                if json_mode:
                    state["items"].extend(rows)
                else:
                    for row in rows:
                        click.echo(json.dumps(row, indent=indent))
                state["count"] += len(rows)
                state["page"] = page
        except XdrError as exc:
            if state["page"] is None:
                raise
            return exc
        return None

    failure = _execute(ctx, op)
    page = state["page"]
    meta = {
        "count": state["count"],
        "total_count": page.total_count,
        "next_cursor": page.next_cursor,
        "sort": {"field": page.sort_field, "order": page.sort_order},
    }
    if failure is not None:
        message = "Listing stopped after %d rows: %s. Resume with --cursor %s" % (
            state["count"], failure, page.next_cursor)
        extra = {"data": state["items"], "meta": meta} if json_mode else None
        emit_error(ctx, error_code_for(failure), message, EXIT_PARTIAL, extra=extra)
    if json_mode:
        emit(ctx, state["items"], meta=meta)
        return
    if page.next_cursor is not None:
        click.echo(
            "Showing %d of %d; next page: --cursor %s" % (state["count"], page.total_count, page.next_cursor),
            err=True,
        )


def _render_obj(ctx: click.Context, obj: Any, indent: Optional[int] = None) -> None:
    if _json_mode(ctx):
        emit(ctx, obj)
        return
    click.echo(json.dumps(obj, indent=indent))


def _readonly_active(ctx: click.Context) -> bool:
    return _client_options(ctx).read_only


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #
@click.group()
@click.option("--verbose", default=False, help="Verbose mode", is_flag=True)
@click.option("--debug", default=False, help="Debug mode", is_flag=True)
@click.option("--base-url", default="", help="Base URL to XDR tenant")
@click.option("--api-key-id", default="", help="API Key ID")
@click.option("--api-key-hash", default="", help="API Key Hash")
@click.option("--profile", "-p", default="", help="Select XDR tenant")
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit a structured {ok,data,error} JSON envelope")
@click.option("--read-only", is_flag=True, default=False, help="Refuse all mutating operations (also via XDR_CLI_READONLY)")
@click.pass_context
def cli(ctx, base_url, verbose, debug, api_key_id, api_key_hash, profile, json_output, read_only):
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["debug"] = debug
    ctx.obj["json"] = json_output
    ctx.obj["read_only"] = read_only
    if verbose:
        _set_log_level(logging.INFO)
    if debug:
        _set_log_level(logging.DEBUG)
    if profile:
        try:
            profiles = _load_auth()
        except FileNotFoundError:
            emit_error(ctx, "CONFIG", f"Auth file not found: {AUTH_FILE}", EXIT_USAGE)
            return
        except json.JSONDecodeError as exc:
            emit_error(ctx, "CONFIG", f"Failed to parse {AUTH_FILE}: {exc}", EXIT_USAGE)
            return
        if profile in profiles:
            ctx.obj["base_url"] = profiles[profile]["base_url"]
            ctx.obj["api_key_id"] = profiles[profile]["api_key_id"]
            ctx.obj["api_key_hash"] = profiles[profile]["api_key_hash"]
        else:
            available = "\n".join(f" - {s}" for s in profiles)
            emit_error(
                ctx,
                "CONFIG",
                "Profile %r does not exist, available profiles:\n%s" % (profile, available),
                EXIT_USAGE,
            )
            return
    else:
        ctx.obj["base_url"] = base_url
        ctx.obj["api_key_id"] = api_key_id
        ctx.obj["api_key_hash"] = api_key_hash
    if (
        not all([ctx.obj["base_url"], ctx.obj["api_key_id"], ctx.obj["api_key_hash"]])
        and ctx.invoked_subcommand not in {"config", "slack", "mcp", "completion"}
    ):
        click.echo("Missing authentication bits", err=True)


# --------------------------------------------------------------------------- #
# Incident
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def incident(ctx):
    pass


@incident.command("list")
@_list_options(IncidentsService, "incidents")
@click.pass_context
def list_incidents(ctx: click.Context, fields: Optional[str], **kwargs: Any):
    _stream_list(ctx, "incidents", fields, **kwargs)


@incident.command("get")
@click.argument("incident_id")
@click.pass_context
def get_incident(ctx: click.Context, incident_id: str) -> None:
    obj = _execute(ctx, lambda client: client.incidents.get(incident_id))
    _render_obj(ctx, obj)


@incident.command("close")
@click.argument("incident_id")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def close_incident(ctx: click.Context, incident_id: str, dry_run: bool):
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "incident close", "incident_id": incident_id})
        return
    obj = _execute(ctx, lambda client: client.incidents.close(incident_id))
    _render_obj(ctx, obj)


# --------------------------------------------------------------------------- #
# Alert
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def alert(ctx):
    pass


@alert.command("list")
@_list_options(AlertsService, "alerts")
@click.pass_context
def list_alerts(ctx: click.Context, fields: Optional[str], **kwargs: Any):
    _stream_list(ctx, "alerts", fields, **kwargs)


@alert.command("close")
@click.argument("alert_id", type=int, nargs=-1)
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def close_alert(ctx: click.Context, alert_id: List[int], dry_run: bool):
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "alert close", "alert_ids": list(alert_id)})
        return
    obj = _execute(ctx, lambda client: client.alerts.close(list(alert_id)))
    _render_obj(ctx, obj)


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def endpoint(ctx: click.Context):
    pass


@endpoint.command("list")
@click.option("--platform", "-p", help="Filter by platform: linux, windows, macos, android", type=str)
@_list_options(EndpointsService, "endpoints")
@click.pass_context
def endpoint_list(ctx: click.Context, fields: Optional[str], platform: Optional[str], **kwargs: Any):
    """List endpoints, optionally filtered by platform."""
    _stream_list(ctx, "endpoints", fields, indent=4, platform=platform or None, **kwargs)


@endpoint.command("get")
@click.argument("hostname")
@click.option("--field", "-f", help="Return only this field", type=str)
@click.pass_context
def endpoint_get(ctx: click.Context, hostname: str, field: str = None):
    """Get more information about an endpoint."""
    endpoints = _execute(ctx, lambda client: client.endpoints.get_by_hostname(hostname))
    if _json_mode(ctx):
        emit(ctx, [_project(e, [field]) for e in endpoints] if field else endpoints)
        return
    for ep in endpoints:
        if field:
            click.echo(ep.get(field, "N/A"))
        else:
            click.echo(json.dumps(ep, indent=4))


@endpoint.command("who-has")
@click.argument("ip")
@click.option("--raw", is_flag=True, help="Display the whole endpoint object")
@click.pass_context
def endpoint_who_has(ctx: click.Context, ip: str, raw: bool = False):
    """Resolve an IP address to a hostname."""
    endpoints = _execute(ctx, lambda client: client.endpoints.who_has(ip))
    if not endpoints:
        emit_error(ctx, "ENDPOINT_NOT_FOUND", "No endpoint matched IP %s" % ip, EXIT_NOT_FOUND)
        return
    if _json_mode(ctx):
        emit(ctx, endpoints if raw else [e.get("endpoint_name") for e in endpoints])
        return
    for ep in endpoints:
        if raw:
            click.echo(json.dumps(ep, indent=4))
        else:
            click.echo(ep["endpoint_name"])


def _target_kwargs(endpoint_id: Optional[str], hostname: Optional[str]) -> Dict[str, Optional[str]]:
    return {"endpoint_id": endpoint_id, "hostname": hostname}


@endpoint.command("isolate")
@click.option("--endpoint-id", "-e", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def endpoint_isolate(ctx: click.Context, endpoint_id: str, hostname: str, dry_run: bool):
    """Network-isolate an endpoint."""
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "isolate", **_target_kwargs(endpoint_id, hostname)})
        return
    obj = _execute(ctx, lambda c: c.actions.isolate(endpoint_id=endpoint_id, hostname=hostname))
    _render_obj(ctx, obj)


@endpoint.command("unisolate")
@click.option("--endpoint-id", "-e", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def endpoint_unisolate(ctx: click.Context, endpoint_id: str, hostname: str, dry_run: bool):
    """Remove network isolation from an endpoint."""
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "unisolate", **_target_kwargs(endpoint_id, hostname)})
        return
    obj = _execute(ctx, lambda c: c.actions.unisolate(endpoint_id=endpoint_id, hostname=hostname))
    _render_obj(ctx, obj)


@endpoint.command("scan")
@click.option("--endpoint-id", "-e", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--all", "all_endpoints", is_flag=True, help="Scan all endpoints matching --platform")
@click.option("--platform", type=str, help="Platform filter when using --all")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def endpoint_scan(ctx: click.Context, endpoint_id: str, hostname: str, all_endpoints: bool, platform: str, dry_run: bool):
    """Trigger a malware scan on an endpoint (or all of a platform)."""
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "scan", "all": all_endpoints, "platform": platform, **_target_kwargs(endpoint_id, hostname)})
        return
    obj = _execute(
        ctx,
        lambda c: c.actions.scan(endpoint_id=endpoint_id, hostname=hostname, all_endpoints=all_endpoints, platform=platform),
    )
    _render_obj(ctx, obj)


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def triage(ctx: click.Context):
    pass


@triage.command("presets")
@click.pass_context
def triage_presets(ctx: click.Context):
    presets = _execute(ctx, lambda client: client.triage.presets())
    _render_list(ctx, presets)


@triage.command("new")
@click.argument("hostname")
@click.option("--collector-uuid", "-c", help="Triage configuration UUID (optional)", type=str, default="")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def triage_new(ctx: click.Context, hostname: str, collector_uuid: str, dry_run: bool):
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "triage new", "hostname": hostname, "collector_uuid": collector_uuid or None})
        return
    reply = _execute(
        ctx,
        lambda client: client.triage.start_by_hostname(hostname, collector_uuid=collector_uuid or None),
    )
    _render_obj(ctx, reply)


@triage.command("status")
@click.argument("group_action_id", type=int, nargs=-1)
@click.pass_context
def triage_status(ctx: click.Context, group_action_id: Tuple[int, ...]):
    if not group_action_id:
        emit_error(ctx, "VALIDATION", "Provide one or more action IDs", EXIT_USAGE)
        return
    payloads = _execute(ctx, lambda client: client.triage.statuses(list(group_action_id)))
    _render_list(ctx, payloads)


# --------------------------------------------------------------------------- #
# Script
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def script(ctx):
    pass


@script.command()
@click.pass_context
@click.option("--incident-id", help="Associate the script execution with an incident ID", type=str)
@click.option("--endpoint_id", "-e", help="Endpoint where to execute the script", type=str, multiple=True)
@click.option("--hostname", help="Endpoint hostname where to execute the script", type=str, multiple=True)
@click.option("--timeout", "-t", help="Execution timeout for the script (in seconds)", default=600, type=int)
@click.option("--wait", is_flag=True, help="Block until the script completes and return its results")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.argument("script", type=click.File())
def run(
    ctx,
    incident_id: str,
    script: io.FileIO,
    endpoint_id: List[str],
    hostname: List[str],
    timeout: int,
    wait: bool,
    dry_run: bool,
):
    """Run a Python snippet on one or more endpoints."""
    content = script.read()
    if dry_run:
        _render_obj(ctx, {
            "dry_run": True,
            "action": "script run",
            "endpoint_ids": list(endpoint_id),
            "hostnames": list(hostname),
            "timeout": timeout,
            "bytes": len(content),
        })
        return
    if wait:
        reply = _execute(
            ctx,
            lambda client: client.scripts.run_snippet_and_wait(
                script_content=content,
                endpoint_ids=list(endpoint_id),
                hostnames=list(hostname),
                timeout=timeout,
                incident_id=incident_id,
            ),
        )
    else:
        reply = _execute(
            ctx,
            lambda client: client.scripts.run_snippet(
                script_content=content,
                endpoint_ids=list(endpoint_id),
                hostnames=list(hostname),
                timeout=timeout,
                incident_id=incident_id,
            ),
        )
    _render_obj(ctx, reply)


@script.command
@click.argument("yara_ruleset", type=click.File("rb"))
@click.option("--all", "-a", help="Select all endpoints matching platform", default=False, is_flag=True)
@click.option("--endpoint", "-e", help="Execute the Yara scan only on these endpoints", type=str, multiple=True)
@click.option("--platform", "-p", help="Filter by platform name: linux, windows, macos, android", multiple=True, type=str)
@click.pass_context
def yara_memory_scan(ctx, yara_ruleset: io.FileIO, all: bool, platform: List[str], endpoint: List[str]):
    if not (all or endpoint):
        emit_error(ctx, "VALIDATION", "Select --all or --endpoint to target endpoints", EXIT_USAGE)
        return
    if all and endpoint:
        emit_error(ctx, "VALIDATION", "Options --all and --endpoint are mutually exclusive", EXIT_USAGE)
        return
    if all and not platform:
        emit_error(ctx, "VALIDATION", "Option --all requires --platform", EXIT_USAGE)
        return
    result = _execute(
        ctx,
        lambda client: client.scripts.yara_memory_scan(
            yara_ruleset_bytes=yara_ruleset.read(),
            endpoint_ids=list(endpoint) if not all else None,
            all_platforms=list(platform) if all else None,
            timeout=600,
        ),
    )
    _render_obj(ctx, result)


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def results(ctx, action_id):
    obj = _execute(ctx, lambda client: client.scripts.execution_results(action_id))
    _render_obj(ctx, obj)


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def status(ctx, action_id):
    obj = _execute(ctx, lambda client: client.scripts.execution_status(action_id))
    _render_obj(ctx, obj)


@script.command
@click.pass_context
@click.argument("action_id", type=int)
@click.argument("endpoint_id", type=str)
def result_files(ctx, endpoint_id: str, action_id: int):
    obj = _execute(
        ctx,
        lambda client: client.scripts.result_files(action_id=int(action_id), endpoint_id=endpoint_id),
    )
    _render_obj(ctx, obj)


# --------------------------------------------------------------------------- #
# File quarantine / restore
# --------------------------------------------------------------------------- #
@cli.group()
@click.pass_context
def file(ctx: click.Context):
    """File response actions."""
    pass


@file.command("quarantine")
@click.option("--endpoint-id", "-e", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--path", required=True, type=str, help="File path to quarantine")
@click.option("--sha256", required=True, type=str, help="SHA256 of the file")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def file_quarantine(ctx, endpoint_id, hostname, path, sha256, dry_run):
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "quarantine", "path": path, "sha256": sha256, **_target_kwargs(endpoint_id, hostname)})
        return
    obj = _execute(ctx, lambda c: c.actions.quarantine(file_path=path, file_hash=sha256, endpoint_id=endpoint_id, hostname=hostname))
    _render_obj(ctx, obj)


@file.command("restore")
@click.option("--endpoint-id", "-e", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--sha256", required=True, type=str, help="SHA256 of the file to restore")
@click.option("--dry-run", is_flag=True, help="Show what would happen without calling the API")
@click.pass_context
def file_restore(ctx, endpoint_id, hostname, sha256, dry_run):
    if dry_run:
        _render_obj(ctx, {"dry_run": True, "action": "restore", "sha256": sha256, **_target_kwargs(endpoint_id, hostname)})
        return
    obj = _execute(ctx, lambda c: c.actions.restore(file_hash=sha256, endpoint_id=endpoint_id, hostname=hostname))
    _render_obj(ctx, obj)


# --------------------------------------------------------------------------- #
# get-file (retrieve + extract)
# --------------------------------------------------------------------------- #
def _safe_extract_zip(zip_path: pathlib.Path, output_dir: pathlib.Path, password: bytes):
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            destination = (output_dir / name).resolve()
            if os.path.commonpath([str(output_dir.resolve()), str(destination)]) != str(output_dir.resolve()):
                raise click.ClickException(f"Unsafe zip entry path: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, pwd=password) as src, open(destination, "wb") as dst:
                dst.write(src.read())


def _flatten_endpoint_path(path: str) -> str:
    normalized = path.replace("\\", "_").replace("/", "_").replace(":", "")
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("._")
    return normalized or "file"


def _rename_payload_files_from_manifest(output_dir: pathlib.Path) -> None:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        raise click.ClickException(f"Failed to parse manifest.json: {exc}")

    used_names = {p.name for p in output_dir.iterdir() if p.is_file()}
    for entry in manifest.get("entries", []):
        for file_info in entry.get("files", []):
            archived_path = str(file_info.get("archivedPath", "")).replace("\\", "/")
            original_path = str(file_info.get("path") or file_info.get("requestedPath") or "")
            if not archived_path or not original_path:
                continue
            src = (output_dir / archived_path).resolve()
            if not src.exists() or not src.is_file():
                continue
            base_name = _flatten_endpoint_path(original_path)
            target = output_dir / base_name
            if target.name in used_names and target != src:
                stem = target.stem
                suffix = target.suffix
                i = 1
                while True:
                    candidate = output_dir / f"{stem}_{i}{suffix}"
                    if candidate.name not in used_names:
                        target = candidate
                        break
                    i += 1
            if target != src:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(target))
            used_names.add(target.name)

    for directory in sorted(output_dir.glob("entry_*"), reverse=True):
        if directory.is_dir():
            for subdir in sorted(directory.rglob("*"), reverse=True):
                if subdir.is_dir():
                    try:
                        subdir.rmdir()
                    except OSError:
                        pass
            try:
                directory.rmdir()
            except OSError:
                pass


def _extract_retrieved_file_zip(content: bytes, output_dir: pathlib.Path) -> None:
    password = b"suspicious"
    output_dir.mkdir(parents=True, exist_ok=True)
    outer_zip_path = output_dir / "retrieved_file.zip"
    outer_zip_path.write_bytes(content)
    _safe_extract_zip(outer_zip_path, output_dir, password)

    inner_zip_path = output_dir / "script_output.zip"
    if inner_zip_path.exists():
        _safe_extract_zip(inner_zip_path, output_dir, password)
        _rename_payload_files_from_manifest(output_dir)
        inner_zip_path.unlink()
    outer_zip_path.unlink()


@cli.command("get-file")
@click.option("--endpoint-id", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option(
    "--output", "-o",
    type=click.Path(file_okay=False, dir_okay=True, path_type=pathlib.Path),
    required=True,
    help="Directory where endpoint files will be stored",
)
@click.argument("path", nargs=-1)
@click.pass_context
def get_file(ctx: click.Context, endpoint_id: str, path: List[str], output: pathlib.Path, hostname):
    """Retrieve a file from an endpoint and extract the returned archives."""
    content = _execute(
        ctx,
        lambda client: client.files.retrieve(endpoint_id=endpoint_id, hostname=hostname, paths=list(path)),
    )
    _extract_retrieved_file_zip(content, output)
    if _json_mode(ctx):
        emit(ctx, {"output_dir": str(output), "paths": list(path)})


# --------------------------------------------------------------------------- #
# XQL
# --------------------------------------------------------------------------- #
def open_editor() -> str:
    # ripped from https://stackoverflow.com/a/19377899
    with tempfile.NamedTemporaryFile() as tmpfile:
        subprocess.call([os.environ.get("EDITOR", None) or "vim", tmpfile.name])
        with open(tmpfile.name, "r") as f:
            data = f.read()
    return data


@cli.command
@click.option("--begin-time", type=click.DateTime(), default=None, help="Begin of the timeframe")
@click.option("--end-time", type=click.DateTime(), default=None, help="End of the timeframe")
@click.option("--output", type=click.Path(exists=False), default=None, help="Output")
@click.option("--excel", is_flag=True, help="Format output as Excel file")
@click.argument("xql_query", type=str, default="")
@click.pass_context
def xql(ctx: click.Context, xql_query: Optional[str], begin_time=None, end_time=None, excel: bool = False, output: str = ""):
    """Execute an XQL query."""
    if excel and not output:
        emit_error(ctx, "VALIDATION", "Excel output requires --output", EXIT_USAGE)
        return
    if not xql_query:
        if not sys.stdin.isatty():
            emit_error(ctx, "VALIDATION", "No XQL query provided (an editor cannot be opened in a non-interactive session)", EXIT_USAGE)
            return
        xql_query = open_editor()
    results = _execute(
        ctx,
        lambda client: client.xql.run(query=xql_query, begin_time=begin_time, end_time=end_time),
    )
    if excel:
        write_xql_results_excel(results, output)
        if _json_mode(ctx):
            emit(ctx, {"output": output, "rows": len(results)})
        return
    _render_list(ctx, results)


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #
@cli.command("completion")
@click.argument("shell", required=False)
@click.pass_context
def completion(ctx: click.Context, shell: Optional[str]):
    if not shell:
        shell = os.path.basename(os.environ.get("SHELL", "")).lower()
    if shell not in ("bash", "zsh"):
        click.echo("Shell must be bash or zsh (or set $SHELL accordingly)", err=True)
        sys.exit(EXIT_USAGE)
    prog_name = ctx.info_name or "xdr-cli"
    complete_name = prog_name.replace("-", "_").replace(".", "_")
    complete_var = f"_{complete_name}_COMPLETE".upper()
    completion_cls = click.shell_completion.get_completion_class(shell)
    comp = completion_cls(cli, {}, prog_name, complete_var)
    click.echo(comp.source())


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@cli.group
def config():
    """Configure xdr-cli."""
    pass


@config.group
def tenant():
    """Add, update, or remove an XDR tenant."""
    pass


@tenant.command
@click.argument("name")
@click.option("--base-url", default=None, help="Base URL (without https://)")
@click.option("--api-key-hash", default=None, help="API Key Hash")
@click.option("--api-key-id", default=None, type=int, help="API Key ID")
@click.option("--yes", "-y", is_flag=True, help="Do not prompt for confirmation")
def add(name: str, base_url, api_key_hash, api_key_id, yes):
    """Add or update a tenant profile."""
    tenants: Dict[str, dict] = {}
    file_exists = os.path.exists(AUTH_FILE)
    interactive = sys.stdin.isatty()
    if file_exists:
        tenants = _load_auth()
    elif not (yes or interactive):
        raise click.ClickException(f"Auth file {AUTH_FILE} does not exist (re-run with --yes to create it)")
    elif not yes:
        click.confirm(f"Authentication file {AUTH_FILE} does not exist, should I create it?", abort=True)

    if name in tenants and not yes:
        if not interactive:
            raise click.ClickException(f"Tenant {name} exists (re-run with --yes to overwrite)")
        click.confirm(f"This tenant {name} already exists, should I update it?", abort=True)

    if base_url is None:
        if not interactive:
            raise click.ClickException("--base-url is required in a non-interactive session")
        base_url = click.prompt("Base URL (without https://)", type=str)
        click.echo(f"Create an Advanced API key manually on https://{base_url}/configuration/api-keys then:\n")
    if api_key_hash is None:
        if not interactive:
            raise click.ClickException("--api-key-hash is required in a non-interactive session")
        api_key_hash = click.prompt("API Key Hash", type=str)
    if api_key_id is None:
        if not interactive:
            raise click.ClickException("--api-key-id is required in a non-interactive session")
        api_key_id = click.prompt("API Key ID", type=int)

    tenants[name] = dict(api_key_id=api_key_id, api_key_hash=api_key_hash, base_url=base_url)
    _save_auth(tenants)


@tenant.command("list")
def list_tenants():
    try:
        tenants = _load_auth()
    except FileNotFoundError:
        return
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Failed to parse {AUTH_FILE}: {exc}")
    for name in tenants:
        click.echo(name)


@tenant.command
@click.argument("name")
def remove(name: str):
    tenants = _load_auth()
    if name not in tenants:
        raise click.ClickException(f"Tenant {name} does not exist")
    del tenants[name]
    _save_auth(tenants)


def _load_tenants_from_auth_file(auth_file: str) -> Dict[str, XdrTenant]:
    try:
        raw = _load_auth(auth_file)
    except FileNotFoundError:
        raise click.ClickException(f"Auth file not found: {auth_file}")
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Failed to parse auth file {auth_file}: {exc}")

    tenants: Dict[str, XdrTenant] = {}
    for alias, cfg in raw.items():
        try:
            tenants[alias] = XdrTenant(
                tenant_base_url=str(cfg["base_url"]),
                credentials=XdrCredentials(
                    api_key_id=str(cfg["api_key_id"]),
                    api_key_hash=str(cfg["api_key_hash"]),
                ),
            )
        except KeyError as exc:
            raise click.ClickException(f"Tenant profile '{alias}' is missing required field: {exc}")
    return tenants


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
@cli.group()
def slack():
    """Slack bot integration."""
    pass


@slack.command("serve")
@click.option("--auth-file", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), default=pathlib.Path(AUTH_FILE), show_default=True, help="Path to xdr-cli tenant auth file")
@click.option("--signing-secret", envvar="SLACK_SIGNING_SECRET", default="", help="Slack app signing secret (or SLACK_SIGNING_SECRET)")
@click.option("--bot-token", envvar="SLACK_BOT_TOKEN", default="", help="Slack bot token xoxb-... (or SLACK_BOT_TOKEN)")
@click.option("--app-token", envvar="SLACK_APP_TOKEN", default="", help="Slack app-level token xapp-... for Socket Mode (or SLACK_APP_TOKEN)")
@click.option("--default-tenant", envvar="XDR_SLACK_DEFAULT_TENANT", default="", help="Default tenant alias when a user omits --tenant")
@click.option("--timeout", type=float, default=30.0, show_default=True, help="XDR API timeout in seconds")
def slack_serve(auth_file, signing_secret, bot_token, app_token, default_tenant, timeout):
    """Run the Slack /xdr bot in Socket Mode."""
    if not signing_secret:
        raise click.ClickException("Missing Slack signing secret (--signing-secret or SLACK_SIGNING_SECRET)")
    if not bot_token:
        raise click.ClickException("Missing Slack bot token (--bot-token or SLACK_BOT_TOKEN)")
    if not app_token:
        raise click.ClickException("Missing Slack app token (--app-token or SLACK_APP_TOKEN)")

    from .slack import create_slack_app
    from .slack.tenant_store import InMemoryTenantStore

    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    except Exception as exc:  # pragma: no cover - optional dependency
        raise click.ClickException("slack-bolt Socket Mode support is unavailable. Install xdr-cli[slack].") from exc

    tenants = _load_tenants_from_auth_file(str(auth_file))
    if not tenants:
        raise click.ClickException(f"No tenant profiles found in {auth_file}")
    user_defaults = {"*": default_tenant} if default_tenant else {}
    tenant_store = InMemoryTenantStore(tenants=tenants, user_defaults=user_defaults)
    app = create_slack_app(
        signing_secret=signing_secret,
        bot_token=bot_token,
        tenant_store=tenant_store,
        client_options=XdrClientOptions(timeout_seconds=timeout),
    )

    async def _serve():
        handler = AsyncSocketModeHandler(app, app_token)
        logger.info("Starting Slack Socket Mode bot with %d tenant profile(s)", len(tenants))
        await handler.start_async()

    asyncio.run(_serve())


# --------------------------------------------------------------------------- #
# MCP
# --------------------------------------------------------------------------- #
@cli.group()
def mcp():
    """Model Context Protocol server."""
    pass


@mcp.command("serve")
@click.option("--auth-file", type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path), default=pathlib.Path(AUTH_FILE), show_default=True, help="Path to xdr-cli tenant auth file")
@click.option("--default-tenant", envvar="XDR_MCP_DEFAULT_TENANT", default="", help="Default tenant alias when a tool omits 'tenant'")
@click.option("--read-only", is_flag=True, default=False, help="Refuse mutating tools (also via XDR_CLI_READONLY)")
@click.option("--timeout", type=float, default=30.0, show_default=True, help="XDR API timeout in seconds")
def mcp_serve(auth_file, default_tenant, read_only, timeout):
    """Run the MCP server over stdio."""
    from .mcp.server import run_stdio_server

    tenants = _load_tenants_from_auth_file(str(auth_file))
    if not tenants:
        raise click.ClickException(f"No tenant profiles found in {auth_file}")
    read_only = read_only or bool(os.environ.get("XDR_CLI_READONLY"))
    options = XdrClientOptions(timeout_seconds=timeout, read_only=read_only)
    run_stdio_server(
        tenants=tenants,
        default_tenant=default_tenant or None,
        client_options=options,
    )


if __name__ == "__main__":
    cli()
