from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional

from ..errors import XdrOperationTimeoutError, XdrValidationError
from .base import BaseService


class XqlService(BaseService):
    async def run(
        self,
        query: str,
        begin_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        poll_interval_seconds: float = 2.0,
        poll_attempts: int = 150,
    ) -> List[Dict]:
        if not query:
            raise XdrValidationError("query must not be empty")
        params = {"request_data": {"query": query}}
        if begin_time and end_time:
            params["request_data"]["timeframe"] = {
                "from": self._serialize_time(begin_time),
                "to": self._serialize_time(end_time),
            }
        start = await self.transport.post_json("xql/start_xql_query", params)
        query_id = start.get("reply")
        results_payload = {
            "request_data": {
                "query_id": query_id,
                "format": "json",
                "pending_flag": True,
            }
        }
        attempts = 0
        while True:
            obj = await self.transport.post_json("xql/get_query_results", results_payload)
            reply = obj.get("reply", {})
            status = reply.get("status")
            if status == "SUCCESS":
                break
            if status != "PENDING":
                raise XdrValidationError("xql/get_query_results failed: %s" % json.dumps(obj))
            attempts += 1
            if attempts >= poll_attempts:
                raise XdrOperationTimeoutError(
                    "Timed out waiting for XQL query %s to complete" % query_id
                )
            await asyncio.sleep(poll_interval_seconds)
        if reply.get("number_of_results", 0) <= 1000:
            return list(reply.get("results", {}).get("data", []))
        stream_id = reply.get("results", {}).get("stream_id")
        return await self.run_stream(stream_id)

    async def run_stream(self, stream_id: str) -> List[Dict]:
        params = {"request_data": {"stream_id": stream_id, "is_gzip_compressed": False}}
        lines = await self.transport.post_stream_lines("xql/get_query_results_stream", params)
        results = []
        for line in lines:
            results.append(json.loads(line))
        return results

    @staticmethod
    def _serialize_time(value: datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone().isoformat()
