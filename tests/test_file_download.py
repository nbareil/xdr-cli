import asyncio

import httpx
import pytest

from xdr_cli.errors import XdrApiError, XdrFileTooLargeError, XdrTransportError, XdrValidationError
from xdr_cli.http import XdrTransport
from xdr_cli.models import XdrClientOptions, XdrCredentials, XdrTenant
from xdr_cli.services.files import FilesService


def make_transport(handler):
    tenant = XdrTenant("tenant.example.com", XdrCredentials("1", "h"))
    transport = XdrTransport(tenant, XdrClientOptions())
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return transport


def download(transport, destination):
    async def go():
        try:
            return await transport.download_url("https://dl.example/f", destination)
        finally:
            await transport.aclose()

    return asyncio.run(go())


def test_download_url_streams_body_to_file(tmp_path):
    body = bytes(range(256)) * 10000

    def handler(request):
        assert request.method == "POST"
        assert request.headers["x-xdr-auth-id"] == "1"
        return httpx.Response(200, content=body)

    destination = tmp_path / "archive.zip"
    assert download(make_transport(handler), destination) == len(body)
    assert destination.read_bytes() == body


def test_download_url_http_error_leaves_no_file(tmp_path):
    destination = tmp_path / "archive.zip"
    with pytest.raises(XdrApiError):
        download(make_transport(lambda request: httpx.Response(404, text="gone")), destination)
    assert not destination.exists()


def test_download_url_network_error_removes_partial_file(tmp_path):
    class BrokenAsync(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("connection reset")

    destination = tmp_path / "archive.zip"
    with pytest.raises(XdrTransportError, match="connection reset"):
        download(make_transport(lambda request: httpx.Response(200, stream=BrokenAsync())), destination)
    assert not destination.exists()


class FakeTransport:
    def __init__(self, statuses, reasons=None):
        self.statuses = list(statuses)
        self.reasons = reasons or {}
        self.downloads = []

    async def post_json(self, uri, params):
        if uri == "endpoints/file_retrieval":
            return {"reply": {"action_id": 7}}
        if uri == "actions/get_action_status":
            return {"reply": {"data": {"e1": self.statuses.pop(0)}, "errorReasons": self.reasons}}
        if uri == "actions/file_retrieval_details":
            return {"reply": {"data": {"e1": "https://dl.example/f"}}}
        raise AssertionError(uri)

    async def download_url(self, url, destination):
        self.downloads.append((url, destination))
        return 42

    async def post_bytes_url(self, url):
        return b"zip"


class FakeClient:
    def __init__(self, transport):
        self.transport = transport


def test_retrieve_to_file_waits_then_streams(tmp_path):
    transport = FakeTransport(["PENDING", "COMPLETED_SUCCESSFULLY"])
    service = FilesService(FakeClient(transport))
    size = asyncio.run(
        service.retrieve_to_file("e1", ["/etc/hosts"], tmp_path / "a.zip", poll_interval_seconds=0)
    )
    assert size == 42
    assert transport.downloads == [("https://dl.example/f", tmp_path / "a.zip")]


def test_retrieve_still_returns_bytes():
    service = FilesService(FakeClient(FakeTransport(["COMPLETED_SUCCESSFULLY"])))
    assert asyncio.run(service.retrieve("e1", ["/etc/hosts"], poll_interval_seconds=0)) == b"zip"


def failed(description):
    return FakeTransport(["FAILED"], {"e1": {"missing_files": [{"path": "/x", "description": description}]}})


def test_retrieve_reports_size_limit(tmp_path):
    service = FilesService(FakeClient(failed("Maximum file size reached")))
    with pytest.raises(XdrFileTooLargeError, match="FAILED: Maximum file size reached"):
        asyncio.run(service.retrieve_to_file("e1", ["/x"], tmp_path / "a.zip", poll_interval_seconds=0))


def test_retrieve_reports_other_failures(tmp_path):
    service = FilesService(FakeClient(failed("File not found")))
    with pytest.raises(XdrValidationError, match="FAILED: File not found") as excinfo:
        asyncio.run(service.retrieve_to_file("e1", ["/x"], tmp_path / "a.zip", poll_interval_seconds=0))
    assert not isinstance(excinfo.value, XdrFileTooLargeError)
