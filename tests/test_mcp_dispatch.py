import pytest

from xdr_cli.errors import XdrValidationError
from xdr_cli.mcp.dispatch import TOOL_SPECS, _HANDLERS, XdrToolDispatcher, _paging
from xdr_cli.models import XdrCredentials, XdrTenant


TENANTS = {
    "demo": XdrTenant("demo.example.com", XdrCredentials("1", "h")),
    "other": XdrTenant("other.example.com", XdrCredentials("2", "h2")),
}


def test_every_advertised_tool_has_a_handler():
    # Catches a tool spec without a matching handler (or a typo'd name).
    spec_names = {spec.name for spec in TOOL_SPECS}
    assert spec_names == set(_HANDLERS)


def test_tool_specs_have_tenant_property():
    for spec in TOOL_SPECS:
        assert "tenant" in spec.input_schema["properties"]


def test_required_args_are_declared_as_properties():
    for spec in TOOL_SPECS:
        props = spec.input_schema["properties"]
        for required in spec.input_schema.get("required", []):
            assert required in props, f"{spec.name}: required {required!r} not in properties"


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


def test_list_tools_advertise_offset_and_limit():
    for name in ("endpoint_list", "incident_list", "alert_list"):
        spec = next(s for s in TOOL_SPECS if s.name == name)
        props = spec.input_schema["properties"]
        assert {"offset", "limit", "sort_field", "sort_order"} <= set(props)
        assert props["sort_order"]["enum"] == ["asc", "desc"]


def test_paging_args_validated():
    assert _paging({}) == {"offset": 0, "limit": None, "sort_field": None, "sort_order": None}
    assert _paging({"offset": 5, "limit": 10, "sort_field": "severity", "sort_order": "asc"}) == {
        "offset": 5, "limit": 10, "sort_field": "severity", "sort_order": "asc",
    }
    with pytest.raises(XdrValidationError):
        _paging({"offset": -1})
    with pytest.raises(XdrValidationError):
        _paging({"limit": 0})
