import asyncio

import pytest

from xdr_cli.errors import XdrApiError, XdrValidationError
from xdr_cli.services.alerts import AlertsService
from xdr_cli.services.base import ListableService
from xdr_cli.services.endpoints import EndpointsService
from xdr_cli.services.incidents import IncidentsService


class FakeTenant:
    """In-memory list endpoint honouring sort, gte/lte filters and
    search_from (inclusive) / search_to (exclusive), like XDR does.

    Ties are ordered by id, so the order is deterministic (as observed on a
    live tenant). ``max_offset`` mimics XDR's HTTP 500 on deep offsets.
    """

    def __init__(self, values, page_cap=100, max_offset=None):
        self.rows = [{"id": i, "ts": v, "rank": v % 3} for i, v in enumerate(values)]
        self.page_cap = page_cap
        self.max_offset = max_offset
        self.calls = []

    async def post_json(self, uri, params, retry=False):
        rd = params["request_data"]
        self.calls.append(rd)
        start, end = rd["search_from"], rd["search_to"]
        if self.max_offset is not None and start > self.max_offset:
            raise XdrApiError(uri, 500, "Internal Server Error", "Resources exceeded")
        rows = self.rows
        for f in rd["filters"]:
            if f["operator"] == "gte":
                rows = [r for r in rows if r[f["field"]] >= f["value"]]
            elif f["operator"] == "lte":
                rows = [r for r in rows if r[f["field"]] <= f["value"]]
        field, desc = rd["sort"]["field"], rd["sort"]["keyword"] == "desc"
        rows = sorted(rows, key=lambda r: (-r[field] if desc else r[field], r["id"]))
        end = min(end, start + self.page_cap)
        return {"reply": {"rows": rows[start:end], "total_count": len(rows)}}

    def expected(self, field="ts", desc=False):
        return [r["id"] for r in sorted(self.rows, key=lambda r: (-r[field] if desc else r[field], r["id"]))]


