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
    # Retries for idempotent reads (post_json(..., retry=True)) only.
    max_retries: int = 4
    retry_backoff_seconds: float = 1.0
    retry_max_backoff_seconds: float = 30.0


@dataclass(frozen=True)
class ListPage:
    items: List[Dict[str, Any]]
    total_count: int
    # Opaque token to resume right after these items; None when complete.
    next_cursor: Optional[str]
    sort_field: str
    sort_order: str
