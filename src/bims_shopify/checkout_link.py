"""HMAC-signed, expiring tokens for buyer-facing payment links.

Two flavours share one format (``<payload>.<signature>``, both base64url):

* navigation tokens bind (tenant slug, order id) and carry no process id;
* checkout tokens additionally carry the Bancard ``process_id`` of the
  single_buy that the page must render.

The signature is verified (constant time) before the payload is parsed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bims_shopify.config import Settings

_VERSION = 1


class CheckoutTokenError(Exception):
    """The token is invalid. ``expired`` is set only for a validly signed, expired token."""

    def __init__(self, message: str = "invalid or expired link", *, expired: bool = False) -> None:
        super().__init__(message)
        self.expired = expired


@dataclass(frozen=True)
class CheckoutClaims:
    slug: str
    order_id: str
    process_id: str | None
    expires_at: datetime


def derive_link_secret(settings: Settings) -> str:
    """Explicit secret if set, else derived from ``fernet_key``; empty if neither."""
    if settings.checkout_link_secret:
        return settings.checkout_link_secret
    if settings.fernet_key:
        return hashlib.sha256(f"checkout-link:{settings.fernet_key}".encode()).hexdigest()
    return ""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not text.isascii():
        raise CheckoutTokenError
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise CheckoutTokenError from exc


def _signature(secret: str, body: str) -> str:
    return _b64encode(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def _require_secret(secret: str) -> None:
    if not secret:
        raise ValueError("checkout link signing secret is not configured")


def _sign(
    secret: str,
    slug: str,
    order_id: str,
    process_id: str | None,
    ttl: timedelta,
    now: datetime | None,
) -> str:
    _require_secret(secret)
    issued = now or datetime.now(UTC)
    claims: dict[str, object] = {
        "v": _VERSION,
        "slug": slug,
        "oid": str(order_id),
        "exp": int((issued + ttl).timestamp()),
    }
    if process_id is not None:
        claims["pid"] = str(process_id)
    body = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_signature(secret, body)}"


def sign_navigation_token(
    secret: str, slug: str, order_id: str, *, ttl: timedelta, now: datetime | None = None
) -> str:
    return _sign(secret, slug, order_id, None, ttl, now)


def sign_checkout_token(
    secret: str,
    slug: str,
    order_id: str,
    process_id: str,
    *,
    ttl: timedelta,
    now: datetime | None = None,
) -> str:
    return _sign(secret, slug, order_id, process_id, ttl, now)


def verify_token(
    secret: str,
    token: str,
    slug: str,
    *,
    require_process_id: bool = False,
    now: datetime | None = None,
) -> CheckoutClaims:
    _require_secret(secret)
    if not isinstance(token, str) or token.count(".") != 1 or not token.isascii():
        raise CheckoutTokenError
    body, signature = token.split(".")
    if not hmac.compare_digest(_signature(secret, body).encode("ascii"), signature.encode("ascii")):
        raise CheckoutTokenError
    try:
        data = json.loads(_b64decode(body))
        if data["v"] != _VERSION:
            raise CheckoutTokenError
        claims = CheckoutClaims(
            slug=str(data["slug"]),
            order_id=str(data["oid"]),
            process_id=None if "pid" not in data else str(data["pid"]),
            expires_at=datetime.fromtimestamp(int(data["exp"]), UTC),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckoutTokenError from exc
    if claims.slug != slug:
        raise CheckoutTokenError
    if require_process_id and claims.process_id is None:
        raise CheckoutTokenError
    if (now or datetime.now(UTC)) > claims.expires_at:
        raise CheckoutTokenError(expired=True)
    return claims
