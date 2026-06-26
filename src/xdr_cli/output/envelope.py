from __future__ import annotations

import json
from typing import Any

import click

from ..errors import (
    XdrApiError,
    XdrNotFoundError,
    XdrOperationTimeoutError,
    XdrReadOnlyError,
    XdrValidationError,
)

# Stable, documented exit codes. POSIX exit status is a single byte, so codes
# must stay in 0-255 (the old ``sys.exit(404)`` silently became 148).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_NOT_FOUND = 4

# Map library exception types to (machine-readable code, process exit code).
_ERROR_MAP = {
    XdrNotFoundError: ("ENDPOINT_NOT_FOUND", EXIT_NOT_FOUND),
    XdrValidationError: ("VALIDATION", EXIT_USAGE),
    XdrReadOnlyError: ("READ_ONLY", EXIT_USAGE),
    XdrOperationTimeoutError: ("TIMEOUT", EXIT_ERROR),
    XdrApiError: ("API_ERROR", EXIT_ERROR),
}


def error_code_for(exc: Exception) -> str:
    for exc_type, (code, _exit) in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            return code
    return "ERROR"


def exit_code_for(exc: Exception) -> int:
    for exc_type, (_code, exit_code) in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            return exit_code
    return EXIT_ERROR


def _json_enabled(ctx: click.Context) -> bool:
    return bool((ctx.obj or {}).get("json"))


def emit(ctx: click.Context, data: Any) -> None:
    """Print a successful result.

    With ``--json`` the data is wrapped in a ``{"ok": true, "data": ...}``
    envelope on a single line. Without it, callers should print their own
    legacy output and not call this (see :func:`emit_default`).
    """
    click.echo(json.dumps({"ok": True, "data": data}))


def emit_error(ctx: click.Context, code: str, message: str, exit_code: int = EXIT_ERROR) -> None:
    """Print a structured error and exit with ``exit_code``."""
    if _json_enabled(ctx):
        click.echo(json.dumps({"ok": False, "error": {"code": code, "message": message}}))
    else:
        click.echo(message, err=True)
    raise SystemExit(exit_code)
