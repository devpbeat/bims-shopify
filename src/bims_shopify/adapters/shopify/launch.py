"""Verification of Shopify admin app-launch requests (``application_url`` hits).

When an operator opens a non-embedded app from the Shopify admin, Shopify
navigates to the app URL with signed query parameters (``hmac``, ``host``,
``session``, ``shop``, ``timestamp``). A valid signature proves the request
was produced by Shopify for this app's client secret.

Why this is not ``oauth.verify_callback_hmac``: the signing algorithm is the
same ("admin" signator in Shopify's reference library: drop ``hmac``, sort the
rest, join ``key=value`` with ``&``, HMAC-SHA256 hex), but a login path needs
extra guarantees that the OAuth helper does not give: repeated keys are
ambiguous and must be rejected, an empty secret must never validate (an HMAC
keyed with ``""`` is forgeable by anyone), the signed ``timestamp`` must be
fresh, and the ``shop`` handle must be a strict ``*.myshopify.com`` host. The
reference library also signs the percent-encoded form of the values, so both
the raw and encoded canonical strings are accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Sequence
from urllib.parse import quote

# A Shopify shop handle starts with an alphanumeric and never contains dots,
# slashes, or whitespace; the whole host must end in `.myshopify.com`.
_SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_MAX_SHOP_LENGTH = 255

LAUNCH_MAX_AGE_SECONDS = 120
LAUNCH_MAX_FUTURE_SKEW_SECONDS = 30


def normalize_shop(value: str) -> str:
    """Canonicalize a shop host for lookup (trim + lowercase)."""
    return (value or "").strip().lower()


def is_valid_launch_shop(shop: str) -> bool:
    """True only for a well-formed, already-normalized ``*.myshopify.com`` host."""
    return len(shop) <= _MAX_SHOP_LENGTH and bool(_SHOP_RE.match(shop))


def _encode(value: str) -> str:
    # Mirrors URLSearchParams serialization (spaces as %20, `*-._` kept).
    return quote(value, safe="*").replace("~", "%7E")


def _canonical_messages(params: dict[str, str]) -> tuple[str, str]:
    items = sorted(params.items())
    raw = "&".join(f"{k}={v}" for k, v in items)
    encoded = "&".join(f"{_encode(k)}={_encode(v)}" for k, v in items)
    return raw, encoded


def verify_launch_hmac(query_items: Sequence[tuple[str, str]], client_secret: str) -> bool:
    """Verify the ``hmac`` of an app-launch request in constant time."""
    if not client_secret:
        return False
    keys = [k for k, _ in query_items]
    if len(keys) != len(set(keys)):
        return False
    params = dict(query_items)
    provided = params.pop("hmac", "")
    if not provided:
        return False

    secret = client_secret.encode("utf-8")
    for message in _canonical_messages(params):
        digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(digest, provided):
            return True
    return False


def is_fresh_timestamp(
    value: str,
    *,
    now: float | None = None,
    max_age: int = LAUNCH_MAX_AGE_SECONDS,
    max_future_skew: int = LAUNCH_MAX_FUTURE_SKEW_SECONDS,
) -> bool:
    """True when ``value`` is a unix timestamp inside the replay window."""
    if not value.isascii() or not value.isdigit():
        return False
    age = (now if now is not None else time.time()) - int(value)
    return -max_future_skew <= age <= max_age
