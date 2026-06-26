from xdr_cli.errors import (
    XdrApiError,
    XdrError,
    XdrNotFoundError,
    XdrOperationTimeoutError,
    XdrReadOnlyError,
    XdrValidationError,
)
from xdr_cli.output.envelope import (
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    error_code_for,
    exit_code_for,
)


def test_not_found_maps_to_exit_4():
    exc = XdrNotFoundError("nope")
    assert error_code_for(exc) == "ENDPOINT_NOT_FOUND"
    assert exit_code_for(exc) == EXIT_NOT_FOUND


def test_validation_and_readonly_map_to_usage_exit():
    assert exit_code_for(XdrValidationError("x")) == EXIT_USAGE
    assert error_code_for(XdrValidationError("x")) == "VALIDATION"
    assert exit_code_for(XdrReadOnlyError("x")) == EXIT_USAGE
    assert error_code_for(XdrReadOnlyError("x")) == "READ_ONLY"


def test_timeout_and_api_error_map_to_generic_error_exit():
    assert exit_code_for(XdrOperationTimeoutError("x")) == EXIT_ERROR
    assert error_code_for(XdrOperationTimeoutError("x")) == "TIMEOUT"
    assert exit_code_for(XdrApiError("uri", 500, "Server Error", "boom")) == EXIT_ERROR
    assert error_code_for(XdrApiError("uri", 500, "Server Error", "boom")) == "API_ERROR"


def test_unmapped_error_falls_back_to_generic():
    exc = XdrError("something")
    assert error_code_for(exc) == "ERROR"
    assert exit_code_for(exc) == EXIT_ERROR


def test_exit_codes_are_within_posix_byte_range():
    # Regression: the old sys.exit(404) wrapped to 148.
    for code in (EXIT_USAGE, EXIT_NOT_FOUND, EXIT_ERROR):
        assert 0 <= code <= 255


def test_api_error_message_includes_status_and_body():
    exc = XdrApiError("endpoints/isolate", 403, "Forbidden", "no access")
    assert "403" in str(exc)
    assert "no access" in str(exc)
