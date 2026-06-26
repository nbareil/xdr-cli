import asyncio

import pytest

from xdr_cli.errors import XdrOperationTimeoutError, XdrValidationError
from xdr_cli.services.xql import XqlService


class XqlTransport:
    def __init__(self, results_replies):
        self._results_replies = list(results_replies)
        self.calls = []

    async def post_json(self, uri, params):
        self.calls.append(uri)
        if uri == "xql/start_xql_query":
            return {"reply": "query-123"}
        if uri == "xql/get_query_results":
            return self._results_replies.pop(0)
        raise AssertionError("unexpected uri %s" % uri)


class FakeClient:
    def __init__(self, transport):
        self.transport = transport


def run(coro):
    return asyncio.run(coro)


def _service(replies):
    return XqlService(FakeClient(XqlTransport(replies)))


def test_xql_polls_through_pending_to_success():
    service = _service(
        [
            {"reply": {"status": "PENDING"}},
            {"reply": {"status": "PENDING"}},
            {"reply": {"status": "SUCCESS", "number_of_results": 2, "results": {"data": [{"a": 1}, {"a": 2}]}}},
        ]
    )
    rows = run(service.run("dataset=x", poll_interval_seconds=0))
    assert rows == [{"a": 1}, {"a": 2}]


def test_xql_raises_on_non_pending_failure():
    service = _service([{"reply": {"status": "FAIL"}}])
    with pytest.raises(XdrValidationError):
        run(service.run("dataset=x", poll_interval_seconds=0))


def test_xql_times_out_when_stuck_pending():
    service = _service([{"reply": {"status": "PENDING"}} for _ in range(5)])
    with pytest.raises(XdrOperationTimeoutError):
        run(service.run("dataset=x", poll_interval_seconds=0, poll_attempts=3))


def test_xql_empty_query_rejected():
    service = _service([])
    with pytest.raises(XdrValidationError):
        run(service.run(""))
