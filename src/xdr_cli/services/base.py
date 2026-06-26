from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import XdrReadOnlyError


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

    async def _paginated_post(
        self, uri: str, params: Dict[str, Any], limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        pages = []
        request_data = params.setdefault("request_data", {})
        offset = -1
        total_count = 0
        fetched = 0
        while offset < total_count:
            obj = await self.transport.post_json(uri, params)
            pages.append(obj)
            reply = obj.get("reply", {})
            result_count = int(reply.get("result_count", 0))
            total_count = int(reply.get("total_count", 0))
            fetched += result_count
            offset += result_count
            request_data["search_from"] = offset
            request_data["search_to"] = offset + 100
            # Terminate when there is nothing to page through, or when a page
            # returns no rows (otherwise ``offset`` never advances and we loop
            # forever against ``total_count``).
            if total_count == 0 or result_count == 0:
                break
            if limit is not None and fetched >= limit:
                break
        return pages
