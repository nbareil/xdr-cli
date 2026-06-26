import asyncio

from xdr_cli.services.base import BaseService


class ScriptedTransport:
    """Returns queued replies and records every request payload."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def post_json(self, uri, params):
        # Copy params: the service mutates request_data between calls.
        self.calls.append({k: dict(v) if isinstance(v, dict) else v for k, v in params.items()})
        return self._replies.pop(0)


class FakeClient:
    def __init__(self, transport):
        self.transport = transport


def run(coro):
    return asyncio.run(coro)


def test_pagination_terminates_on_zero_result_count():
    # total_count stays positive but a page returns no rows: must not loop forever.
    transport = ScriptedTransport(
        [
            {"reply": {"result_count": 0, "total_count": 5}},
        ]
    )
    service = BaseService(FakeClient(transport))
    pages = run(service._paginated_post("x/y", {"request_data": {}}))
    assert len(pages) == 1
    assert len(transport.calls) == 1


def test_pagination_walks_until_total_reached():
    # The service's offset arithmetic lands one short of total after the last
    # data page, so it issues a final request that returns an empty page; the
    # result_count==0 guard must terminate there instead of looping forever.
    transport = ScriptedTransport(
        [
            {"reply": {"result_count": 100, "total_count": 250}},
            {"reply": {"result_count": 100, "total_count": 250}},
            {"reply": {"result_count": 50, "total_count": 250}},
            {"reply": {"result_count": 0, "total_count": 250}},
        ]
    )
    service = BaseService(FakeClient(transport))
    pages = run(service._paginated_post("x/y", {"request_data": {}}))
    assert len(pages) == 4  # 3 data pages + terminal empty page


def test_pagination_respects_limit():
    transport = ScriptedTransport(
        [
            {"reply": {"result_count": 100, "total_count": 1000}},
            {"reply": {"result_count": 100, "total_count": 1000}},
        ]
    )
    service = BaseService(FakeClient(transport))
    pages = run(service._paginated_post("x/y", {"request_data": {}}, limit=150))
    assert len(pages) == 2  # stops once fetched (200) >= limit (150)


def test_pagination_empty_dataset_breaks_immediately():
    transport = ScriptedTransport([{"reply": {"result_count": 0, "total_count": 0}}])
    service = BaseService(FakeClient(transport))
    pages = run(service._paginated_post("x/y", {"request_data": {}}))
    assert len(pages) == 1
