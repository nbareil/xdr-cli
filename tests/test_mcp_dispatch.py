import asyncio

import pytest

from xdr_cli.errors import XdrValidationError
from xdr_cli.mcp.dispatch import TOOL_SPECS, XdrToolDispatcher
from xdr_cli.models import XdrCredentials, XdrTenant


TENANTS = {
    "demo": XdrTenant("demo.example.com", XdrCredentials("1", "h")),
    "other": XdrTenant("other.example.com", XdrCredentials("2", "h2")),
}


def run(coro):
    return asyncio.run(coro)


def test_tool_specs_have_tenant_property():
    for spec in TOOL_SPECS:
        assert "tenant" in spec.input_schema["properties"]


def test_resolve_default_tenant():
    d = XdrToolDispatcher(TENANTS, default_tenant="demo")
    assert d._resolve_tenant(None).tenant_base_url == "demo.example.com"


def test_resolve_explicit_tenant():
    d = XdrToolDispatcher(TENANTS)
    assert d._resolve_tenant("other").tenant_base_url == "other.example.com"


def test_resolve_unknown_tenant_rejected():
    d = XdrToolDispatcher(TENANTS, default_tenant="demo")
    with pytest.raises(XdrValidationError):
        d._resolve_tenant("nope")


def test_ambiguous_tenant_requires_selection():
    d = XdrToolDispatcher(TENANTS)  # two tenants, no default
    with pytest.raises(XdrValidationError):
        d._resolve_tenant(None)


def test_single_tenant_auto_selected():
    d = XdrToolDispatcher({"only": TENANTS["demo"]})
    assert d._resolve_tenant(None).tenant_base_url == "demo.example.com"


def test_call_dispatches_to_service(monkeypatch):
    d = XdrToolDispatcher(TENANTS, default_tenant="demo")

    captured = {}

    class FakeEndpoints:
        async def list(self, platform=None, limit=None):
            captured["platform"] = platform
            captured["limit"] = limit
            return [{"endpoint_name": "h1"}]

    class FakeClient:
        def __init__(self, tenant, options=None):
            self.endpoints = FakeEndpoints()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("xdr_cli.mcp.dispatch.XdrClient", FakeClient)
    result = run(d.call("endpoint_list", {"platform": "linux", "limit": 5}))
    assert result == [{"endpoint_name": "h1"}]
    assert captured == {"platform": "linux", "limit": 5}


def test_unknown_tool_rejected(monkeypatch):
    d = XdrToolDispatcher(TENANTS, default_tenant="demo")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr("xdr_cli.mcp.dispatch.XdrClient", FakeClient)
    with pytest.raises(XdrValidationError):
        run(d.call("does_not_exist", {}))
