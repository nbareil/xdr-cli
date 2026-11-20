from __future__ import annotations

import asyncio
import os
from typing import List, Optional, Union

from ..errors import XdrOperationTimeoutError, XdrValidationError
from .actions import FINAL_ACTION_STATES as _FINAL_STATES
from .base import BaseService


class FilesService(BaseService):
    async def get_action_status(self, action_id, endpoint_id) -> str:
        params = {"request_data": {"group_action_id": action_id}}
        obj = await self.transport.post_json("actions/get_action_status", params)
        return obj.get("reply", {}).get("data", {}).get(endpoint_id, "")

    async def retrieve_details(self, action_id):
        params = {"request_data": {"group_action_id": action_id}}
        obj = await self.transport.post_json("actions/file_retrieval_details", params)
        return obj.get("reply", {}).get("data", {})

    async def retrieve(self, endpoint_id: str, paths: List[str], hostname: Optional[str] = None, poll_interval_seconds: float = 5.0, poll_attempts: Optional[int] = None) -> bytes:
        """Retrieve ``paths`` and return the archive in memory."""
        dl_url = await self._retrieve_download_url(endpoint_id, paths, hostname, poll_interval_seconds, poll_attempts)
        return await self.transport.post_bytes_url(dl_url)

    async def retrieve_to_file(self, endpoint_id: str, paths: List[str], destination: Union[str, "os.PathLike[str]"], hostname: Optional[str] = None, poll_interval_seconds: float = 5.0, poll_attempts: Optional[int] = None) -> int:
        """Retrieve ``paths`` and stream the archive to ``destination``.

        Returns the archive size in bytes.
        """
        dl_url = await self._retrieve_download_url(endpoint_id, paths, hostname, poll_interval_seconds, poll_attempts)
        return await self.transport.download_url(dl_url, destination)

    async def _retrieve_download_url(self, endpoint_id: str, paths: List[str], hostname: Optional[str], poll_interval_seconds: float, poll_attempts: Optional[int]) -> str:
        """Start a file retrieval, wait for it, and return its download URL."""
        if hostname:
            endpoints = await self.client.endpoints.get_by_hostname(hostname)
            endpoint_id = endpoints[0].get("endpoint_id")
        if not endpoint_id:
            raise XdrValidationError("endpoint_id or hostname is required")
        params = {
            "request_data": {
                "filters": [
                    {"field": "endpoint_id_list", "operator": "in", "value": [endpoint_id]}
                ],
                "files": {"windows": list(paths), "macos": list(paths), "linux": list(paths)},
            }
        }
        obj = await self.transport.post_json("endpoints/file_retrieval", params)
        action_id = obj.get("reply", {}).get("action_id")
        if not action_id:
            raise XdrValidationError("There was an error retrieving the file")

        attempts = 0
        while True:
            status = await self.get_action_status(action_id, endpoint_id)
            if status in _FINAL_STATES:
                break
            attempts += 1
            if poll_attempts is not None and attempts >= poll_attempts:
                raise XdrOperationTimeoutError("Timed out waiting for file retrieval action %s" % action_id)
            await asyncio.sleep(poll_interval_seconds)
        if not status.startswith("COMPLETED"):
            raise XdrValidationError("File retrieval ended in state %s" % status)
        details = await self.retrieve_details(action_id)
        dl_url = details.get(endpoint_id)
        if not dl_url:
            raise XdrValidationError("No download URL for endpoint %s" % endpoint_id)
        return dl_url
