"""Verification of Shopify checkout UI extension session tokens.

The token is an HS256 JWT signed with the app's client secret. Beyond the
signature, expiry and not-before time, the audience must be this app's client
id and the issuer and destination hostnames must agree. The returned shop
domain is the only identity the caller may trust.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt

_ALGORITHM = "HS256"
_LEEWAY_SECONDS = 10
_REQUIRED_CLAIMS = ["exp", "nbf", "aud", "iss", "dest"]


class SessionTokenError(Exception):
    """The session token is missing, forged, expired or addressed elsewhere."""


@dataclass(frozen=True)
class SessionClaims:
    shop_domain: str
    """Normalized (trimmed, lowercase) hostname of the shop the token was issued for."""


def normalize_shop_domain(value: str) -> str:
    return (value or "").strip().lower()


def _hostname(url: object) -> str:
    """Hostname from either an https URL or a bare host.

    Shopify is not consistent across claims: checkout UI extension tokens send
    ``iss`` as a full URL (``https://shop.myshopify.com/checkouts``) but ``dest``
    as a bare hostname (``shop.myshopify.com``). Requiring a scheme on both
    rejected every real token with a 401.
    """
    if not isinstance(url, str):
        raise SessionTokenError("claim is not a URL")
    raw = url.strip()
    if "//" not in raw:
        host = normalize_shop_domain(raw.split("/", 1)[0])
        if not host or " " in host:
            raise SessionTokenError("claim is not a hostname")
        return host
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        raise SessionTokenError("claim is not an https URL")
    return normalize_shop_domain(parts.hostname)


def verify_session_token(token: str, *, client_secret: str, client_id: str) -> SessionClaims:
    if not client_secret or not client_id:
        raise SessionTokenError("verification is not configured")
    try:
        claims = jwt.decode(
            token,
            client_secret,
            algorithms=[_ALGORITHM],
            audience=client_id,
            leeway=_LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise SessionTokenError(type(exc).__name__) from exc
    dest = _hostname(claims.get("dest"))
    if _hostname(claims.get("iss")) != dest:
        raise SessionTokenError("issuer and destination differ")
    return SessionClaims(shop_domain=dest)
