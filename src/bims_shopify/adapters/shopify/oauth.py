"""Shopify OAuth authorization code grant: authorize URL, callback HMAC, token exchange."""
from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import urlencode

import httpx

SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.myshopify\.com$")

DEFAULT_SCOPES = (
    "read_products,write_products,read_inventory,write_inventory,"
    "read_orders,write_orders,read_locations"
)

_PRIMARY_LOCATION_QUERY = """
query PrimaryLocation {
  locations(first: 1, query: "active:true") {
    nodes { id name }
  }
}
"""


def is_valid_shop_domain(shop: str) -> bool:
    return bool(SHOP_DOMAIN_RE.match(shop))


def build_authorize_url(
    *,
    shop: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: str = DEFAULT_SCOPES,
) -> str:
    """Build the `/admin/oauth/authorize` redirect URL to start the install flow."""
    params = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"


def verify_callback_hmac(query_params: dict[str, str], client_secret: str) -> bool:
    """Verify the callback's `hmac` param.

    Per Shopify's authorization code grant spec: drop `hmac` (and `signature`,
    a legacy alias) from the query string, sort the remaining params
    alphabetically by key, join as `key=value` pairs with `&`, then compute
    HMAC-SHA256 (hex digest, NOT base64 — unlike webhook body verification)
    over that string using the app's client secret.
    """
    provided = query_params.get("hmac", "")
    if not provided:
        return False

    filtered = {k: v for k, v in query_params.items() if k not in ("hmac", "signature")}
    message = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    digest = hmac.new(client_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


class ShopifyOAuthClient:
    """Handles the token exchange and post-install setup calls to Shopify."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def exchange_code_for_token(
        self, *, shop: str, client_id: str, client_secret: str, code: str
    ) -> dict:
        """POST to `/admin/oauth/access_token`, returning the offline-token payload."""
        response = await self._client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
            },
        )
        response.raise_for_status()
        return response.json()

    async def fetch_primary_location_id(self, *, shop: str, access_token: str) -> str:
        """Fetch the shop's primary/active location id via the Admin GraphQL API."""
        response = await self._client.post(
            f"https://{shop}/admin/api/2025-07/graphql.json",
            json={"query": _PRIMARY_LOCATION_QUERY},
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        nodes = (((payload.get("data") or {}).get("locations") or {}).get("nodes")) or []
        return nodes[0]["id"] if nodes else ""
