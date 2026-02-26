from __future__ import annotations

from typing import Optional

from .http import XdrTransport
from .models import XdrClientOptions, XdrTenant
from .services.alerts import AlertsService
from .services.endpoints import EndpointsService
from .services.files import FilesService
from .services.incidents import IncidentsService
from .services.scripts import ScriptsService
from .services.triage import TriageService
from .services.xql import XqlService


class XdrClient:
    def __init__(self, tenant: XdrTenant, options: Optional[XdrClientOptions] = None):
        self.transport = XdrTransport(tenant=tenant, options=options)
        self.endpoints = EndpointsService(self)
        self.incidents = IncidentsService(self)
        self.alerts = AlertsService(self)
        self.triage = TriageService(self)
        self.scripts = ScriptsService(self)
        self.files = FilesService(self)
        self.xql = XqlService(self)

    async def aclose(self) -> None:
        await self.transport.aclose()

    async def __aenter__(self) -> "XdrClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
