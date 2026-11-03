"""Request signing for the portal's bulk export endpoint.

Reverse-engineered from the portal's Transformers page bundle: the browser fetches a
shared secret from ``GET /portal/keys`` and signs

    "{METHOD}\\n{PATH}\\n{QUERY}\\n{UNIX_SECONDS}"

with HMAC-SHA256, sending the hex digest in ``x-signature`` and the timestamp in
``x-timestamp``. The server accepts timestamps within roughly ±5 minutes.
"""

from __future__ import annotations

import hashlib
import hmac


def canonical_string(method: str, path: str, query: str, timestamp: str) -> str:
    return "\n".join([method.upper(), path, query, timestamp])


def sign(secret: str, method: str, path: str, query: str, timestamp: str) -> str:
    message = canonical_string(method, path, query, timestamp).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, method: str, path: str, query: str, unix_seconds: float) -> dict[str, str]:
    ts = str(int(unix_seconds))
    return {"x-timestamp": ts, "x-signature": sign(secret, method, path, query, ts)}