class ScriptedTransport:
    """Returns queued replies and records every request payload."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    async def post_json(self, uri, params, retry=False):
        self.calls.append(params["request_data"])
        return self._replies.pop(0)


class FakeClient:
    def __init__(self, transport):
        self.transport = transport


class RowsService(ListableService):
    LIST_URI = "x/y"
    LIST_KEY = "rows"
    SORT_FIELDS = ("ts", "rank")
    DEFAULT_SORT_ORDER = "asc"
    KEYSET_FIELDS = {"ts": "ts"}


def run(coro):
    return asyncio.run(coro)


def service(transport):
    return RowsService(FakeClient(transport))


def fetch(transport, **kwargs):
    return run(service(transport).list(**kwargs))


def walk(transport, limit, **kwargs):
    """Page through everything via next_cursor, ``limit`` rows per call."""
    ids, cursor, calls = [], None, 0
    while True:
        page = fetch(transport, limit=limit, cursor=cursor, **(kwargs if cursor is None else {}))
        ids.extend(r["id"] for r in page.items)
        cursor = page.next_cursor
        calls += 1
        assert calls < 10_000
        if cursor is None:
            return ids


# --- full listings ---------------------------------------------------------- #

def test_walks_all_rows_once_in_order():
    tenant = FakeTenant(range(250))
    page = fetch(tenant)
    assert [r["id"] for r in page.items] == list(range(250))
    assert page.total_count == 250
    assert page.next_cursor is None


def test_keyset_keeps_search_from_small():
    tenant = FakeTenant(range(5000))
    page = fetch(tenant)
    assert len(page.items) == 5000
    assert max(c["search_from"] for c in tenant.calls) < 100


def test_keyset_survives_deep_offset_limit():
    # The tenant 500s past offset 500; keyset paging never goes that deep.
    tenant = FakeTenant(range(3000), max_offset=500)
    assert [r["id"] for r in fetch(tenant).items] == list(range(3000))


def test_offset_only_sort_still_hits_deep_offset_limit():
    tenant = FakeTenant(range(3000), max_offset=500)
    with pytest.raises(XdrApiError):
        fetch(tenant, sort_field="rank")


@pytest.mark.parametrize("desc", [False, True])
def test_ties_larger_than_a_page_are_neither_lost_nor_repeated(desc):
    values = [1] * 30 + [5] * 250 + [7] * 3 + [9] * 120 + [12]
    tenant = FakeTenant(values)
    page = fetch(tenant, sort_order="desc" if desc else "asc")
    assert [r["id"] for r in page.items] == tenant.expected(desc=desc)


# --- limit / cursor chaining ------------------------------------------------ #

@pytest.mark.parametrize("limit", [1, 37, 100, 101])
@pytest.mark.parametrize("desc", [False, True])
def test_cursor_chain_covers_everything_once(limit, desc):
    values = [i // 4 for i in range(150)] + [100] * 130 + list(range(200, 260))
    tenant = FakeTenant(values)
    assert walk(tenant, limit, sort_order="desc" if desc else "asc") == tenant.expected(desc=desc)


def test_cursor_chain_with_offset_only_sort():
    tenant = FakeTenant(range(230))
    assert walk(tenant, 70, sort_field="rank") == tenant.expected(field="rank")


def test_offset_start_inside_a_run_of_ties():
    # First 150 rows share a value: starting at offset 50 must not re-emit
    # rows 0-49's peers or skip any of the run when resuming.
    values = [3] * 150 + list(range(10, 60))
    tenant = FakeTenant(values)
    first = fetch(tenant, offset=50, limit=60)
    ids = [r["id"] for r in first.items]
    cursor = first.next_cursor
    while cursor:
        page = fetch(tenant, cursor=cursor, limit=45)
        ids.extend(r["id"] for r in page.items)
        cursor = page.next_cursor
    assert ids == tenant.expected()[50:]


def test_limit_exactly_at_end_has_no_cursor():
    page = fetch(FakeTenant(range(100)), limit=100)
    assert len(page.items) == 100
    assert page.next_cursor is None


def test_offset_past_end_returns_empty_page():
    page = fetch(FakeTenant(range(10)), offset=50)
    assert page.items == []
    assert page.total_count == 10
    assert page.next_cursor is None


def test_iter_pages_yields_each_request():
    tenant = FakeTenant(range(250))

    async def collect():
        return [page async for page in service(tenant).iter_pages()]

    pages = run(collect())
    assert [len(p.items) for p in pages] == [100, 100, 50]
    assert pages[0].next_cursor and pages[1].next_cursor and pages[2].next_cursor is None


# --- termination guards ------------------------------------------------------ #

def test_short_pages_from_server_cap_keep_paging():
    tenant = FakeTenant(range(100), page_cap=30)
    page = fetch(tenant)
    assert [r["id"] for r in page.items] == list(range(100))
    assert page.next_cursor is None


def test_terminates_on_empty_page_despite_positive_total():
    transport = ScriptedTransport([{"reply": {"rows": [], "total_count": 5}}])
    assert fetch(transport).items == []
    assert len(transport.calls) == 1


def test_oversized_reply_is_trimmed_to_limit():
    rows = [{"id": i, "ts": i} for i in range(10)]
    transport = ScriptedTransport([{"reply": {"rows": rows, "total_count": 50}}])
    page = fetch(transport, limit=4)
    assert [r["id"] for r in page.items] == [0, 1, 2, 3]
    assert page.next_cursor is not None


# --- validation ---------------------------------------------------------------- #

@pytest.mark.parametrize(
    "kwargs",
    [
        {"sort_field": "bogus"},
        {"sort_order": "sideways"},
        {"offset": -1},
        {"limit": 0},
        {"cursor": "not-a-cursor!!"},
    ],
)
def test_invalid_arguments_rejected_before_any_request(kwargs):
    transport = ScriptedTransport([])
    with pytest.raises(XdrValidationError):
        fetch(transport, **kwargs)
    assert transport.calls == []


@pytest.mark.parametrize("extra", [{"offset": 5}, {"sort_field": "ts"}, {"sort_order": "asc"}])
def test_cursor_cannot_be_combined_with_offset_or_sort(extra):
    cursor = fetch(FakeTenant(range(10)), limit=3).next_cursor
    with pytest.raises(XdrValidationError):
        fetch(FakeTenant(range(10)), cursor=cursor, **extra)


def test_cursor_carries_sort():
    tenant = FakeTenant(range(10))
    page = fetch(tenant, limit=3, sort_order="desc")
    resumed = fetch(tenant, cursor=page.next_cursor)
    assert (resumed.sort_field, resumed.sort_order) == ("ts", "desc")
    assert [r["id"] for r in page.items + resumed.items] == list(range(9, -1, -1))


# --- per-service defaults ------------------------------------------------------ #

def empty_transport():
    return ScriptedTransport([{"reply": {"total_count": 0}}])


@pytest.mark.parametrize(
    "service_cls, kwargs, sort, filters",
    [
        (AlertsService, {}, {"field": "creation_time", "keyword": "desc"}, []),
        (IncidentsService, {}, {"field": "creation_time", "keyword": "desc"}, []),
        (EndpointsService, {"platform": "linux"}, {"field": "first_seen", "keyword": "asc"},
         [{"field": "platform", "operator": "in", "value": ["linux"]}]),
    ],
)
def test_service_defaults(service_cls, kwargs, sort, filters):
    # Defaults were picked against a live tenant: time fields that both sort
    # and accept gte/lte filters, so large listings use keyset paging.
    transport = empty_transport()
    run(service_cls(FakeClient(transport)).list(**kwargs))
    assert transport.calls[0]["sort"] == sort
    assert transport.calls[0]["filters"] == filters


def test_alert_keyset_filters_on_creation_time_using_detection_timestamp():
    rows = [{"alert_id": str(i), "detection_timestamp": 1000 - i} for i in range(100)]
    transport = ScriptedTransport([
        {"reply": {"alerts": rows, "total_count": 150}},
        {"reply": {"alerts": [], "total_count": 51}},
    ])
    run(AlertsService(FakeClient(transport)).list())
    assert transport.calls[1]["filters"] == [{"field": "creation_time", "operator": "lte", "value": 901}]
    assert transport.calls[1]["search_from"] == 1


def test_unknown_filter_rejected():
    with pytest.raises(XdrValidationError):
        run(AlertsService(FakeClient(empty_transport())).list(platform="linux"))
