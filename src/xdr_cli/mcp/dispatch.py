from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..client import XdrClient
from ..errors import XdrValidationError
from ..models import ListPage, XdrClientOptions, XdrTenant
from ..services.alerts import AlertsService
from ..services.endpoints import EndpointsService
from ..services.incidents import IncidentsService


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    mutating: bool = False


def _obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": dict(properties)}
    schema["properties"].setdefault(
        "tenant", {"type": "string", "description": "Tenant alias (uses default if omitted)"}
    )
    if required:
        schema["required"] = list(required)
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_PAGING_DOC = (
    " Returns {items, total_count, next_cursor, sort_field, sort_order}; pass"
    " next_cursor back as 'cursor' (alone, without offset or sort) to fetch the"
    " next page. next_cursor is null when there are no more rows. Use 'limit'"
    " on large tenants."
)


def _paging_props(service_cls) -> Dict[str, Any]:
    return {
        "offset": _INT,
        "cursor": {"type": "string", "description": "next_cursor from a previous call"},
        "limit": _INT,
        "sort_field": {"type": "string", "enum": list(service_cls.SORT_FIELDS),
                       "description": "Default: %s" % service_cls.SORT_FIELDS[0]},
        "sort_order": {"type": "string", "enum": ["asc", "desc"],
                       "description": "Default: %s" % service_cls.DEFAULT_SORT_ORDER},
    }


TOOL_SPECS: List[ToolSpec] = [
    ToolSpec("endpoint_list", "List endpoints, optionally filtered by platform." + _PAGING_DOC,
             _obj({"platform": _STR, **_paging_props(EndpointsService)})),
    ToolSpec("endpoint_get", "Get endpoint details by hostname.",
             _obj({"hostname": _STR, "field": _STR}, required=["hostname"])),
    ToolSpec("endpoint_who_has", "Resolve an IP address to endpoint(s).",
             _obj({"ip": _STR}, required=["ip"])),
    ToolSpec("incident_list", "List incidents." + _PAGING_DOC, _obj(_paging_props(IncidentsService))),
    ToolSpec("incident_get", "Get extra data for an incident.",
             _obj({"incident_id": _STR}, required=["incident_id"])),
    ToolSpec("incident_close", "Close (resolve) an incident.",
             _obj({"incident_id": _STR}, required=["incident_id"]), mutating=True),
    ToolSpec("alert_list", "List alerts." + _PAGING_DOC, _obj(_paging_props(AlertsService))),
    ToolSpec("alert_close", "Close (resolve) one or more alerts.",
             _obj({"alert_ids": {"type": "array", "items": _INT}}, required=["alert_ids"]), mutating=True),
    ToolSpec("triage_presets", "List triage presets.", _obj({})),
    ToolSpec("triage_new", "Start a triage collection on a host.",
             _obj({"hostname": _STR, "collector_uuid": _STR}, required=["hostname"]), mutating=True),
    ToolSpec("triage_status", "Status for one or more triage action IDs.",
             _obj({"action_ids": {"type": "array", "items": _INT}}, required=["action_ids"])),
    ToolSpec("script_status", "Get script execution status by action ID.",
             _obj({"action_id": _INT}, required=["action_id"])),
    ToolSpec("script_results", "Get script execution results by action ID.",
             _obj({"action_id": _INT}, required=["action_id"])),
    ToolSpec("xql_run", "Run an XQL query and return rows.",
             _obj({"query": _STR}, required=["query"])),
    ToolSpec("endpoint_isolate", "Network-isolate an endpoint.",
             _obj({"endpoint_id": _STR, "hostname": _STR}), mutating=True),
    ToolSpec("endpoint_unisolate", "Remove isolation from an endpoint.",
             _obj({"endpoint_id": _STR, "hostname": _STR}), mutating=True),
    ToolSpec("endpoint_scan", "Trigger a malware scan on an endpoint.",
             _obj({"endpoint_id": _STR, "hostname": _STR, "all": {"type": "boolean"}, "platform": _STR}), mutating=True),
]


