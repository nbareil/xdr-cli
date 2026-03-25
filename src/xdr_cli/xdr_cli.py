import base64
import asyncio
import io
import tempfile
import subprocess
import os
import pathlib
import hashlib
import gzip
import json
import logging
import re
import shutil
import time
import sys
import string
import secrets
import zipfile
from typing import Dict, Optional, Tuple

import requests
import click

from typing import List
from datetime import datetime, timezone

from .client import XdrClient
from .errors import XdrError, XdrNotFoundError, XdrValidationError
from .models import XdrCredentials, XdrTenant
from .output.excel import write_xql_results_excel

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


def _run_with_xdr_client(ctx: click.Context, op):
    async def _runner():
        async with XdrClient(_tenant_from_ctx(ctx)) as client:
            return await op(client)

    try:
        return asyncio.run(_runner())
    except XdrError as exc:
        raise click.ClickException(str(exc))


@click.group()
@click.option("--verbose", default=False, help="Verbose mode", is_flag=True)
@click.option("--debug", default=False, help="Debug mode", is_flag=True)
@click.option("--base-url", default="", help="Base URL to XDR tenant")
@click.option("--api-key-id", default="", help="API Key ID")
@click.option("--api-key-hash", default="", help="API Key Hash")
@click.option("--profile", "-p", default="", help="Select XDR tenant")
@click.pass_context
def cli(ctx, base_url, verbose, debug, api_key_id, api_key_hash, profile):
    # ensure that ctx.obj exists and is a dict (in case `cli()` is called
    # by means other than the `if` block below)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["debug"] = debug
    if verbose:
        _set_log_level(logging.INFO)
    if debug:
        _set_log_level(logging.DEBUG)
    if profile:
        profiles = json.load(open(AUTH_FILE))
        if profile in profiles:
            ctx.obj["base_url"] = profiles[profile]["base_url"]
            ctx.obj["api_key_id"] = profiles[profile]["api_key_id"]
            ctx.obj["api_key_hash"] = profiles[profile]["api_key_hash"]
        else:
            logger.error(
                "This profile does not exist, please use one of the followings:\n"
                + "\n".join(map(lambda s: f" - {s}", profiles))
            )
            sys.exit(2)
    else:
        ctx.obj["base_url"] = base_url
        ctx.obj["api_key_id"] = api_key_id
        ctx.obj["api_key_hash"] = api_key_hash
    if (
        not all([ctx.obj["base_url"], ctx.obj["api_key_id"], ctx.obj["api_key_hash"]])
        and ctx.invoked_subcommand not in {"config", "slack"}
    ):
        click.echo("Missing authentication bits")


def _load_tenants_from_auth_file(auth_file: str) -> Dict[str, XdrTenant]:
    try:
        raw = json.load(open(auth_file))
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
            raise click.ClickException(
                f"Tenant profile '{alias}' is missing required field: {exc}"
            )
    return tenants


def generate_auth_headers(api_key_id, api_key) -> dict:
    nonce = "".join(
        [secrets.choice(string.ascii_letters + string.digits) for _ in range(64)]
    )
    timestamp = int(datetime.now(timezone.utc).timestamp()) * 1000
    auth_key = "%s%s%s" % (api_key, nonce, timestamp)
    auth_key = auth_key.encode("utf-8")
    api_key_hash = hashlib.sha256(auth_key).hexdigest()
    headers = {
        "x-xdr-timestamp": str(timestamp),
        "x-xdr-nonce": nonce,
        "x-xdr-auth-id": str(api_key_id),
        "Authorization": api_key_hash,
    }
    return headers


class XDRException(Exception):
    pass


def do_requests_post(
    ctx: click.Context, uri: str, parameters: dict = None, headers: dict = None
) -> requests.Response:
    parameters = parameters or {}
    req_headers = dict(headers or {})
    req_headers.update(
        **generate_auth_headers(ctx.obj["api_key_id"], ctx.obj["api_key_hash"])
    )
    base = str(ctx.obj.get("base_url") or "").strip().replace("https://", "").replace("http://", "").strip("/")
    base = base.split("/", 1)[0]
    if not base.startswith("api-"):
        base = f"api-{base}"
    url = f"https://{base}/public_api/v1/{uri}"
    res = requests.post(url, headers=req_headers, json=parameters)
    if res.status_code != 200:
        raise XDRException(
            f"{uri} failed with {res.status_code}: {res.reason} {res.text}"
        )
    return res


