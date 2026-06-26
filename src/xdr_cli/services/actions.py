from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from ..errors import XdrOperationTimeoutError, XdrValidationError
from .base import BaseService


FINAL_ACTION_STATES = {
    "CANCELLED",
    "ABORTED",
    "EXPIRED",
    "COMPLETED_PARTIAL",
    "COMPLETED_SUCCESSFULLY",
    "FAILED",
    "TIMEOUT",
}


class ResponseActionsService(BaseService):
    """Endpoint response actions (isolate, scan, quarantine, ...).

    Mutating helpers accept either ``endpoint_id`` or ``hostname`` and resolve
    hostnames through :class:`EndpointsService`.
    """

    async def _resolve_endpoint_id(
        self, endpoint_id: Optional[str], hostname: Optional[str]
    ) -> str:
        if endpoint_id:
            return endpoint_id
        if hostname:
            endpoints = await self.client.endpoints.get_by_hostname(hostname)
            resolved = endpoints[0].get("endpoint_id") if endpoints else None
            if not resolved:
                raise XdrValidationError("No endpoint_id for hostname %s" % hostname)
            return resolved
        raise XdrValidationError("endpoint_id or hostname is required")

    def _endpoint_filter(self, endpoint_id: str) -> Dict:
        return {
            "request_data": {
                "filters": [
                    {"field": "endpoint_id_list", "operator": "in", "value": [endpoint_id]}
                ]
            }
        }

    async def isolate(self, endpoint_id: Optional[str] = None, hostname: Optional[str] = None) -> Dict:
        self._ensure_writable("isolate")
        resolved = await self._resolve_endpoint_id(endpoint_id, hostname)
        obj = await self.transport.post_json("endpoints/isolate", self._endpoint_filter(resolved))
        return obj.get("reply", {})

    async def unisolate(self, endpoint_id: Optional[str] = None, hostname: Optional[str] = None) -> Dict:
        self._ensure_writable("unisolate")
        resolved = await self._resolve_endpoint_id(endpoint_id, hostname)
        obj = await self.transport.post_json("endpoints/unisolate", self._endpoint_filter(resolved))
        return obj.get("reply", {})

    async def scan(
        self,
        endpoint_id: Optional[str] = None,
        hostname: Optional[str] = None,
        all_endpoints: bool = False,
        platform: Optional[str] = None,
    ) -> Dict:
        self._ensure_writable("scan")
        if all_endpoints:
            if not platform:
                raise XdrValidationError("scanning all endpoints requires a platform")
            params = {
                "request_data": {
                    "filters": [
                        {"field": "platform", "operator": "in", "value": [platform]}
                    ]
                }
            }
        else:
            resolved = await self._resolve_endpoint_id(endpoint_id, hostname)
            params = self._endpoint_filter(resolved)
        obj = await self.transport.post_json("endpoints/scan", params)
        return obj.get("reply", {})

    async def quarantine(
        self,
        file_path: str,
        file_hash: str,
        endpoint_id: Optional[str] = None,
        hostname: Optional[str] = None,
    ) -> Dict:
        self._ensure_writable("quarantine")
        resolved = await self._resolve_endpoint_id(endpoint_id, hostname)
        params = {
            "request_data": {
                "filters": [
                    {"field": "endpoint_id_list", "operator": "in", "value": [resolved]}
                ],
                "file_path": file_path,
                "file_hash": file_hash,
            }
        }
        obj = await self.transport.post_json("endpoints/quarantine", params)
        return obj.get("reply", {})

    async def restore(
        self,
        file_hash: str,
        endpoint_id: Optional[str] = None,
        hostname: Optional[str] = None,
    ) -> Dict:
        self._ensure_writable("restore")
        request_data: Dict = {"file_hash": file_hash}
        if endpoint_id or hostname:
            resolved = await self._resolve_endpoint_id(endpoint_id, hostname)
            request_data["endpoint_id"] = resolved
        obj = await self.transport.post_json("endpoints/restore", {"request_data": request_data})
        return obj.get("reply", {})

    async def action_status(self, action_id: int) -> Dict:
        params = {"request_data": {"group_action_id": action_id}}
        obj = await self.transport.post_json("actions/get_action_status", params)
        return obj.get("reply", {}).get("data", {})

    async def poll_until_done(
        self,
        action_id: int,
        endpoint_id: Optional[str] = None,
        poll_interval_seconds: float = 5.0,
        poll_attempts: Optional[int] = 120,
    ) -> Dict:
        """Poll ``actions/get_action_status`` until the action reaches a final
        state for the relevant endpoint(s), then return the status map."""
        attempts = 0
        while True:
            data = await self.action_status(action_id)
            statuses: List[str]
            if endpoint_id is not None:
                statuses = [data.get(endpoint_id, "")]
            else:
                statuses = list(data.values())
            if statuses and all(s in FINAL_ACTION_STATES for s in statuses):
                return data
            attempts += 1
            if poll_attempts is not None and attempts >= poll_attempts:
                raise XdrOperationTimeoutError(
                    "Timed out waiting for action %s" % action_id
                )
            await asyncio.sleep(poll_interval_seconds)
