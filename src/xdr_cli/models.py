from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class XdrCredentials:
    api_key_id: str
    api_key_hash: str


@dataclass(frozen=True)
class XdrTenant:
    tenant_base_url: str
    credentials: XdrCredentials


@dataclass(frozen=True)
class XdrClientOptions:
    timeout_seconds: float = 30.0
    user_agent: str = "xdr-cli-library/0.1"
    verify_ssl: bool = True
    read_only: bool = False


@dataclass(frozen=True)
class ListPage:
    items: List[Dict[str, Any]]
    offset: int
    total_count: int
    next_offset: Optional[int]
