from __future__ import annotations

from typing import Dict, Optional, Protocol


class JobStore(Protocol):
    async def create(self, job_id: str, payload: Dict) -> None:
        ...

    async def update(self, job_id: str, payload: Dict) -> None:
        ...

    async def get(self, job_id: str) -> Optional[Dict]:
        ...


class InMemoryJobStore:
    def __init__(self):
        self._data = {}

    async def create(self, job_id: str, payload: Dict) -> None:
        self._data[job_id] = dict(payload)

    async def update(self, job_id: str, payload: Dict) -> None:
        current = self._data.get(job_id, {})
        current.update(payload)
        self._data[job_id] = current

    async def get(self, job_id: str) -> Optional[Dict]:
        return self._data.get(job_id)
