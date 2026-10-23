from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..errors import XdrReadOnlyError, XdrValidationError
from ..models import ListPage

PAGE_SIZE = 100
SORT_ORDERS = ("asc", "desc")


class BaseService:
    # Sort fields the list endpoint accepts (first is the default) and the
    # default order. Subclasses with a paginated list() override these.
    SORT_FIELDS: Tuple[str, ...] = ()
    DEFAULT_SORT_ORDER = "desc"

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

    async def _paginated_list(
        self,
        uri: str,
        params: Dict[str, Any],
        key: str,
        offset: int = 0,
        limit: Optional[int] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> ListPage:
        """Fetch ``reply[key]`` rows starting at ``offset``, at most ``limit``
        of them (all remaining rows when ``limit`` is None).

        ``search_from`` is inclusive and ``search_to`` exclusive; the API
        returns at most ``PAGE_SIZE`` rows per request. Results are always
        explicitly sorted so offsets stay consistent across calls.
        """
        sort_field = sort_field or self.SORT_FIELDS[0]
        sort_order = sort_order or self.DEFAULT_SORT_ORDER
        if sort_field not in self.SORT_FIELDS:
            raise XdrValidationError(
                "Cannot sort by %r; choose one of: %s" % (sort_field, ", ".join(self.SORT_FIELDS))
            )
        if sort_order not in SORT_ORDERS:
            raise XdrValidationError("Sort order must be 'asc' or 'desc', not %r" % sort_order)
        request_data = params.setdefault("request_data", {})
        request_data["sort"] = {"field": sort_field, "keyword": sort_order}
        items: List[Dict[str, Any]] = []
        position = offset
        total_count = 0
        while limit is None or len(items) < limit:
            size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(items))
            request_data["search_from"] = position
            request_data["search_to"] = position + size
            reply = (await self.transport.post_json(uri, params)).get("reply", {})
            rows = reply.get(key, [])
            total_count = int(reply.get("total_count", 0))
            items.extend(rows)
            position += len(rows)
            # A short or empty page means the window is exhausted; stopping on
            # an empty page also guards against looping forever.
            if len(rows) < size or position >= total_count:
                break
        if limit is not None and len(items) > limit:
            items = items[:limit]
            position = offset + limit
        next_offset = position if position < total_count else None
        return ListPage(
            items=items,
            offset=offset,
            total_count=total_count,
            next_offset=next_offset,
            sort_field=sort_field,
            sort_order=sort_order,
        )