@cli.group()
@click.pass_context
def incident(ctx):
    pass


@cli.group()
@click.pass_context
def script(ctx):
    pass


py_yara_base_script = r"""
import base64
import json
import logging
import os

import yara
import psutil

yara_rules = base64.b64decode('XXX_XDR_CLI_INSERT_YARA_CONTENT_XXX').decode("utf-8")

# Do not edit past this line
# ------------------------------------------------------------------

logger = logging.getLogger("xdr_yara_process_scan")
logging.basicConfig(level=logging.ERROR)

def is_expected_yara_error(err: yara.Error):
    for arg in err.args:
        if "access denied" in arg:
            return True
        elif "internal error: 47" in arg:
            # #define ERROR_COULD_NOT_READ_PROCESS_MEMORY  47
            # https://github.com/VirusTotal/yara/blob/master/libyara/include/yara/error.h#L94C1-L95C1
            return True
    return False

class BytesEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return base64.b64encode(o).decode("ascii")
        else:
            return super().default(o)


allow_list_exe = [
    "C:\\Program Files\\Palo Alto Networks\\Traps\\tlaworker.exe",
    "C:\\Program Files\\Palo Alto Networks\\Traps\\cysandbox.exe",
    "C:\\Program Files (x86)\\ossec-agent\\wazuh-agent.exe",
]

def yara_process_scan():
    rules = yara.compile(source=yara_rules)
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            # Don't scan yourself
            continue
        logger.debug(f"Scanning process Name={process.name()} PID={process.pid}")
        try:
            matches = rules.match(pid=process.pid)
        except yara.Error as err:
            if is_expected_yara_error(err):
                log_func  = logger.warning
            else:
                log_func = logger.error
            log_func(f"Failed to scan process name {process.name()} PID={process.pid}: Error {err=}")
            continue
        if not matches:
            continue
        if process.exe() in allow_list_exe:
            continue
        logger.info(f"Process Name={process.name()} PID={process.pid} matched {len(matches)} Yara rules")
        obj = {
            "process": {
                "name": process.name(),
                "pid": process.pid,
                "ppid": process.ppid(),
                "cmdline": process.cmdline(),
                "exe": process.exe(),
            }
        }
        for m in matches:
            obj[m.rule] = {}
            for (offset, str_id, matched_data) in m.strings:
                obj[m.rule][str_id] = matched_data
        print(json.dumps(obj, cls=BytesEncoder))


if __name__ == "__main__":
    yara_process_scan()
"""


def build_py_script_from_yara(yara):
    b64blob = base64.b64encode(yara.read()).decode("utf-8")
    final_pyscript = py_yara_base_script.replace(
        "XXX_XDR_CLI_INSERT_YARA_CONTENT_XXX", b64blob
    )
    return final_pyscript


def do_script_run(ctx, name: str, script, endpoint_id: List[str], timeout: int):
    if hasattr(script, "seek"):
        script.seek(0)
    script_content = script.read()
    params = dict(
        request_data={
            "timeout": timeout,
            "snippet_code": script_content,
            "filters": [
                {
                    "field": "endpoint_id_list",
                    "operator": "in",
                    "value": endpoint_id,
                },
            ],
        }
    )
    res = do_requests_post(ctx, "scripts/run_snippet_code_script", params)
    obj = res.json()
    click.echo(json.dumps(obj["reply"]))
    return obj["reply"]

@script.command()
@click.pass_context
@click.option("--name", "-n", help="Script name", type=str)
@click.option("--incident-id", help="Associate the script execution with an incident ID", type=str)
@click.option(
    "--endpoint_id",
    "-e",
    help="Endpoint where to execute the script",
    type=str,
    multiple=True,
)
@click.option(
    "--timeout",
    "-t",
    help="Execution timeout for the script (in seconds)",
    default=600,
    type=int,
)
@click.argument("script", type=click.File())
def run(ctx, name: str, incident_id: str, script: io.FileIO, endpoint_id: List[str], timeout: int):
    reply = _run_with_xdr_client(
        ctx,
        lambda client: client.scripts.run_snippet(
            script_content=script.read(),
            endpoint_ids=list(endpoint_id),
            timeout=timeout,
            name=name,
            incident_id=incident_id,
        ),
    )
    click.echo(json.dumps(reply))


