from __future__ import annotations

from typing import Optional

from ..models import XdrClientOptions
from .router import SlackXdrDispatcher
from .tenant_store import TenantStore

try:
    from slack_bolt.async_app import AsyncApp
except Exception:  # pragma: no cover - optional dependency
    AsyncApp = None


def create_slack_app(signing_secret: str, bot_token: str, tenant_store: TenantStore, client_options: Optional[XdrClientOptions] = None):
    if AsyncApp is None:
        raise RuntimeError("slack-bolt is not installed. Install xdr-cli[slack].")
    app = AsyncApp(signing_secret=signing_secret, token=bot_token)
    dispatcher = SlackXdrDispatcher(tenant_store=tenant_store, client_options=client_options)

    @app.command("/xdr")
    async def handle_xdr_command(ack, body, respond, command, logger):
        await ack()
        user_id = body.get("user_id") or command.get("user_id") or ""
        thread_ts = body.get("trigger_id")

        async def send(message: str) -> None:
            if thread_ts:
                await respond(text=message, response_type="ephemeral")
            else:
                await respond(text=message)

        await dispatcher.dispatch(command.get("text", ""), user_id=user_id, send=send)

    return app
