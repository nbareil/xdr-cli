import asyncio

import pytest

from xdr_cli.errors import XdrValidationError
from xdr_cli.services.alerts import AlertsService
from xdr_cli.services.base import BaseService
from xdr_cli.services.endpoints import EndpointsService
from xdr_cli.services.incidents import IncidentsService


class ScriptedTransport:
    """Returns queued replies and records every request payload."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def post_json(self, uri, params):
        # Copy params: the service mutates request_data between calls.
        self.calls.append({k: dict(v) if isinstance(v, dict) else v for k, v in params.items()})
        return self._replies.pop(0)

    def windows(self):
        return [(c["request_data"]["search_from"], c["request_data"]["search_to"]) for c in self.calls]


class FakeTenant:
    """Serves rows 0..total-1 honouring search_from (inclusive) / search_to (exclusive)."""

    def __init__(self, total, page_cap=100):
        self.total = total
        self.page_cap = page_cap
        self.calls = []

    async def post_json(self, uri, params):
        rd = params["request_data"]
        start, end = rd["search_from"], rd["search_to"]
        self.calls.append((start, end))
        end = min(end, start + self.page_cap, self.total)
        rows = [{"id": i} for i in range(start, max(start, end))]
        return {"reply": {"rows": rows, "result_count": len(rows), "total_count": self.total}}


class FakeClient:
    def __init__(self, transport):
        self.transport = transport


class SortableService(BaseService):
    SORT_FIELDS = ("id", "ts")


def run(coro):
    return asyncio.run(coro)


def fetch(transport, **kwargs):
    service = SortableService(FakeClient(transport))
    return run(service._paginated_list("x/y", {"request_data": {}}, "rows", **kwargs))


def test_walks_all_rows_without_duplicates():
    tenant = FakeTenant(total=250)
    page = fetch(tenant)
    assert [r["id"] for r in page.items] == list(range(250))
    assert tenant.calls == [(0, 100), (100, 200), (200, 300)]
    assert page.total_count == 250
    assert page.next_offset is None


def test_offset_and_limit_select_a_window():
    tenant = FakeTenant(total=1000)
    page = fetch(tenant, offset=150, limit=120)
    assert [r["id"] for r in page.items] == list(range(150, 270))
    assert tenant.calls == [(150, 250), (250, 270)]
    assert page.offset == 150
    assert page.next_offset == 270


def test_next_offset_chains_to_full_coverage():
    tenant = FakeTenant(total=95)
    seen, offset = [], 0
    while offset is not None:
        page = fetch(tenant, offset=offset, limit=40)
        seen.extend(r["id"] for r in page.items)
        offset = page.next_offset
    assert seen == list(range(95))


def test_limit_exactly_at_end_has_no_next_offset():
    page = fetch(FakeTenant(total=100), limit=100)
    assert len(page.items) == 100
    assert page.next_offset is None


def test_offset_past_end_returns_empty_page():
    page = fetch(FakeTenant(total=10), offset=50)
    assert page.items == []
    assert page.total_count == 10
    assert page.next_offset is None


def test_short_page_from_server_cap_keeps_paging():
    # Server returns fewer rows than requested (smaller cap): the short page ends
    # the call, but next_offset lets the caller resume without losing rows.
    tenant = FakeTenant(total=100, page_cap=30)
    page = fetch(tenant)
    assert [r["id"] for r in page.items] == list(range(30))
    assert page.next_offset == 30


def test_terminates_on_empty_page_despite_positive_total():
    transport = ScriptedTransport([{"reply": {"rows": [], "total_count": 5}}])
    page = fetch(transport)
    assert page.items == []
    assert len(transport.calls) == 1
    assert transport.windows() == [(0, 100)]


def test_empty_dataset_breaks_immediately():
    transport = ScriptedTransport([{"reply": {"rows": [], "total_count": 0}}])
    page = fetch(transport)
    assert page.items == []
    assert page.next_offset is None
    assert len(transport.calls) == 1


def test_oversized_reply_is_trimmed_to_limit():
    rows = [{"id": i} for i in range(10)]
    transport = ScriptedTransport([{"reply": {"rows": rows, "total_count": 50}}])
    page = fetch(transport, limit=4)
    assert [r["id"] for r in page.items] == [0, 1, 2, 3]
    assert page.next_offset == 4


def empty_transport(n=1):
    return ScriptedTransport([{"reply": {"rows": [], "total_count": 0}}] * n)


def test_default_sort_is_sent_on_every_request():
    tenant = FakeTenant(total=150)
    service = SortableService(FakeClient(tenant))
    seen = []
    original = tenant.post_json

    async def spy(uri, params):
        seen.append(dict(params["request_data"]["sort"]))
        return await original(uri, params)

    tenant.post_json = spy
    page = run(service._paginated_list("x/y", {"request_data": {}}, "rows"))
    assert seen == [{"field": "id", "keyword": "desc"}] * 2
    assert (page.sort_field, page.sort_order) == ("id", "desc")


def test_explicit_sort_is_forwarded():
    transport = empty_transport()
    page = fetch(transport, sort_field="ts", sort_order="asc")
    assert transport.calls[0]["request_data"]["sort"] == {"field": "ts", "keyword": "asc"}
    assert (page.sort_field, page.sort_order) == ("ts", "asc")


@pytest.mark.parametrize("kwargs", [{"sort_field": "bogus"}, {"sort_order": "sideways"}])
def test_invalid_sort_rejected_before_any_request(kwargs):
    transport = empty_transport()
    with pytest.raises(XdrValidationError):
        fetch(transport, **kwargs)
    assert transport.calls == []


@pytest.mark.parametrize(
    "service_cls, method_kwargs, expected",
    [
        (AlertsService, {}, {"field": "creation_time", "keyword": "desc"}),
        (IncidentsService, {}, {"field": "incident_id", "keyword": "desc"}),
        (EndpointsService, {"platform": "linux"}, {"field": "endpoint_id", "keyword": "asc"}),
    ],
)
def test_service_default_sorts(service_cls, method_kwargs, expected):
    # Defaults were picked against a live tenant: these are the fields each
    # endpoint actually honours (see the SORT_FIELDS comments).
    transport = empty_transport()
    run(service_cls(FakeClient(transport)).list(**method_kwargs))
    assert transport.calls[0]["request_data"]["sort"] == expected