@script.command
@click.argument("yara_ruleset", type=click.File("rb"))
@click.option(
    "--all",
    "-a",
    help="Select all endpoints matching platform",
    default=False,
    is_flag=True,
)
@click.option(
    "--endpoint",
    "-e",
    help="Execute the Yara scan only on these selected endpoints",
    type=str,
    multiple=True,
)
@click.option(
    "--platform",
    "-p",
    help="Filter by platform name: linux, windows, macos, android",
    multiple=True,
    type=str,
)
@click.pass_context
def yara_memory_scan(
    ctx, yara_ruleset: io.FileIO, all: bool, platform: List[str], endpoint: List[str]
):
    if not (all or endpoint):
        logger.error("Select --all or --endpoint to target endpoints")
    if all and endpoint:
        logger.error(
            "Options --all and --endpoint are mutually exclusive, please select only one"
        )
        sys.exit(2)
    if all and not platform:
        logger.error("Option --all requires to select --platform")
        sys.exit(3)
    result = _run_with_xdr_client(
        ctx,
        lambda client: client.scripts.yara_memory_scan(
            yara_ruleset_bytes=yara_ruleset.read(),
            endpoint_ids=list(endpoint) if not all else None,
            all_platforms=list(platform) if all else None,
            timeout=600,
        ),
    )
    click.echo(json.dumps(result))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def results(ctx, action_id):
    obj = _run_with_xdr_client(ctx, lambda client: client.scripts.execution_results(action_id))
    click.echo(json.dumps(obj))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def status(ctx, action_id):
    obj = _run_with_xdr_client(ctx, lambda client: client.scripts.execution_status(action_id))
    click.echo(json.dumps(obj))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
@click.argument("endpoint_id", type=str)
def result_files(ctx, endpoint_id: str, action_id: str):
    obj = _run_with_xdr_client(
        ctx,
        lambda client: client.scripts.result_files(action_id=int(action_id), endpoint_id=endpoint_id),
    )
    click.echo(json.dumps(obj))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def get_execution_result(ctx, action_id):
    pass


def do_paginated_post_request(ctx: click.Context, uri: str, params: dict):
    offset = -1
    total_count = 0
    while offset < total_count:
        res = do_requests_post(ctx, uri, params)
        obj = res.json()
        yield obj
        offset += obj["reply"]["result_count"]
        total_count = obj["reply"]["total_count"]
        params["request_data"]["search_from"] = offset
        params["request_data"]["search_to"] = offset + 100


def do_list_incidents(ctx: click.Context):
    params = dict(request_data=dict())
    for obj in do_paginated_post_request(ctx, "incidents/get_incidents", params):
        yield obj["reply"]["incidents"]


@incident.command("list")
@click.pass_context
def list_incidents(ctx: click.Context):
    incidents = _run_with_xdr_client(ctx, lambda client: client.incidents.list())
    for incident in incidents:
        click.echo(json.dumps(incident))


@incident.command("get")
@click.argument("incident_id")
@click.pass_context
def get_incident(ctx: click.Context, incident_id: str) -> None:
    obj = _run_with_xdr_client(ctx, lambda client: client.incidents.get(incident_id))
    click.echo(json.dumps(obj))


@incident.command("close")
@click.argument("incident_id")
@click.pass_context
def close_incident(ctx: click.Context, incident_id: str):
    obj = _run_with_xdr_client(ctx, lambda client: client.incidents.close(incident_id))
    click.echo(json.dumps(obj))


@cli.group()
@click.pass_context
def alert(ctx):
    pass


def do_alert_list(ctx: click.Context):
    params = dict(request_data=dict())
    for obj in do_paginated_post_request(ctx, "alerts/get_alerts", params):
        alerts = obj["reply"]["alerts"]
        yield from alerts


@alert.command("list")
@click.pass_context
def list_alerts(ctx: click.Context):
    alerts = _run_with_xdr_client(ctx, lambda client: client.alerts.list())
    for alert in alerts:
        click.echo(json.dumps(alert))


@alert.command("close")
@click.argument("alert_id", type=int, nargs=-1)
@click.pass_context
def close_alert(ctx: click.Context, alert_id: List[int]):
    obj = _run_with_xdr_client(ctx, lambda client: client.alerts.close(list(alert_id)))
    click.echo(json.dumps(obj))


@cli.group()
@click.pass_context
def endpoint(ctx: click.Context):
    pass


