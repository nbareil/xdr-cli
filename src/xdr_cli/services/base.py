from __future__ import annotations

from typing import Any, Dict, List


class BaseService:
    def __init__(self, client):
        self.client = client
        self.transport = client.transport

    async def _paginated_post(self, uri: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        pages = []
        request_data = params.setdefault("request_data", {})
        offset = -1
        total_count = 0
        while offset < total_count:
            obj = await self.transport.post_json(uri, params)
            pages.append(obj)
            reply = obj.get("reply", {})
            offset += int(reply.get("result_count", 0))
            total_count = int(reply.get("total_count", 0))
            request_data["search_from"] = offset
            request_data["search_to"] = offset + 100
            if total_count == 0:
                break
        return pages
