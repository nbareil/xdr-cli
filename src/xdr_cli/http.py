from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from .auth import generate_auth_headers
from .errors import XdrApiError, XdrTransportError
from .models import XdrClientOptions, XdrTenant

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# A 500 carrying this BigQuery message is deterministic (e.g. a too-deep
# search_from); retrying only burns time.
_NON_TRANSIENT_MARKERS = ("Resources exceeded",)


class XdrTransport:
    def __init__(self, tenant: XdrTenant, options: Optional[XdrClientOptions] = None):
        self._tenant = tenant
        self._options = options or XdrClientOptions()
        self._client = httpx.AsyncClient(
            timeout=self._options.timeout_seconds,
            verify=self._options.verify_ssl,
            headers={"User-Agent": self._options.user_agent},
        )
        self._sleep = asyncio.sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def tenant(self) -> XdrTenant:
        return self._tenant

    @property
    def options(self) -> XdrClientOptions:
        return self._options

    def _normalized_api_host(self) -> str:
        base = self._tenant.tenant_base_url.strip()
        base = base.replace("https://", "").replace("http://", "").strip("/")
        base = base.split("/", 1)[0]
        if not base.startswith("api-"):
            base = "api-%s" % base
        return base

    def build_api_url(self, uri: str) -> str:
        return "https://%s/public_api/v1/%s" % (self._normalized_api_host(), uri.lstrip("/"))

    def build_auth_headers(self) -> Dict[str, str]:
        return generate_auth_headers(
            self._tenant.credentials.api_key_id,
            self._tenant.credentials.api_key_hash,
        )

    async def post_json(
        self,
        uri: str,
        parameters: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: bool = False,
    ) -> Dict[str, Any]:
        """POST ``parameters`` as JSON and return the decoded reply.

        With ``retry=True`` (only for idempotent reads), timeouts, network
        errors, 429 and transient 5xx are retried with exponential backoff.
        Mutating calls must not retry: a timed-out request may have run.
        """
        attempts = 1 + (self._options.max_retries if retry else 0)
        for attempt in range(1, attempts + 1):
            try:
                return await self._post_json_once(uri, parameters or {}, headers)
            except (XdrTransportError, XdrApiError) as exc:
                if attempt == attempts or not _is_transient(exc):
                    raise
                delay = self._backoff(attempt, exc)
                logger.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                               uri, attempt, attempts, _short(exc), delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int, exc: Exception) -> float:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return min(retry_after, self._options.retry_max_backoff_seconds)
        delay = self._options.retry_backoff_seconds * (2 ** (attempt - 1))
        return min(delay, self._options.retry_max_backoff_seconds)

    async def _post_json_once(self, uri: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]]) -> Dict[str, Any]:
        req_headers = {}
        if headers:
            req_headers.update(headers)
        req_headers.update(self.build_auth_headers())
        url = self.build_api_url(uri)
        try:
            response = await self._client.post(url, headers=req_headers, json=payload)
        except httpx.HTTPError as exc:
            raise XdrTransportError(str(exc) or type(exc).__name__)
        if response.status_code != 200:
            error = XdrApiError(uri, response.status_code, response.reason_phrase, response.text)
            error.retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            raise error
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


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, XdrTransportError):
        return True
    if not isinstance(exc, XdrApiError) or exc.status_code not in RETRYABLE_STATUS:
        return False
    return not any(marker in (exc.response_text or "") for marker in _NON_TRANSIENT_MARKERS)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _short(exc: Exception) -> str:
    if isinstance(exc, XdrApiError):
        return "HTTP %s" % exc.status_code
    return str(exc)[:120]