@cli.group()
@click.pass_context
def triage(ctx: click.Context):
    pass


@cli.command("completion")
@click.argument("shell", required=False)
@click.pass_context
def completion(ctx: click.Context, shell: Optional[str]):
    if not shell:
        shell = os.path.basename(os.environ.get("SHELL", "")).lower()
    if shell not in ("bash", "zsh"):
        click.echo("Shell must be bash or zsh (or set $SHELL accordingly)")
        sys.exit(2)
    prog_name = ctx.info_name or "xdr-cli"
    complete_name = prog_name.replace("-", "_").replace(".", "_")
    complete_var = f"_{complete_name}_COMPLETE".upper()
    completion_cls = click.shell_completion.get_completion_class(shell)
    completion = completion_cls(cli, {}, prog_name, complete_var)
    click.echo(completion.source())


@endpoint.command("list")
@click.option(
    "--platform",
    "-p",
    help="Filter by platform name: linux, windows, macos, android",
    type=str,
)
@click.pass_context
def endpoint_list(ctx: click.Context, platform: str = ""):
    """Get more information about an endpoint"""
    endpoints = _run_with_xdr_client(ctx, lambda client: client.endpoints.list(platform=platform or None))
    for endpoint in endpoints:
        click.echo(json.dumps(endpoint, indent=4))


def do_endpoint_list(ctx: click.Context, platform: str):
    params = dict(request_data=dict(filters=[]))
    if platform:
        params["request_data"]["filters"].append(
            {"field": "platform", "operator": "in", "value": [platform]}
        )
    for obj in do_paginated_post_request(ctx, "endpoints/get_endpoint", params):
        endpoints = obj["reply"]["endpoints"]
        yield from endpoints


@endpoint.command("get")
@click.argument("hostname")
@click.option("--field", "-f", help="Return only this field", type=str)
@click.pass_context
def endpoint_get(ctx: click.Context, hostname: str, field: str = None):
    """Get more information about an endpoint"""
    try:
        endpoints = _run_with_xdr_client(ctx, lambda client: client.endpoints.get_by_hostname(hostname))
    except click.ClickException as exc:
        if "No endpoint matched hostname" in str(exc):
            click.echo("No match")
            sys.exit(404)
        raise
    for endpoint in endpoints:
        if field:
            click.echo(endpoint.get(field, "N/A"))
        else:
            click.echo(json.dumps(endpoint, indent=4))


def do_endpoint_get(ctx: click.Context, hostname: str):
    # click.echo(hostname)
    parameters = {
        "request_data": {
            "filters": [
                {"field": "hostname", "operator": "in", "value": [hostname]},
            ],
        }
    }
    res = do_requests_post(ctx, "endpoints/get_endpoint", parameters)
    obj = res.json()
    if obj.get("reply", {}).get("total_count", 0) == 0:
        click.echo("No match")
        sys.exit(404)
    return obj["reply"]["endpoints"]


def do_get_triage_presets(ctx: click.Context):
    params = dict(request_data={})
    res = do_requests_post(ctx, "get_triage_presets", params)
    obj = res.json()
    return obj.get("reply", {}).get("triage_presets", [])


def do_initiate_triage(ctx: click.Context, agent_ids: List[str], collector_uuid: str):
    request_data = {"agent_ids": agent_ids}
    if collector_uuid:
        request_data["collector_uuid"] = collector_uuid
    params = dict(request_data=request_data)
    res = do_requests_post(ctx, "triage_endpoint", params)
    obj = res.json()
    return obj.get("reply", {})


def do_action_status(ctx: click.Context, group_action_id: int):
    params = dict(request_data=dict(group_action_id=group_action_id))
    res = do_requests_post(ctx, "actions/get_action_status", params)
    obj = res.json()
    return obj.get("reply", {}).get("data", {})


@triage.command("presets")
@click.pass_context
def triage_presets(ctx: click.Context):
    presets = _run_with_xdr_client(ctx, lambda client: client.triage.presets())
    for preset in presets:
        click.echo(json.dumps(preset))


