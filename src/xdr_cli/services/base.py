from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..errors import XdrReadOnlyError, XdrValidationError
from ..models import ListPage

PAGE_SIZE = 100
SORT_ORDERS = ("asc", "desc")


class BaseService:
    def __init__(self, client):
        self.client = client
        self.transport = client.transport

    def _ensure_writable(self, operation: str) -> None:
        """Block mutating operations when the client is in read-only mode."""
        options = getattr(self.transport, "options", None)
        if getattr(options, "read_only", False):
            raise XdrReadOnlyError(
                "Refusing to perform '%s': client is in read-only mode" % operation
            )


@dataclass(frozen=True)
class _Position:
    """Where the next request starts.

    ``after`` is the sort value of the last row returned (None until a page
    has moved past the starting offset) and ``skip`` counts the rows at the
    start of the ``after``-filtered result set that were already returned.
    """

    sort_field: str
    sort_order: str
    after: Optional[int]
    skip: int

    def encode(self) -> str:
        raw = json.dumps({"f": self.sort_field, "o": self.sort_order, "a": self.after, "s": self.skip},
                         separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> "_Position":
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            obj = json.loads(raw)
            return cls(str(obj["f"]), str(obj["o"]), obj["a"], int(obj["s"]))
        except (binascii.Error, ValueError, KeyError, TypeError):
            raise XdrValidationError("Invalid cursor %r" % cursor)


class ListableService(BaseService):
    """A service whose list endpoint takes filters, sort and search_from/to.

    Pages are walked by keyset where possible: when sorting by a time field,
    each request filters on "at or after the last value seen" (gte/lte) and
    restarts near offset 0, so search_from never grows. XDR fails deep
    offsets (HTTP 500, BigQuery out of memory past ~10k rows), so keyset is
    what makes very large listings possible. Other sort fields fall back to
    plain offsets.
    """

    LIST_URI = ""
    LIST_KEY = ""
    # Sort fields the list endpoint honours; the first is the default.
    SORT_FIELDS: Tuple[str, ...] = ()
    DEFAULT_SORT_ORDER = "desc"
    # Sort field -> row key holding its value, for fields that also accept
    # gte/lte filters. Only these get keyset paging.
    KEYSET_FIELDS: Dict[str, str] = {}

    def _list_filters(self, **filters: Any) -> List[Dict[str, Any]]:
        if filters:
            raise XdrValidationError("Unsupported filters: %s" % ", ".join(sorted(filters)))
        return []

    def _start(self, offset: int, cursor: Optional[str], sort_field: Optional[str],
               sort_order: Optional[str]) -> _Position:
        if cursor:
            if offset or sort_field or sort_order:
                raise XdrValidationError("A cursor already fixes offset and sort; pass it alone")
            position = _Position.decode(cursor)
        else:
            position = _Position(sort_field or self.SORT_FIELDS[0],
                                 sort_order or self.DEFAULT_SORT_ORDER, None, offset)
        if position.sort_field not in self.SORT_FIELDS:
            raise XdrValidationError("Cannot sort by %r; choose one of: %s"
                                     % (position.sort_field, ", ".join(self.SORT_FIELDS)))
        if position.sort_order not in SORT_ORDERS:
            raise XdrValidationError("Sort order must be 'asc' or 'desc', not %r" % position.sort_order)
        if position.skip < 0:
            raise XdrValidationError("offset must be >= 0")
        if position.after is not None and position.sort_field not in self.KEYSET_FIELDS:
            raise XdrValidationError("Invalid cursor for sort field %r" % position.sort_field)
        return position

    async def iter_pages(
        self,
        offset: int = 0,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
        **filters: Any,
    ) -> AsyncIterator[ListPage]:
        """Yield one ListPage per API request, so callers can stream output.

        Each page's ``next_cursor`` resumes right after it (None once the
        listing is complete). ``total_count`` is the API's count for the
        starting request: all matching rows for a fresh listing, the rows
        from the cursor's position onward when resuming.
        """
        if limit is not None and limit < 1:
            raise XdrValidationError("limit must be >= 1")
        position = self._start(offset, cursor, sort_field, sort_order)
        base_filters = self._list_filters(**filters)
        value_key = self.KEYSET_FIELDS.get(position.sort_field)
        operator = "gte" if position.sort_order == "asc" else "lte"
        total_count: Optional[int] = None
        returned = 0
        while True:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - returned)
            request_filters = list(base_filters)
            if position.after is not None:
                request_filters.append(
                    {"field": position.sort_field, "operator": operator, "value": position.after}
                )
            params = {
                "request_data": {
                    "filters": request_filters,
                    "sort": {"field": position.sort_field, "keyword": position.sort_order},
                    "search_from": position.skip,
                    "search_to": position.skip + size,
                }
            }
            obj = await self.transport.post_json(self.LIST_URI, params, retry=True)
            reply = obj.get("reply", {})
            rows = reply.get(self.LIST_KEY, [])
            window_total = int(reply.get("total_count", 0))
            if total_count is None:
                total_count = window_total
            consumed = rows[:size]
            returned += len(consumed)
            # A short page alone doesn't mean the end (the server may cap
            # pages lower); an empty page always does, so this can't spin.
            exhausted = len(consumed) == len(rows) and (
                not rows or position.skip + len(rows) >= window_total
            )
            position = _advance(position, consumed, value_key)
            yield ListPage(
                items=consumed,
                total_count=total_count,
                next_cursor=None if exhausted else position.encode(),
                sort_field=position.sort_field,
                sort_order=position.sort_order,
            )
            if exhausted or (limit is not None and returned >= limit):
                return

    async def list(self, **kwargs: Any) -> ListPage:
        """Collect iter_pages() into a single ListPage."""
        items: List[Dict[str, Any]] = []
        page = None
        async for page in self.iter_pages(**kwargs):
            items.extend(page.items)
        return ListPage(items=items, total_count=page.total_count, next_cursor=page.next_cursor,
                        sort_field=page.sort_field, sort_order=page.sort_order)


def _advance(position: _Position, rows: List[Dict[str, Any]], value_key: Optional[str]) -> _Position:
    """Move past ``rows``, preferring a keyset position over a bigger offset."""
    if not rows:
        return position
    offset_step = _Position(position.sort_field, position.sort_order, position.after,
                            position.skip + len(rows))
    if value_key is None:
        return offset_step
    last = rows[-1].get(value_key)
    if last is None:
        return offset_step
    trailing = 0
    for row in reversed(rows):
        if row.get(value_key) != last:
            break
        trailing += 1
    # Still inside the same run of equal values: step past it positionally.
    # With no keyset yet and an all-equal page, rows before it (skipped by
    # the starting offset) may share the value, so trailing would undercount.
    if last == position.after or (position.after is None and trailing == len(rows)):
        return offset_step
    return _Position(position.sort_field, position.sort_order, last, trailing)
