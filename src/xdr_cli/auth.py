import hashlib
import secrets
import string
from datetime import datetime, timezone
from typing import Dict


_NONCE_CHARS = string.ascii_letters + string.digits


def generate_auth_headers(api_key_id: str, api_key_hash: str) -> Dict[str, str]:
    nonce = "".join(secrets.choice(_NONCE_CHARS) for _ in range(64))
    timestamp = int(datetime.now(timezone.utc).timestamp()) * 1000
    auth_key = ("%s%s%s" % (api_key_hash, nonce, timestamp)).encode("utf-8")
    authorization = hashlib.sha256(auth_key).hexdigest()
    return {
        "x-xdr-timestamp": str(timestamp),
        "x-xdr-nonce": nonce,
        "x-xdr-auth-id": str(api_key_id),
        "Authorization": authorization,
    }