class XdrToolDispatcher:
    """Maps MCP tool names to XdrClient service calls. Independent of the MCP
    SDK so it can be unit-tested directly."""

    def __init__(
        self,
        tenants: Dict[str, XdrTenant],
        default_tenant: Optional[str] = None,
        client_options: Optional[XdrClientOptions] = None,
    ):
        self._tenants = dict(tenants)
        self._default_tenant = default_tenant
        self._client_options = client_options or XdrClientOptions()

    def _resolve_tenant(self, alias: Optional[str]) -> XdrTenant:
        alias = alias or self._default_tenant
        if not alias:
            if len(self._tenants) == 1:
                return next(iter(self._tenants.values()))
            raise XdrValidationError(
                "Specify 'tenant'. Available: %s" % ", ".join(sorted(self._tenants))
            )
        if alias not in self._tenants:
            raise XdrValidationError("Unknown tenant %r" % alias)
        return self._tenants[alias]

    async def call(self, name: str, arguments: Dict[str, Any]) -> Any:
        arguments = dict(arguments or {})
        tenant = self._resolve_tenant(arguments.pop("tenant", None))
        async with XdrClient(tenant=tenant, options=self._client_options) as client:
            handler = _HANDLERS.get(name)
            if handler is None:
                raise XdrValidationError("Unknown tool %r" % name)
            return await handler(client, arguments)


def _require(args: Dict[str, Any], key: str) -> Any:
    if key not in args or args[key] in (None, ""):
        raise XdrValidationError("Missing required argument: %s" % key)
    return args[key]


def _paging(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = args.get("limit")
    return {
        "offset": int(args.get("offset") or 0),
        "cursor": args.get("cursor"),
        "limit": int(limit) if limit is not None else None,
        "sort_field": args.get("sort_field"),
        "sort_order": args.get("sort_order"),
    }


async def _page(coro: Awaitable[ListPage]) -> Dict[str, Any]:
    return asdict(await coro)


Handler = Callable[[XdrClient, Dict[str, Any]], Awaitable[Any]]

_HANDLERS: Dict[str, Handler] = {
    "endpoint_list": lambda c, a: _page(c.endpoints.list(platform=a.get("platform"), **_paging(a))),
    "endpoint_get": lambda c, a: c.endpoints.get_by_hostname(_require(a, "hostname")),
    "endpoint_who_has": lambda c, a: c.endpoints.who_has(_require(a, "ip")),
    "incident_list": lambda c, a: _page(c.incidents.list(**_paging(a))),
    "incident_get": lambda c, a: c.incidents.get(_require(a, "incident_id")),
    "incident_close": lambda c, a: c.incidents.close(_require(a, "incident_id")),
    "alert_list": lambda c, a: _page(c.alerts.list(**_paging(a))),
    "alert_close": lambda c, a: c.alerts.close([int(x) for x in _require(a, "alert_ids")]),
    "triage_presets": lambda c, a: c.triage.presets(),
    "triage_new": lambda c, a: c.triage.start_by_hostname(_require(a, "hostname"), collector_uuid=a.get("collector_uuid")),
    "triage_status": lambda c, a: c.triage.statuses([int(x) for x in _require(a, "action_ids")]),
    "script_status": lambda c, a: c.scripts.execution_status(int(_require(a, "action_id"))),
    "script_results": lambda c, a: c.scripts.execution_results(int(_require(a, "action_id"))),
    "xql_run": lambda c, a: c.xql.run(query=_require(a, "query")),
    "endpoint_isolate": lambda c, a: c.actions.isolate(endpoint_id=a.get("endpoint_id"), hostname=a.get("hostname")),
    "endpoint_unisolate": lambda c, a: c.actions.unisolate(endpoint_id=a.get("endpoint_id"), hostname=a.get("hostname")),
    "endpoint_scan": lambda c, a: c.actions.scan(endpoint_id=a.get("endpoint_id"), hostname=a.get("hostname"), all_endpoints=bool(a.get("all")), platform=a.get("platform")),
}
