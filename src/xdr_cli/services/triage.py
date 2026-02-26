from __future__ import annotations

from typing import Dict, List, Optional

from ..errors import XdrValidationError
from .base import BaseService


class TriageService(BaseService):
    async def presets(self) -> List[Dict]:
        obj = await self.transport.post_json("get_triage_presets", {"request_data": {}})
        return obj.get("reply", {}).get("triage_presets", [])

    async def start(self, agent_ids: List[str], collector_uuid: Optional[str] = None) -> Dict:
        if not agent_ids:
            raise XdrValidationError("agent_ids must not be empty")
        request_data = {"agent_ids": agent_ids}
        if collector_uuid:
            request_data["collector_uuid"] = collector_uuid
        obj = await self.transport.post_json("triage_endpoint", {"request_data": request_data})
        return obj.get("reply", {})

    async def start_by_hostname(self, hostname: str, collector_uuid: Optional[str] = None) -> Dict:
        endpoints = await self.client.endpoints.get_by_hostname(hostname)
        agent_ids = [e.get("endpoint_id") for e in endpoints if e.get("endpoint_id")]
        if not agent_ids:
            raise XdrValidationError("No matching agent IDs found for hostname %s" % hostname)
        return await self.start(agent_ids, collector_uuid=collector_uuid)

    async def action_status(self, group_action_id: int) -> Dict:
        params = {"request_data": {"group_action_id": group_action_id}}
        obj = await self.transport.post_json("actions/get_action_status", params)
        return obj.get("reply", {}).get("data", {})

    async def statuses(self, group_action_ids: List[int]) -> List[Dict]:
        ret = []
        for action_id in group_action_ids:
            ret.append({
                "group_action_id": action_id,
                "statuses": await self.action_status(action_id),
            })
        return ret
