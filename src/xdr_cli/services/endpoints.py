from __future__ import annotations

from typing import Dict, List, Optional

from ..errors import XdrNotFoundError
from .base import ListableService


class EndpointsService(ListableService):
    LIST_URI = "endpoints/get_endpoint"
    LIST_KEY = "endpoints"
    # first_seen (asc) is the default: it supports keyset paging and new
    # enrolments land at the end, so earlier pages don't shift. last_seen is
    # accepted by get_endpoint but does not actually order the results, and
    # endpoint_name is rejected, so neither is offered.
    SORT_FIELDS = ("first_seen", "endpoint_id")
    DEFAULT_SORT_ORDER = "asc"
    KEYSET_FIELDS = {"first_seen": "first_seen"}

    def _list_filters(self, platform: Optional[str] = None) -> List[Dict]:
        if not platform:
            return []
        return [{"field": "platform", "operator": "in", "value": [platform]}]

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
