from typing import Optional


class XdrError(Exception):
    """Base error for xdr_cli library."""


class XdrValidationError(XdrError):
    pass


class XdrAuthError(XdrError):
    pass


class XdrTransportError(XdrError):
    pass


class XdrApiError(XdrError):
    # Seconds from a Retry-After header, when the server sent one.
    retry_after: Optional[float] = None

    def __init__(self, endpoint: str, status_code: int, reason: str, response_text: str):
        self.endpoint = endpoint
        self.status_code = status_code
        self.reason = reason
        self.response_text = response_text
        super().__init__(
            "%s failed with %s: %s %s" % (endpoint, status_code, reason, response_text)
        )


class XdrNotFoundError(XdrError):
    def __init__(self, message: str, resource: Optional[str] = None):
        self.resource = resource
        super().__init__(message)


class XdrOperationTimeoutError(XdrError):
    pass


class XdrReadOnlyError(XdrError):
    """Raised when a mutating operation is attempted in read-only mode."""
