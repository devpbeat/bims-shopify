"""Shopify webhook HMAC-SHA256 verification."""
from __future__ import annotations

import base64
import hashlib
import hmac


def verify_shopify_hmac(secret: str, body: bytes, provided_hmac_b64: str) -> bool:
    """Verify the X-Shopify-Hmac-Sha256 header against the raw request body.

    Shopify computes HMAC-SHA256 over the raw request body using the webhook
    secret as key, then base64-encodes the digest.
    """
    if not provided_hmac_b64:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, provided_hmac_b64)
