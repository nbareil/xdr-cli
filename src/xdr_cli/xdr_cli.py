import base64
import io
import tempfile
import subprocess
import os
import hashlib
import gzip
import json
import logging
import time
import sys
import string
import secrets
from typing import Optional

import requests
import click

from typing import List
from datetime import datetime, timezone

# API Documentation: https://cortex-panw.stoplight.io/docs/cortex-xdr/branches/main/axpm6b98x4p18-cortex-xdr-api-overview

AUTH_FILE = os.path.join(os.environ.get("HOME"), ".xdr-cli-auth.json")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)


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
        logging.basicConfig(level=logging.INFO)
    if debug:
        logging.basicConfig(level=logging.DEBUG)
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
    if not (all([ctx.obj["base_url"], ctx.obj["api_key_id"], ctx.obj["api_key_hash"]])):
        click.echo("Missing authentication bits")


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
    ctx: click.Context, uri: str, parameters: dict = {}, headers: dict = {}
) -> requests.Response:
    headers.update(
        **generate_auth_headers(ctx.obj["api_key_id"], ctx.obj["api_key_hash"])
    )
    url = f"https://api-{ctx.obj.get('base_url')}/public_api/v1/{uri}"
    res = requests.post(url, headers=headers, json=parameters)
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


def do_script_run(ctx, name: str, script, endpoint_id: list[str], timeout: int):
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
def run(ctx, name: str, script: io.FileIO, endpoint_id: list[str], timeout: int):
    return do_script_run(ctx, name, script, endpoint_id, timeout)


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
    ctx, yara_ruleset: io.FileIO, all: bool, platform: list[str], endpoint: list[str]
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
    py_script = io.StringIO(build_py_script_from_yara(yara_ruleset))
    if all:
        endpoint = []
        for p in platform:
            endpoint += (x["endpoint_id"] for x in do_endpoint_list(ctx, p))

    all_action_id = []
    endpoint_id_list = []
    for i, endpoint_id in enumerate(endpoint):
        endpoint_id_list.append(endpoint_id)
        if i and (i % 1000 == 0):
            ret = do_script_run(ctx, "yara_memory_scan", py_script, endpoint_id_list, 600)
            all_action_id.append(ret.get("action_id"))
            endpoint_id_list = []
    if endpoint_id_list:
        ret = do_script_run(ctx, "yara_memory_scan", py_script, endpoint_id_list, 600)
        all_action_id.append(ret.get("action_id"))
    click.echo(json.dumps(dict(actions_id=all_action_id)))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def results(ctx, action_id):
    params = dict(request_data={"action_id": action_id})
    res = do_requests_post(ctx, "scripts/get_script_execution_results", params)
    obj = res.json()
    click.echo(json.dumps(obj["reply"]))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
def status(ctx, action_id):
    params = dict(request_data={"action_id": action_id})
    res = do_requests_post(ctx, "scripts/get_script_execution_status", params)
    obj = res.json()
    click.echo(json.dumps(obj["reply"]))


@script.command
@click.pass_context
@click.argument("action_id", type=int)
@click.argument("endpoint_id", type=str)
def result_files(ctx, endpoint_id: str, action_id: str):
    params = dict(request_data={"action_id": action_id, "endpoint_id": endpoint_id})
    res = do_requests_post(ctx, "scripts/get_script_execution_results_files", params)
    obj = res.json()
    click.echo(json.dumps(obj["reply"]))


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
    for incident in do_list_incidents(ctx):
        click.echo(json.dumps(incident))


@incident.command("get")
@click.argument("incident_id")
@click.pass_context
def get_incident(ctx: click.Context, incident_id: str) -> None:
    params = dict(request_data=dict(incident_id=incident_id))
    res = do_requests_post(ctx, "incidents/get_incident_extra_data", params)
    obj = res.json()
    click.echo(json.dumps(obj["reply"]))


@incident.command("close")
@click.argument("incident_id")
@click.pass_context
def close_incident(ctx: click.Context, incident_id: str):
    params = dict(
        request_data=dict(incident_id_=incident_id), update_data=dict(status="resolved")
    )
    res = do_requests_post(ctx, "incidents/update_incidents", params)
    click.echo(res)


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
    for alert in do_alert_list(ctx):
        click.echo(json.dumps(alert))


@alert.command("close")
@click.argument("alert_id", type=int, nargs=-1)
@click.pass_context
def close_alert(ctx: click.Context, alert_id: List[int]):
    params = dict(
        request_data=dict(
            alert_id_list=alert_id, update_data=dict(status="resolved_other")
        )
    )
    res = do_requests_post(ctx, "alerts/update_alerts", params)


@cli.group()
@click.pass_context
def endpoint(ctx: click.Context):
    pass


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
    endpoints = do_endpoint_list(ctx, platform)
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
    endpoints = do_endpoint_get(ctx, hostname)
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


