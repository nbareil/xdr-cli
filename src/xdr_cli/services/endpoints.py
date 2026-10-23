from __future__ import annotations

from typing import Dict, List, Optional

from ..errors import XdrNotFoundError
from ..models import ListPage
from .base import BaseService


class EndpointsService(BaseService):
    # last_seen is accepted by get_endpoint but does not actually order the
    # results, and endpoint_name is rejected, so neither is offered.
    SORT_FIELDS = ("endpoint_id", "first_seen")
    DEFAULT_SORT_ORDER = "asc"

    async def list(
        self,
        platform: Optional[str] = None,
        offset: int = 0,
        limit: Optional[int] = None,
        sort_field: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> ListPage:
        params = {"request_data": {"filters": []}}
        if platform:
            params["request_data"]["filters"].append(
                {"field": "platform", "operator": "in", "value": [platform]}
            )
        return await self._paginated_list(
            "endpoints/get_endpoint",
            params,
            "endpoints",
            offset=offset,
            limit=limit,
            sort_field=sort_field,
            sort_order=sort_order,
        )

    async def get_by_hostname(self, hostname: str) -> List[Dict]:
        parameters = {
            "request_data": {
                "filters": [
                    {"field": "hostname", "operator": "in", "value": [hostname]}
                ]
            }
        }
        obj = await self.transport.post_json("endpoints/get_endpoint", parameters)
        if obj.get("reply", {}).get("total_count", 0) == 0:
            raise XdrNotFoundError("No endpoint matched hostname %s" % hostname, resource="endpoint")
        return obj.get("reply", {}).get("endpoints", [])

    async def who_has(self, ip: str) -> List[Dict]:
        parameters = {
            "request_data": {
                "filters": [
                    {"field": "ip_list", "operator": "in", "value": [ip]},
                ]
            }
        }
        obj = await self.transport.post_json("endpoints/get_endpoint", parameters)
        if obj.get("reply", {}).get("total_count", 0) == 0:
            raise XdrNotFoundError("No endpoint matched IP %s" % ip, resource="endpoint")
        return obj.get("reply", {}).get("endpoints", [])
