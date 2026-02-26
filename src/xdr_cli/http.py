from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from .auth import generate_auth_headers
from .errors import XdrApiError, XdrTransportError
from .models import XdrClientOptions, XdrTenant


class XdrTransport:
    def __init__(self, tenant: XdrTenant, options: Optional[XdrClientOptions] = None):
        self._tenant = tenant
        self._options = options or XdrClientOptions()
        self._client = httpx.AsyncClient(
            timeout=self._options.timeout_seconds,
            verify=self._options.verify_ssl,
            headers={"User-Agent": self._options.user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def tenant(self) -> XdrTenant:
        return self._tenant

    def build_api_url(self, uri: str) -> str:
        base = self._tenant.tenant_base_url.replace("https://", "").replace("http://", "")
        return "https://api-%s/public_api/v1/%s" % (base, uri.lstrip("/"))

    def build_auth_headers(self) -> Dict[str, str]:
        return generate_auth_headers(
            self._tenant.credentials.api_key_id,
            self._tenant.credentials.api_key_hash,
        )

    async def post_json(self, uri: str, parameters: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        payload = parameters or {}
        req_headers = {}
        if headers:
            req_headers.update(headers)
        req_headers.update(self.build_auth_headers())
        url = self.build_api_url(uri)
        try:
            response = await self._client.post(url, headers=req_headers, json=payload)
        except httpx.HTTPError as exc:
            raise XdrTransportError(str(exc))
        if response.status_code != 200:
            raise XdrApiError(uri, response.status_code, response.reason_phrase, response.text)
        return response.json()

    async def post_bytes_url(self, url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
        req_headers = {}
        if headers:
            req_headers.update(headers)
        req_headers.update(self.build_auth_headers())
        try:
            response = await self._client.post(url, headers=req_headers)
        except httpx.HTTPError as exc:
            raise XdrTransportError(str(exc))
        if response.status_code != 200:
            raise XdrApiError(url, response.status_code, response.reason_phrase, response.text)
        return response.content

    async def post_stream_lines(self, uri: str, parameters: Dict[str, Any]) -> list:
        url = self.build_api_url(uri)
        headers = self.build_auth_headers()
        lines = []
        try:
            async with self._client.stream("POST", url, headers=headers, json=parameters) as response:
                if response.status_code != 200:
                    text = await response.aread()
                    raise XdrApiError(uri, response.status_code, response.reason_phrase, text.decode("utf-8", errors="replace"))
                async for line in response.aiter_lines():
                    if line:
                        lines.append(line)
        except httpx.HTTPError as exc:
            raise XdrTransportError(str(exc))
        return lines