@triage.command("new")
@click.argument("hostname")
@click.option(
    "--collector-uuid",
    "-c",
    help="Triage configuration UUID (optional)",
    type=str,
    default="",
)
@click.pass_context
def triage_new(ctx: click.Context, hostname: str, collector_uuid: str):
    try:
        reply = _run_with_xdr_client(
            ctx,
            lambda client: client.triage.start_by_hostname(hostname, collector_uuid=collector_uuid or None),
        )
    except click.ClickException as exc:
        if "No matching agent IDs found" in str(exc):
            click.echo("No matching agent IDs found for hostname")
            sys.exit(4)
        if "No endpoint matched hostname" in str(exc):
            click.echo("No match")
            sys.exit(404)
        raise
    click.echo(json.dumps(reply))


@triage.command("status")
@click.argument("group_action_id", type=int, nargs=-1)
@click.pass_context
def triage_status(ctx: click.Context, group_action_id: Tuple[int, ...]):
    if not group_action_id:
        click.echo("Provide one or more action IDs")
        sys.exit(2)
    payloads = _run_with_xdr_client(ctx, lambda client: client.triage.statuses(list(group_action_id)))
    for payload in payloads:
        click.echo(json.dumps(payload))


@endpoint.command("who-has")
@click.argument("ip")
@click.option("--raw", is_flag=True, help="Display the whole endpoint object")
@click.pass_context
def endpoint_who_has(ctx: click.Context, ip: str, raw: bool = False):
    """Resolve an IP address to a hostname"""
    try:
        endpoints = _run_with_xdr_client(ctx, lambda client: client.endpoints.who_has(ip))
    except click.ClickException as exc:
        if "No endpoint matched IP" in str(exc):
            click.echo("No match")
            sys.exit(404)
        raise
    if not endpoints:
        click.echo("No match")
        sys.exit(404)
    for endpoint in endpoints:
        if raw:
            click.echo(json.dumps(endpoint, indent=4))
        else:
            click.echo(endpoint["endpoint_name"])


@cli.group
def config():
    """Configure xdr-cli"""
    pass


@config.group
def tenant():
    """Add, update, or remove an XDR tenant"""
    pass


@tenant.command
@click.argument("name")
def add(name: str):
    tenants = {}
    if os.path.exists(AUTH_FILE):
        tenants = json.load(open(AUTH_FILE))
    else:
        click.confirm(
            f"Authentication file {AUTH_FILE} does not exist, should I create it?",
            abort=True,
        )
    if name in tenants:
        click.confirm(
            f"This tenant {name} already exists, should I update it?", abort=True
        )
    base_url = click.prompt("Base URL (without https://)", type=str)
    click.echo(
        f"Create an Advanced API key manually on https://{base_url}/configuration/api-keys then:\n"
    )
    api_key_hash = click.prompt("API Key Hash", type=str)
    api_key_id = click.prompt("API Key ID", type=int)
    tenants[name] = dict(
        api_key_id=api_key_id, api_key_hash=api_key_hash, base_url=base_url
    )
    json.dump(tenants, open(AUTH_FILE, "w"))


@tenant.command("list")
def list_tenants():
    try:
        tenants = json.load(open(AUTH_FILE))
        for name in tenants:
            click.echo(name)
    except:
        pass


@tenant.command
@click.argument("name")
def remove(name: str):
    tenants = json.load(open(AUTH_FILE))
    del tenants[name]
    json.dump(tenants, open(AUTH_FILE, "w"))


@cli.group()
def slack():
    """Slack bot integration"""
    pass


@slack.command("serve")
@click.option(
    "--auth-file",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
    default=pathlib.Path(AUTH_FILE),
    show_default=True,
    help="Path to xdr-cli tenant auth file",
)
@click.option(
    "--signing-secret",
    envvar="SLACK_SIGNING_SECRET",
    default="",
    help="Slack app signing secret (or set SLACK_SIGNING_SECRET)",
)
@click.option(
    "--bot-token",
    envvar="SLACK_BOT_TOKEN",
    default="",
    help="Slack bot token xoxb-... (or set SLACK_BOT_TOKEN)",
)
@click.option(
    "--app-token",
    envvar="SLACK_APP_TOKEN",
    default="",
    help="Slack app-level token xapp-... for Socket Mode (or set SLACK_APP_TOKEN)",
)
@click.option(
    "--default-tenant",
    envvar="XDR_SLACK_DEFAULT_TENANT",
    default="",
    help="Default tenant alias to use when a user omits --tenant",
)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="XDR API timeout in seconds for Slack-triggered requests",
)
def slack_serve(
    auth_file: pathlib.Path,
    signing_secret: str,
    bot_token: str,
    app_token: str,
    default_tenant: str,
    timeout: float,
):
    """Run the Slack /xdr bot in Socket Mode."""
    if not signing_secret:
        raise click.ClickException("Missing Slack signing secret (--signing-secret or SLACK_SIGNING_SECRET)")
    if not bot_token:
        raise click.ClickException("Missing Slack bot token (--bot-token or SLACK_BOT_TOKEN)")
    if not app_token:
        raise click.ClickException("Missing Slack app token (--app-token or SLACK_APP_TOKEN)")

    from .models import XdrClientOptions
    from .slack import create_slack_app
    from .slack.tenant_store import InMemoryTenantStore

    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    except Exception as exc:  # pragma: no cover - optional dependency
        raise click.ClickException(
            "slack-bolt Socket Mode support is unavailable. Install xdr-cli[slack]."
        ) from exc

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


