from __future__ import annotations

from typing import Dict, List, Optional

from ..errors import XdrNotFoundError
from .base import BaseService


class EndpointsService(BaseService):
    async def list(self, platform: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        params = {"request_data": {"filters": []}}
        if platform:
            params["request_data"]["filters"].append(
                {"field": "platform", "operator": "in", "value": [platform]}
            )
        pages = await self._paginated_post("endpoints/get_endpoint", params, limit=limit)
        endpoints = []
        for obj in pages:
            endpoints.extend(obj.get("reply", {}).get("endpoints", []))
        return endpoints[:limit] if limit is not None else endpoints

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
