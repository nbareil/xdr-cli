from __future__ import annotations

import logging
from typing import Optional

from ..models import XdrClientOptions
from .router import SlackXdrDispatcher
from .responders import SlackMessage
from .tenant_store import TenantStore

try:
    from slack_bolt.async_app import AsyncApp
except Exception:  # pragma: no cover - optional dependency
    AsyncApp = None


logger = logging.getLogger(__name__)


def create_slack_app(signing_secret: str, bot_token: str, tenant_store: TenantStore, client_options: Optional[XdrClientOptions] = None):
    if AsyncApp is None:
        raise RuntimeError("slack-bolt is not installed. Install xdr-cli[slack].")
    app = AsyncApp(signing_secret=signing_secret, token=bot_token)
    dispatcher = SlackXdrDispatcher(tenant_store=tenant_store, client_options=client_options)

    @app.command("/xdr")
    async def handle_xdr_command(ack, body, respond, command, logger):
        await ack()
        raw_text = command.get("text", "")
        logger.info(
            "Slack /xdr command received user_id=%s command=%r text=%r",
            command.get("user_id") or body.get("user_id"),
            command.get("command"),
            raw_text,
        )
        logger.debug("Slack /xdr body=%r", body)
        logger.debug("Slack /xdr command payload=%r", command)
        user_id = body.get("user_id") or command.get("user_id") or ""
        thread_ts = body.get("trigger_id")

        async def send(message) -> None:
            kwargs = {}
            if isinstance(message, SlackMessage):
                kwargs["text"] = message.text
                kwargs["attachments"] = message.attachments
            else:
                kwargs["text"] = str(message)
            if thread_ts:
                kwargs["response_type"] = "ephemeral"
                await respond(**kwargs)
            else:
                await respond(**kwargs)

        await dispatcher.dispatch(raw_text, user_id=user_id, send=send)

    return app
