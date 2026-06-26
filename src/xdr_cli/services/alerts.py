from __future__ import annotations

from typing import Dict, List, Optional

from .base import BaseService


class AlertsService(BaseService):
    async def list(self, limit: Optional[int] = None) -> List[Dict]:
        pages = await self._paginated_post("alerts/get_alerts", {"request_data": {}}, limit=limit)
        items = []
        for obj in pages:
            items.extend(obj.get("reply", {}).get("alerts", []))
        return items[:limit] if limit is not None else items

    async def close(self, alert_ids: List[int]) -> Dict:
        self._ensure_writable("alert close")
        params = {
            "request_data": {
                "alert_id_list": list(alert_ids),
                "update_data": {"status": "resolved_other"},
            }
        }
        return await self.transport.post_json("alerts/update_alerts", params)