@endpoint.command("who-has")
@click.argument("ip")
@click.option("--raw", is_flag=True, help="Display the whole endpoint object")
@click.pass_context
def endpoint_who_has(ctx: click.Context, ip: str, raw: bool = False):
    """Resolve an IP address to a hostname"""
    # click.echo(hostname)
    parameters = {
        "request_data": {
            "filters": [
                {"field": "ip_list", "operator": "in", "value": [ip]},
            ],
        }
    }
    res = do_requests_post(ctx, "endpoints/get_endpoint", parameters)
    obj = res.json()
    if obj.get("reply", {}).get("total_count", 0) == 0:
        click.echo("No match")
        sys.exit(404)
    for endpoint in obj["reply"]["endpoints"]:
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


@tenant.command
def list():
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


@cli.command("get-file")
@click.option("--endpoint-id", type=str, help="Endpoint ID")
@click.option("--hostname", type=str, help="Endpoint hostname")
@click.option("--output", "-o", type=click.File("wb"), required=True)
@click.argument("path", nargs=-1)
@click.pass_context
def get_file(ctx: click.Context, endpoint_id: str, path: List[str], output, hostname):
    """Retrieve a file from an endpoint."""
    if hostname:
        endpoints = do_endpoint_get(ctx, hostname)
        endpoint_id = endpoints[0]["endpoint_id"]
    params = {
        "request_data": {
            "filters": [
                {"field": "endpoint_id_list", "operator": "in", "value": [endpoint_id]}
            ],
            "files": {
                "windows": path,
                "macos": path,
                "linux": path,
            },
        }
    }
    res = do_requests_post(ctx, "endpoints/file_retrieval", params)
    ret = res.json()
    action_id = ret.get("reply", {}).get("action_id")
    if not action_id:
        click.echo("There was an error retrieving the file")
        sys.exit(1)

    final_states = [
        "CANCELLED",
        "ABORTED",
        "EXPIRED",
        "COMPLETED_PARTIAL",
        "COMPLETED_SUCCESSFULLY",
        "FAILED",
        "TIMEOUT",
    ]
    while True:
        status = get_action_status(ctx, action_id, endpoint_id)
        if status in final_states:
            break
        time.sleep(5)
    if not status.startswith("COMPLETED"):
        click.echo(f"File retrieval ended in state {status}")
        sys.exit(2)
    retrieve_file(ctx, output, action_id, endpoint_id)


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

    params = {
        "request_data": {
            "query": xql_query,
        }
    }

    if begin_time and end_time:
        params["request_data"]["timeframe"] = {
            "from": begin_time,
            "to": end_time,
        }
    res = do_requests_post(ctx, "xql/start_xql_query", params)
    res = res.json()
    query_id = res.get("reply")
    logger.debug(query_id)
    params = {
        "request_data": {
            "query_id": query_id,
            "format": "json",
            "pending_flag": False,  # Be synchronous (i.e. blocking)
        }
    }
    res = do_requests_post(ctx, "xql/get_query_results", params)
    resp = res.json()["reply"]
    if resp.get("status") != "SUCCESS":
        logger.error("xql/get_query_results failed: " + res.text)
        sys.exit(1)
    if resp.get("number_of_results", 0) <= 1000:
        results = [
            json.dumps(entry) for entry in resp.get("results", {}).get("data", [])
        ]
    else:
        logger.debug("Need to switch to the stream version")
        params = {
            "request_data": {
                "stream_id": resp.get("results").get("stream_id"),
                "is_gzip_compressed": False,
            }
        }
        logger.debug(params)

        headers = generate_auth_headers(ctx.obj["api_key_id"], ctx.obj["api_key_hash"])
        # headers["Accept-Encoding"] = "gzip"
        uri = "xql/get_query_results_stream"
        url = f"https://api-{ctx.obj.get('base_url')}/public_api/v1/{uri}"
        with requests.post(url, json=params, headers=headers, stream=True) as r:
            if not res.ok:
                logger.error("xql/get_query_results_stream failed: " + res.text)
                sys.exit(2)
            results = [chunk.decode("utf-8") for chunk in r.iter_lines()]

    if excel:
        import xlsxwriter

        workbook = xlsxwriter.Workbook(output)
        worksheet = workbook.add_worksheet()
        results = map(json.loads, results)
        first = next(results)
        logger.debug(first)
        columns = first.keys()
        logger.debug(columns)
        for i, col in enumerate(columns):
            worksheet.write(0, i, col)
            worksheet.write(1, i, first.get(col, ""))
        row = 0
        for r in results:
            row += 1
            for i, col in enumerate(columns):
                worksheet.write(row, i, r.get(col, ""))
        workbook.close()
    else:
        for r in results:
            click.echo(r)


if __name__ == "__main__":
    cli()
