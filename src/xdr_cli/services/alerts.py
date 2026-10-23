from __future__ import annotations

from typing import Dict, List, Optional

from ..models import ListPage
from .base import BaseService


class AlertsService(BaseService):
    async def list(self, offset: int = 0, limit: Optional[int] = None) -> ListPage:
        return await self._paginated_list(
            "alerts/get_alerts", {"request_data": {}}, "alerts", offset=offset, limit=limit
        )

    async def close(self, alert_ids: List[int]) -> Dict:
        self._ensure_writable("alert close")
        params = {
            "request_data": {
                "alert_id_list": list(alert_ids),
                "update_data": {"status": "resolved_other"},
            }
        }
        return await self.transport.post_json("alerts/update_alerts", params)
