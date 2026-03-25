from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from .. import XdrClient, XdrClientOptions
from ..errors import XdrError, XdrNotFoundError, XdrValidationError
from .commands import parse_slack_command
from .responders import SlackMessage, format_endpoints_compact, format_json_block, format_list_summary
from .tenant_store import TenantStore


SendFunc = Callable[[Any], Awaitable[None]]
logger = logging.getLogger(__name__)


class SlackXdrDispatcher:
    def __init__(self, tenant_store: TenantStore, client_options: Optional[XdrClientOptions] = None):
        self.tenant_store = tenant_store
        self.client_options = client_options or XdrClientOptions()

    async def dispatch(self, text: str, user_id: str, send: SendFunc) -> None:
        try:
            logger.info("Dispatching Slack command user_id=%s raw_text=%r", user_id, text)
            cmd = parse_slack_command(text)
            logger.info(
                "Parsed Slack command group=%s action=%s tenant=%r positional=%r options=%r flags=%r",
                cmd.group,
                cmd.action,
                cmd.tenant,
                cmd.positional,
                cmd.options,
                cmd.flags,
            )
            tenant_alias = await self._resolve_tenant_alias(cmd.tenant, user_id)
            logger.info("Resolved Slack tenant alias user_id=%s tenant=%s", user_id, tenant_alias)
            tenant = await self.tenant_store.get_tenant(tenant_alias)
            await send("Working on `%s %s` for tenant `%s`..." % (cmd.group, cmd.action, tenant_alias))
            async with XdrClient(tenant=tenant, options=self.client_options) as client:
                payload = await self._execute(client, cmd)
            if isinstance(payload, SlackMessage):
                command_text = text.strip()
                command_echo = f"```/xdr {command_text}```" if command_text else "```/xdr```"
                attachments = list(payload.attachments or [])
                if attachments and isinstance(attachments[0], dict):
                    first = dict(attachments[0])
                    blocks = list(first.get("blocks") or [])
                    blocks.insert(
                        0,
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": command_echo},
                        },
                    )
                    first["blocks"] = blocks
                    attachments[0] = first
                await send(
                    SlackMessage(
                        text=payload.text,
                        attachments=attachments or payload.attachments,
                    )
                )
            elif isinstance(payload, str):
                await send(payload)
            else:
                await send(format_json_block(payload))
        except XdrNotFoundError as exc:
            await send(str(exc))
        except XdrValidationError as exc:
            logger.warning("Slack command validation error user_id=%s raw_text=%r err=%s", user_id, text, exc)
            await send("Invalid command: %s" % exc)
        except XdrError as exc:
            logger.exception("Slack XDR error user_id=%s raw_text=%r", user_id, text)
            await send("XDR error: %s" % exc)
        except Exception as exc:
            logger.exception("Unhandled Slack command error user_id=%s raw_text=%r", user_id, text)
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
        logger.info("Executing Slack command branch %s %s", cmd.group, cmd.action)
        if cmd.group == "endpoint" and cmd.action == "list":
            platform = cmd.options.get("platform")
            logger.debug("Calling endpoints.list platform=%r", platform)
            items = await client.endpoints.list(platform=platform)
            title = "Endpoints"
            if platform:
                title = f"Endpoints (platform={platform})"
            return format_endpoints_compact(items, title=title)
        if cmd.group == "endpoint" and cmd.action == "get":
            if not cmd.positional:
                raise XdrValidationError("endpoint get requires HOSTNAME")
            logger.debug("Calling endpoints.get_by_hostname hostname=%r", cmd.positional[0])
            items = await client.endpoints.get_by_hostname(cmd.positional[0])
            if "field" in cmd.options:
                field = cmd.options["field"]
                return [{field: item.get(field, "N/A")} for item in items]
            return format_endpoints_compact(items, title="Endpoint lookup")
        if cmd.group == "endpoint" and cmd.action == "who-has":
            if not cmd.positional:
                raise XdrValidationError("endpoint who-has requires IP")
            logger.debug("Calling endpoints.who_has ip=%r", cmd.positional[0])
            ip = cmd.positional[0]
            items = await client.endpoints.who_has(ip)
            if "field" in cmd.options:
                field = cmd.options["field"]
                return [{field: item.get(field, "N/A")} for item in items]
            return format_endpoints_compact(items, title=f"Endpoints for IP {ip}")
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
