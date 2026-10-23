import asyncio

import httpx
import pytest

from xdr_cli.errors import XdrApiError, XdrTransportError
from xdr_cli.http import XdrTransport
from xdr_cli.models import XdrClientOptions, XdrCredentials, XdrTenant


def make_transport(responses, max_retries=4):
    """Transport whose HTTP layer replays ``responses`` (status codes,
    (status, headers, body) tuples or exceptions) and records sleeps."""
    queue = list(responses)
    seen = {"requests": 0, "sleeps": []}

    def handler(request):
        seen["requests"] += 1
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, headers, body = item if isinstance(item, tuple) else (item, {}, "{}")
        return httpx.Response(status, headers=headers, text=body)

    tenant = XdrTenant("tenant.example.com", XdrCredentials("1", "h"))
    transport = XdrTransport(tenant, XdrClientOptions(max_retries=max_retries))
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_sleep(delay):
        seen["sleeps"].append(delay)

    transport._sleep = fake_sleep
    return transport, seen


def post(transport, retry=True):
    async def go():
        try:
            return await transport.post_json("x/y", {}, retry=retry)
        finally:
            await transport.aclose()

    return asyncio.run(go())


def test_transient_5xx_retried_with_exponential_backoff():
    transport, seen = make_transport([503, 502, (200, {}, '{"reply": {"ok": 1}}')])
    assert post(transport) == {"reply": {"ok": 1}}
    assert seen["requests"] == 3
    assert seen["sleeps"] == [1.0, 2.0]


def test_retry_after_header_is_honoured():
    transport, seen = make_transport([(429, {"Retry-After": "7"}, ""), 200])
    post(transport)
    assert seen["sleeps"] == [7.0]


def test_network_errors_and_timeouts_are_retried():
    request = httpx.Request("POST", "https://x")
    transport, seen = make_transport([httpx.ReadTimeout("slow", request=request),
                                      httpx.ConnectError("down", request=request), 200])
    post(transport)
    assert seen["requests"] == 3


def test_gives_up_after_max_retries():
    transport, seen = make_transport([500] * 3, max_retries=2)
    with pytest.raises(XdrApiError):
        post(transport)
    assert seen["requests"] == 3
    assert seen["sleeps"] == [1.0, 2.0]


def test_deep_offset_500_is_not_retried():
    body = '{"reply": {"err_extra": "Resources exceeded during query execution"}}'
    transport, seen = make_transport([(500, {}, body)])
    with pytest.raises(XdrApiError):
        post(transport)
    assert seen["requests"] == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    transport, seen = make_transport([status])
    with pytest.raises(XdrApiError):
        post(transport)
    assert seen["requests"] == 1


def test_no_retry_unless_requested():
    # Mutating calls (isolate, close, script run) must never be re-sent.
    request = httpx.Request("POST", "https://x")
    transport, seen = make_transport([httpx.ReadTimeout("slow", request=request)])
    with pytest.raises(XdrTransportError):
        post(transport, retry=False)
    assert seen["requests"] == 1
