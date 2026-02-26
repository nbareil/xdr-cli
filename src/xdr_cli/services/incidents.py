from __future__ import annotations

from typing import Dict, List

from .base import BaseService


class IncidentsService(BaseService):
    async def list(self) -> List[Dict]:
        pages = await self._paginated_post("incidents/get_incidents", {"request_data": {}})
        items = []
        for obj in pages:
            items.extend(obj.get("reply", {}).get("incidents", []))
        return items

    async def get(self, incident_id: str) -> Dict:
        params = {"request_data": {"incident_id": incident_id}}
        obj = await self.transport.post_json("incidents/get_incident_extra_data", params)
        return obj.get("reply", {})

    async def close(self, incident_id: str) -> Dict:
        params = {
            "request_data": {"incident_id_": incident_id},
            "update_data": {"status": "resolved"},
        }
        return await self.transport.post_json("incidents/update_incidents", params)
