from .errors import (
    XdrApiError,
    XdrAuthError,
    XdrError,
    XdrNotFoundError,
    XdrOperationTimeoutError,
    XdrReadOnlyError,
    XdrTransportError,
    XdrValidationError,
)
from .models import XdrClientOptions, XdrCredentials, XdrTenant

__all__ = [
    "XdrClient",
    "XdrClientOptions",
    "XdrCredentials",
    "XdrTenant",
    "XdrError",
    "XdrAuthError",
    "XdrTransportError",
    "XdrApiError",
    "XdrValidationError",
    "XdrNotFoundError",
    "XdrOperationTimeoutError",
    "XdrReadOnlyError",
]


def __getattr__(name):
    if name == "XdrClient":
        from .client import XdrClient

        return XdrClient
    raise AttributeError(name)
