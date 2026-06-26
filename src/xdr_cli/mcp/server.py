from __future__ import annotations

import asyncio
import json
from typing import Dict, Optional

from ..errors import XdrError
from ..models import XdrClientOptions, XdrTenant
from .dispatch import TOOL_SPECS, XdrToolDispatcher


def build_server(
    tenants: Dict[str, XdrTenant],
    default_tenant: Optional[str] = None,
    client_options: Optional[XdrClientOptions] = None,
):
    """Construct an MCP ``Server`` exposing XDR services as tools.

    Imports the MCP SDK lazily so the core package stays importable without it.
    """
    try:
        from mcp.server import Server
        import mcp.types as types
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("The 'mcp' package is required. Install xdr-cli[mcp].") from exc

    dispatcher = XdrToolDispatcher(
        tenants=tenants, default_tenant=default_tenant, client_options=client_options
    )
    read_only = bool(getattr(client_options, "read_only", False))
    server = Server("xdr-cli")

    @server.list_tools()
    async def list_tools():
        tools = []
        for spec in TOOL_SPECS:
            if read_only and spec.mutating:
                continue
            tools.append(
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.input_schema,
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            result = await dispatcher.call(name, arguments or {})
        except XdrError as exc:
            payload = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
            return [types.TextContent(type="text", text=json.dumps(payload))]
        payload = {"ok": True, "data": result}
        return [types.TextContent(type="text", text=json.dumps(payload, default=str))]

    return server


def run_stdio_server(
    tenants: Dict[str, XdrTenant],
    default_tenant: Optional[str] = None,
    client_options: Optional[XdrClientOptions] = None,
) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(tenants, default_tenant=default_tenant, client_options=client_options)

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())
