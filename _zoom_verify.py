"""Webhook verification for Zoom chatbot events.
Modern Zoom Marketplace Chatbot apps authenticate inbound webhooks with a
single **Secret Token** (Features → Access), which is used two ways:

  1. HMAC signature on every request. Zoom sends headers:
        x-zm-request-timestamp
        x-zm-signature            (format: "v0=<hex hmac>")
     where the HMAC is SHA-256 over ``v0:{timestamp}:{rawBody}`` keyed with the
     secret token.

  2. ``endpoint.url_validation`` handshake, performed once when you save the
     webhook URL. Zoom POSTs
        {"event": "endpoint.url_validation",
         "payload": {"plainToken": "<token>"}}
     and expects
        {"plainToken": "<token>",
         "encryptedToken": "<base64 HMAC-SHA256(secret, plainToken)>"}

Zoom's older "Verification Token" (a plaintext shared secret sent in the
``Authorization`` header) is NOT supported — it is weaker than the signature
mechanism, and Signature is the only webhook-auth path the current Marketplace
offers, so supporting it added confusion with no real-world compatibility
benefit.

All of this was validated against the real Zoom Marketplace in the standalone
``zoom_bridge`` toolkit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

_OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"


def compute_encrypted_token(secret_token: str, plain_token: str) -> str:
    """encryptedToken = base64( HMAC-SHA256(secret_token, plain_token) )."""
    digest = hmac.new(secret_token.encode(), plain_token.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_hmac_signature(
    timestamp: str, signature: str, raw_body: bytes, secret_token: str
) -> bool:
    """Verify the ``x-zm-signature`` header against the raw request body."""
    if not (timestamp and signature and secret_token):
        return False
    message = f"v0:{timestamp}:{raw_body.decode('utf-8', 'replace')}".encode()
    digest = hmac.new(secret_token.encode(), message, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def verify_request(headers: dict, raw_body: bytes, secret_token: str) -> tuple[bool, str]:
    """Decide whether a webhook request is authentic.

    Returns ``(ok, method_or_reason)``. There is exactly one auth path:
    a valid ``x-zm-signature`` HMAC keyed with ``secret_token``. Anything else
    is rejected. ``connect()`` enforces that ``secret_token`` is non-empty, so
    reaching this function with an empty secret is a programmer error.
    """
    zm_sig = headers.get("x-zm-signature") or headers.get("X-Zm-Signature")
    zm_ts = headers.get("x-zm-request-timestamp") or headers.get("X-Zm-Request-Timestamp")

    if zm_sig:
        ok = verify_hmac_signature(zm_ts or "", zm_sig, raw_body, secret_token)
        return ok, ("hmac-signature" if ok else "hmac-signature-MISMATCH")

    # No signature header → not a legitimate Zoom request.
    return False, "signature-MISSING"
