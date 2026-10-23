from __future__ import annotations

from typing import Dict, List

from .base import ListableService


class AlertsService(ListableService):
    LIST_URI = "alerts/get_alerts"
    LIST_KEY = "alerts"
    # get_alerts rejects alert_id/detection_timestamp as sort fields.
    # Rows don't carry creation_time, but detection_timestamp matches it
    # (checked against a live tenant), which is what keyset paging needs.
    SORT_FIELDS = ("creation_time", "severity")
    KEYSET_FIELDS = {"creation_time": "detection_timestamp"}

    async def close(self, alert_ids: List[int]) -> Dict:
        self._ensure_writable("alert close")
        params = {
            "request_data": {
                "alert_id_list": list(alert_ids),
                "update_data": {"status": "resolved_other"},
            }
        }
        return await self.transport.post_json("alerts/update_alerts", params)
