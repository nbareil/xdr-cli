import os
import stat

import pytest

from xdr_cli.xdr_cli import _load_auth, _save_auth


def test_save_auth_is_owner_only_readable(tmp_path):
    path = str(tmp_path / "auth.json")
    _save_auth({"demo": {"base_url": "x", "api_key_id": 1, "api_key_hash": "h"}}, path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_save_then_load_roundtrip(tmp_path):
    path = str(tmp_path / "auth.json")
    tenants = {"a": {"base_url": "u", "api_key_id": 7, "api_key_hash": "hh"}}
    _save_auth(tenants, path)
    assert _load_auth(path) == tenants


def test_save_auth_overwrites_atomically(tmp_path):
    path = str(tmp_path / "auth.json")
    _save_auth({"a": {"base_url": "1", "api_key_id": 1, "api_key_hash": "1"}}, path)
    _save_auth({"b": {"base_url": "2", "api_key_id": 2, "api_key_hash": "2"}}, path)
    assert list(_load_auth(path)) == ["b"]
    # No temp files left behind in the directory.
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".xdr-cli-auth-")]
    assert leftovers == []


def test_load_auth_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_auth(str(tmp_path / "nope.json"))