def _safe_extract_zip(zip_path: pathlib.Path, output_dir: pathlib.Path, password: bytes):
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            destination = (output_dir / name).resolve()
            if os.path.commonpath([str(output_dir.resolve()), str(destination)]) != str(
                output_dir.resolve()
            ):
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
    "--output",
    "-o",
    type=click.Path(file_okay=False, dir_okay=True, path_type=pathlib.Path),
    required=True,
    help="Directory where endpoint files will be stored",
)
@click.argument("path", nargs=-1)
@click.pass_context
def get_file(
    ctx: click.Context,
    endpoint_id: str,
    path: List[str],
    output: pathlib.Path,
    hostname,
):
    """Retrieve a file from an endpoint and extract the returned archives."""
    content = _run_with_xdr_client(
        ctx,
        lambda client: client.files.retrieve(
            endpoint_id=endpoint_id,
            hostname=hostname,
            paths=list(path),
        ),
    )
    _extract_retrieved_file_zip(content, output)


def get_action_status(ctx: click.Context, action_id, endpoint_id) -> str:
    # PENDING IN_PROGRESS CANCELLED PENDING_ABORT ABORTED EXPIRED
    # COMPLETED_PARTIAL COMPLETED_SUCCESSFULLY FAILED TIMEOUT
    params = dict(request_data=dict(group_action_id=action_id))
    res = do_requests_post(ctx, "actions/get_action_status", params)
    res = res.json()
    return res.get("reply", {}).get("data", {}).get(endpoint_id, "")


def retrieve_file(ctx: click.Context, output, action_id, endpoint_id):
    params = {"request_data": {"group_action_id": action_id}}
    res = do_requests_post(ctx, "actions/file_retrieval_details", params)
    ret = res.json()
    dl_url = ret.get("reply", {}).get("data", {}).get(endpoint_id)
    res = requests.post(
        dl_url,
        headers=generate_auth_headers(ctx.obj["api_key_id"], ctx.obj["api_key_hash"]),
    )
    output.write(res.content)
    output.close()


def open_editor():
    # ripped from https://stackoverflow.com/a/19377899
    with tempfile.NamedTemporaryFile() as tmpfile:
        subprocess.call([os.environ.get("EDITOR", None) or "vim", tmpfile.name])

        with open(tmpfile.name, "r") as f:
            data = f.read()
    return data


@cli.command
@click.option(
    "--begin-time", type=click.DateTime(), default=None, help="Begin of the timeframe"
)
@click.option(
    "--end-time", type=click.DateTime(), default=None, help="End of the timeframe"
)
@click.option("--output", type=click.Path(exists=False), default=None, help="Output")
@click.option("--excel", is_flag=True, help="Format output as Excel file")
@click.argument("xql_query", type=str, default="")
@click.pass_context
def xql(
    ctx: click.Context,
    xql_query: Optional[str],
    begin_time: str = "",
    end_time: str = "",
    excel: bool = False,
    output: str = "",
):
    """Execute an XQL query"""
    if excel and not output:
        logger.error(
            "You have selected Excel output but you did not specify a path where to write results (use --output!)"
        )
        sys.exit(3)

    if not xql_query:
        xql_query = open_editor()
    results = _run_with_xdr_client(
        ctx,
        lambda client: client.xql.run(
            query=xql_query,
            begin_time=begin_time,
            end_time=end_time,
        ),
    )
    if excel:
        write_xql_results_excel(results, output)
        return
    for r in results:
        click.echo(json.dumps(r))


if __name__ == "__main__":
    cli()
