import hashlib

from xdr_cli.auth import generate_auth_headers


def test_authorization_is_sha256_of_hash_nonce_timestamp():
    headers = generate_auth_headers("42", "secrethash")
    nonce = headers["x-xdr-nonce"]
    timestamp = headers["x-xdr-timestamp"]
    expected = hashlib.sha256(("secrethash" + nonce + timestamp).encode("utf-8")).hexdigest()
    assert headers["Authorization"] == expected


def test_header_keys_and_auth_id():
    headers = generate_auth_headers("42", "secrethash")
    assert set(headers) == {"x-xdr-timestamp", "x-xdr-nonce", "x-xdr-auth-id", "Authorization"}
    assert headers["x-xdr-auth-id"] == "42"


def test_nonce_is_64_alphanumeric_chars():
    nonce = generate_auth_headers("1", "h")["x-xdr-nonce"]
    assert len(nonce) == 64
    assert nonce.isalnum()


def test_timestamp_is_epoch_milliseconds():
    timestamp = generate_auth_headers("1", "h")["x-xdr-timestamp"]
    # seconds * 1000 -> always a positive integer ending in 000
    assert timestamp.isdigit()
    assert int(timestamp) > 0
    assert timestamp.endswith("000")


def test_nonce_differs_between_calls():
    a = generate_auth_headers("1", "h")["x-xdr-nonce"]
    b = generate_auth_headers("1", "h")["x-xdr-nonce"]
    assert a != b
