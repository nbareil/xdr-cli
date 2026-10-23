from __future__ import annotations

from typing import Dict

from .base import ListableService


class IncidentsService(ListableService):
    LIST_URI = "incidents/get_incidents"
    LIST_KEY = "incidents"
    # incident_id only supports "in" filters, so the time fields come first
    # to get keyset paging.
    SORT_FIELDS = ("creation_time", "modification_time", "incident_id")
    KEYSET_FIELDS = {"creation_time": "creation_time", "modification_time": "modification_time"}

    async def get(self, incident_id: str) -> Dict:
        params = {"request_data": {"incident_id": incident_id}}
        obj = await self.transport.post_json("incidents/get_incident_extra_data", params)
        return obj.get("reply", {})

    async def close(self, incident_id: str) -> Dict:
        self._ensure_writable("incident close")
        params = {
            "request_data": {"incident_id_": incident_id},
            "update_data": {"status": "resolved"},
        }
        return await self.transport.post_json("incidents/update_incidents", params)
