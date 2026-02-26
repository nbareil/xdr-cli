from __future__ import annotations

from typing import Awaitable, Callable, Optional

from .. import XdrClient, XdrClientOptions
from ..errors import XdrError, XdrNotFoundError, XdrValidationError
from .commands import parse_slack_command
from .responders import format_json_block, format_list_summary
from .tenant_store import TenantStore


SendFunc = Callable[[str], Awaitable[None]]


class SlackXdrDispatcher:
    def __init__(self, tenant_store: TenantStore, client_options: Optional[XdrClientOptions] = None):
        self.tenant_store = tenant_store
        self.client_options = client_options or XdrClientOptions()

    async def dispatch(self, text: str, user_id: str, send: SendFunc) -> None:
        try:
            cmd = parse_slack_command(text)
            tenant_alias = await self._resolve_tenant_alias(cmd.tenant, user_id)
            tenant = await self.tenant_store.get_tenant(tenant_alias)
            await send("Working on `%s %s` for tenant `%s`..." % (cmd.group, cmd.action, tenant_alias))
            async with XdrClient(tenant=tenant, options=self.client_options) as client:
                payload = await self._execute(client, cmd)
            await send(format_json_block(payload))
        except XdrNotFoundError as exc:
            await send(str(exc))
        except XdrValidationError as exc:
            await send("Invalid command: %s" % exc)
        except XdrError as exc:
            await send("XDR error: %s" % exc)
        except Exception as exc:
            await send("Unhandled error: %s" % exc)

    async def _resolve_tenant_alias(self, explicit_alias: Optional[str], user_id: str) -> str:
        if explicit_alias:
            return explicit_alias
        default_alias = await self.tenant_store.get_default_tenant_for_user(user_id)
        if default_alias:
            return default_alias
        aliases = await self.tenant_store.list_tenants_for_user(user_id)
        if len(aliases) == 1:
            return aliases[0]
        if not aliases:
            raise XdrValidationError("No tenant is configured for this workspace")
        raise XdrValidationError(
            "Please specify --tenant. Available tenants: %s" % ", ".join(aliases)
        )

    async def _execute(self, client: XdrClient, cmd):
        if cmd.group == "endpoint" and cmd.action == "list":
            platform = cmd.options.get("platform")
            items = await client.endpoints.list(platform=platform)
            return {"summary": format_list_summary(items, "endpoints"), "items": items}
        if cmd.group == "endpoint" and cmd.action == "get":
            if not cmd.positional:
                raise XdrValidationError("endpoint get requires HOSTNAME")
            items = await client.endpoints.get_by_hostname(cmd.positional[0])
            if "field" in cmd.options:
                field = cmd.options["field"]
                return [{field: item.get(field, "N/A")} for item in items]
            return items
        if cmd.group == "endpoint" and cmd.action == "who-has":
            if not cmd.positional:
                raise XdrValidationError("endpoint who-has requires IP")
            return await client.endpoints.who_has(cmd.positional[0])
        if cmd.group == "incident" and cmd.action == "list":
            items = await client.incidents.list()
            return {"summary": format_list_summary(items, "incidents"), "items": items}
        if cmd.group == "incident" and cmd.action == "get":
            if not cmd.positional:
                raise XdrValidationError("incident get requires INCIDENT_ID")
            return await client.incidents.get(cmd.positional[0])
        if cmd.group == "incident" and cmd.action == "close":
            if not cmd.positional:
                raise XdrValidationError("incident close requires INCIDENT_ID")
            return await client.incidents.close(cmd.positional[0])
        if cmd.group == "alert" and cmd.action == "list":
            items = await client.alerts.list()
            return {"summary": format_list_summary(items, "alerts"), "items": items}
        if cmd.group == "alert" and cmd.action == "close":
            if not cmd.positional:
                raise XdrValidationError("alert close requires one or more ALERT_ID values")
            return await client.alerts.close([int(x) for x in cmd.positional])
        if cmd.group == "triage" and cmd.action == "presets":
            return await client.triage.presets()
        if cmd.group == "triage" and cmd.action == "new":
            if not cmd.positional:
                raise XdrValidationError("triage new requires HOSTNAME")
            return await client.triage.start_by_hostname(
                cmd.positional[0], collector_uuid=cmd.options.get("collector-uuid")
            )
        if cmd.group == "triage" and cmd.action == "status":
            if not cmd.positional:
                raise XdrValidationError("triage status requires one or more action IDs")
            return await client.triage.statuses([int(x) for x in cmd.positional])
        if cmd.group == "script" and cmd.action == "status":
            if not cmd.positional:
                raise XdrValidationError("script status requires ACTION_ID")
            return await client.scripts.execution_status(int(cmd.positional[0]))
        if cmd.group == "script" and cmd.action == "results":
            if not cmd.positional:
                raise XdrValidationError("script results requires ACTION_ID")
            return await client.scripts.execution_results(int(cmd.positional[0]))
        if cmd.group == "xql" and cmd.action == "run":
            query = " ".join(cmd.positional).strip() or cmd.options.get("query", "").strip()
            if not query:
                raise XdrValidationError("xql run requires a query string")
            results = await client.xql.run(query)
            return {"summary": format_list_summary(results, "rows"), "results": results}
        raise XdrValidationError("Unsupported command: %s %s" % (cmd.group, cmd.action))
