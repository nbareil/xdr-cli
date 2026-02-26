from __future__ import annotations

from typing import Dict, List, Optional, Protocol

from ..models import XdrTenant


class TenantStore(Protocol):
    async def get_tenant(self, tenant_alias: str) -> XdrTenant:
        ...

    async def list_tenants_for_user(self, user_id: str) -> List[str]:
        ...

    async def get_default_tenant_for_user(self, user_id: str) -> Optional[str]:
        ...


class InMemoryTenantStore:
    def __init__(self, tenants: Dict[str, XdrTenant], user_defaults: Optional[Dict[str, str]] = None):
        self._tenants = dict(tenants)
        self._user_defaults = dict(user_defaults or {})

    async def get_tenant(self, tenant_alias: str) -> XdrTenant:
        return self._tenants[tenant_alias]

    async def list_tenants_for_user(self, user_id: str) -> List[str]:
        return sorted(self._tenants.keys())

    async def get_default_tenant_for_user(self, user_id: str) -> Optional[str]:
        return self._user_defaults.get(user_id)
