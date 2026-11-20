import asyncio

import pytest
from click.testing import CliRunner

from xdr_cli.errors import XdrValidationError
from xdr_cli.services.scripts import ScriptsService
from xdr_cli.xdr_cli import cli


class FakeTransport:
    def __init__(self):
        self.calls = []

    async def post_json(self, uri, params):
        self.calls.append((uri, params))
        return {"reply": {"action_id": 123, "status": 1}}


class FakeEndpoints:
    def __init__(self, by_hostname):
        self.by_hostname = by_hostname

    async def get_by_hostname(self, hostname):
        return self.by_hostname[hostname]


class FakeClient:
    def __init__(self, by_hostname=None):
        self.transport = FakeTransport()
        self.endpoints = FakeEndpoints(by_hostname or {})


def run(coro):
    return asyncio.run(coro)


def endpoint_filter_value(client):
    params = client.transport.calls[0][1]
    return params["request_data"]["filters"][0]["value"]


def test_run_snippet_keeps_endpoint_id_targets():
    client = FakeClient()
    service = ScriptsService(client)

    reply = run(service.run_snippet("print('hi')", endpoint_ids=["id-1", "id-2"]))

    assert reply["action_id"] == 123
    assert client.transport.calls[0][0] == "scripts/run_snippet_code_script"
    assert endpoint_filter_value(client) == ["id-1", "id-2"]


def test_run_snippet_resolves_hostname_targets():
    client = FakeClient({"host1": [{"endpoint_id": "id-1"}]})
    service = ScriptsService(client)

    run(service.run_snippet("print('hi')", hostnames=["host1"]))

    assert endpoint_filter_value(client) == ["id-1"]


def test_run_snippet_combines_and_deduplicates_targets():
    client = FakeClient(
        {
            "host1": [{"endpoint_id": "id-1"}, {"endpoint_id": "id-2"}],
            "host2": [{"endpoint_id": "id-2"}, {"endpoint_id": "id-3"}],
        }
    )
    service = ScriptsService(client)

    run(
        service.run_snippet(
            "print('hi')",
            endpoint_ids=["id-0", "id-1"],
            hostnames=["host1", "host2"],
        )
    )

    assert endpoint_filter_value(client) == ["id-0", "id-1", "id-2", "id-3"]


def test_run_snippet_requires_a_resolved_target():
    client = FakeClient({"host1": [{"endpoint_name": "host1"}]})
    service = ScriptsService(client)

    with pytest.raises(XdrValidationError, match="endpoint_ids or hostnames must not be empty"):
        run(service.run_snippet("print('hi')", hostnames=["host1"]))


def test_script_run_help_includes_hostname_option():
    result = CliRunner().invoke(cli, ["script", "run", "--help"])

    assert result.exit_code == 0
    assert "--hostname TEXT" in result.output


def test_run_snippet_reports_rejected_script():
    client = FakeClient()

    async def rejected(uri, params):
        return {"reply": ['Contains import of unsupported module "paramiko"']}

    client.transport.post_json = rejected
    service = ScriptsService(client)
    with pytest.raises(XdrValidationError, match='rejected the script: .*"paramiko"'):
        run(service.run_snippet("import paramiko", endpoint_ids=["e1"]))
