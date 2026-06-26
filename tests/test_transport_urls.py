import asyncio

import pytest

from xdr_cli.http import XdrTransport
from xdr_cli.models import XdrCredentials, XdrTenant


def _transport(base_url):
    tenant = XdrTenant(base_url, XdrCredentials("1", "h"))
    return XdrTransport(tenant)


def _close(transport):
    asyncio.run(transport.aclose())


@pytest.mark.parametrize(
    "base_url",
    [
        "tenant.xdr.us.paloaltonetworks.com",
        "https://tenant.xdr.us.paloaltonetworks.com",
        "http://tenant.xdr.us.paloaltonetworks.com/",
        "tenant.xdr.us.paloaltonetworks.com/some/path",
    ],
)
def test_build_api_url_normalizes_host(base_url):
    transport = _transport(base_url)
    try:
        url = transport.build_api_url("incidents/get_incidents")
        assert url == (
            "https://api-tenant.xdr.us.paloaltonetworks.com"
            "/public_api/v1/incidents/get_incidents"
        )
    finally:
        _close(transport)


def test_build_api_url_does_not_double_prefix_api():
    transport = _transport("api-tenant.xdr.us.paloaltonetworks.com")
    try:
        assert transport.build_api_url("x").startswith(
            "https://api-tenant.xdr.us.paloaltonetworks.com/"
        )
    finally:
        _close(transport)


def test_build_api_url_strips_leading_slash_on_uri():
    transport = _transport("tenant.example.com")
    try:
        assert transport.build_api_url("/alerts/get_alerts").endswith(
            "/public_api/v1/alerts/get_alerts"
        )
    finally:
        _close(transport)
