import asyncio

import pytest

from xdr_cli.errors import XdrReadOnlyError, XdrValidationError
from xdr_cli.models import XdrClientOptions
from xdr_cli.services.actions import ResponseActionsService


class FakeTransport:
    def __init__(self, read_only=False):
        self.calls = []
        self.options = XdrClientOptions(read_only=read_only)

    async def post_json(self, uri, params):
        self.calls.append((uri, params))
        return {"reply": {"action_id": 99}}


class FakeEndpoints:
    def __init__(self, by_hostname):
        self.by_hostname = by_hostname

    async def get_by_hostname(self, hostname):
        return self.by_hostname[hostname]


class FakeClient:
    def __init__(self, by_hostname=None, read_only=False):
        self.transport = FakeTransport(read_only=read_only)
        self.endpoints = FakeEndpoints(by_hostname or {})


def run(coro):
    return asyncio.run(coro)


def test_isolate_builds_endpoint_filter():
    client = FakeClient()
    service = ResponseActionsService(client)
    reply = run(service.isolate(endpoint_id="abc"))
    uri, params = client.transport.calls[0]
    assert uri == "endpoints/isolate"
    assert params["request_data"]["filters"][0]["value"] == ["abc"]
    assert reply["action_id"] == 99


def test_isolate_resolves_hostname():
    client = FakeClient({"host1": [{"endpoint_id": "id-1"}]})
    service = ResponseActionsService(client)
    run(service.isolate(hostname="host1"))
    _, params = client.transport.calls[0]
    assert params["request_data"]["filters"][0]["value"] == ["id-1"]


def test_scan_all_requires_platform():
    service = ResponseActionsService(FakeClient())
    with pytest.raises(XdrValidationError):
        run(service.scan(all_endpoints=True))


def test_quarantine_payload():
    client = FakeClient()
    service = ResponseActionsService(client)
    run(service.quarantine(file_path="C:/evil.exe", file_hash="deadbeef", endpoint_id="abc"))
    uri, params = client.transport.calls[0]
    assert uri == "endpoints/quarantine"
    assert params["request_data"]["file_path"] == "C:/evil.exe"
    assert params["request_data"]["file_hash"] == "deadbeef"


def test_readonly_blocks_mutation():
    client = FakeClient(read_only=True)
    service = ResponseActionsService(client)
    with pytest.raises(XdrReadOnlyError):
        run(service.isolate(endpoint_id="abc"))
    assert client.transport.calls == []  # no network call attempted


def test_missing_target_raises():
    service = ResponseActionsService(FakeClient())
    with pytest.raises(XdrValidationError):
        run(service.isolate())


def test_poll_until_done_returns_final_status():
    client = FakeClient()

    statuses = [
        {"reply": {"data": {"abc": "PENDING"}}},
        {"reply": {"data": {"abc": "COMPLETED_SUCCESSFULLY"}}},
    ]

    async def fake_post(uri, params):
        return statuses.pop(0)

    client.transport.post_json = fake_post
    service = ResponseActionsService(client)
    data = run(service.poll_until_done(1, endpoint_id="abc", poll_interval_seconds=0))
    assert data == {"abc": "COMPLETED_SUCCESSFULLY"}
