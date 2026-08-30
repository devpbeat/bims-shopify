"""HTTP client for the BIMS Core REST API."""
from __future__ import annotations

from typing import Any

import httpx

USER_AGENT = "bims-shopify-sync/0.1 (+https://github.com/devpbeat/bims-shopify)"


class BIMSAPIError(RuntimeError):
    """Raised when BIMS answers with an error envelope (status == "error")."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"BIMS error {code}: {message}")
        self.code = code
        self.message = message

from bims_shopify.domain.tenant import Tenant

from .form_encoding import encode_cakephp_form


class BIMSClient:
    """Thin async wrapper around the BIMS legacy REST API.

    Handles authentication and the two write encodings documented for
    /api/sales/add.json and /api/sales/edit.json (form-urlencoded and JSON),
    plus the form-urlencoded encoding used by every other legacy write
    endpoint.
    """

    def __init__(self, tenant: Tenant, http_client: httpx.AsyncClient | None = None) -> None:
        self._tenant = tenant
        self._client = http_client or httpx.AsyncClient(
            base_url=tenant.bims_base_url,
            headers={
                "Authorization": tenant.bims_auth_header,
                "Accept": "application/json",
                # BIMS sits behind Cloudflare, which rejects generic HTTP-library user agents.
                "User-Agent": USER_AGENT,
            },
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        """Decode a BIMS envelope, treating body-level error codes as failures.

        BIMS returns HTTP 200 even for auth/permission errors and signals them
        through ``status``/``code`` in the JSON body.
        """
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("status") == "error":
            raise BIMSAPIError(str(body.get("code")), body.get("message") or "BIMS request failed")
        return body

    def _drop_session_cookies(self) -> None:
        """Discard cookies BIMS sets on every response.

        BIMS starts a PHP session on each call and rejects requests that carry
        both a session cookie and an API key ("Session ID no coincide con la
        cookie de sesion activa"). Stateless API-key auth must stay cookie-free.
        """
        self._client.cookies.clear()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._drop_session_cookies()
        response = await self._client.get(path, params=params)
        return self._parse(response)

    async def post_form(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a nested payload encoded with the CakePHP data[Model][field] convention."""
        encoded = encode_cakephp_form(payload)
        self._drop_session_cookies()
        response = await self._client.post(path, data=encoded)
        return self._parse(response)

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a payload as raw JSON (only supported by sales/add.json and sales/edit.json)."""
        self._drop_session_cookies()
        response = await self._client.post(path, json=payload)
        return self._parse(response)

    # -- Products -------------------------------------------------------
    async def list_products(self, **params: Any) -> dict[str, Any]:
        """GET /api/products/index.json. Supports ``last_update`` for incremental pulls."""
        return await self.get("/api/products/index.json", params=params)

    async def list_deleted_products(self, last_update: str) -> dict[str, Any]:
        """GET /api/products/deleted.json?last_update=... (required filter)."""
        return await self.get("/api/products/deleted.json", params={"last_update": last_update})

    async def stock_fenicio(
        self, skus: list[str], warehouse_ids: list[int] | None = None, request_id: str | None = None
    ) -> dict[str, Any]:
        """POST /api/products_stocks/stock_fenicio.json.

        Returns aggregated stock per SKU, optionally scoped to ``warehouse_ids``.
        Read-only: only reads stock, never writes it (despite living under the
        ``products_stocks`` namespace).
        """
        payload: dict[str, Any] = {"_idSolicitud": request_id, "skus": skus}
        if warehouse_ids is not None:
            payload["warehouse_ids"] = warehouse_ids
        body = await self.post_json("/api/products_stocks/stock_fenicio.json", payload)
        # This endpoint uses its own uppercase "OK"/"ERROR" envelope instead of
        # the lowercase "ok"/"error" convention handled by ``_parse``.
        if str(body.get("status", "")).upper() == "ERROR":
            raise BIMSAPIError("stock_fenicio", body.get("mensaje") or "stock_fenicio request failed")
        return body

    # -- Sales ------------------------------------------------------------
    async def add_sale(self, sale_payload: dict[str, Any], as_json: bool = True) -> dict[str, Any]:
        if as_json:
            return await self.post_json("/api/sales/add.json", sale_payload)
        return await self.post_form("/api/sales/add.json", sale_payload)

    async def edit_sale(self, sale_payload: dict[str, Any], as_json: bool = True) -> dict[str, Any]:
        if as_json:
            return await self.post_json("/api/sales/edit.json", sale_payload)
        return await self.post_form("/api/sales/edit.json", sale_payload)

    async def verify_sale_synced(self, sale_fields: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form(
            "/api/sales/verify_synced.json", {"Sale": sale_fields}
        )

    # -- Purchase orders / restocking -------------------------------------
    async def create_purchase_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form("/api/purchase_orders/add.json", payload)

    async def request_restocking(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form("/api/restockings/request.json", payload)

    # -- Stock (undocumented / tenant-configurable field schema) ----------
    async def update_stock(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to one of the generic, untyped stock write endpoints.

        The BIMS spec does not enumerate field names for stock writes
        (stocks/add|edit|update, invads/add, products_stocks/stock_fenicio,
        service_tools/stocks). Callers must supply an already-built payload
        derived from ``Tenant.field_mappings``.
        """
        return await self.post_form(endpoint, payload)

    # -- Bancard / BIMS Pay (generic, untyped) -----------------------------
    async def bancard_add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form("/api/bancard_transactions/add.json", payload)

    async def bims_pay_create_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form("/api/bims_pay/create_link.json", payload)

    async def bims_pay_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.post_form("/api/bims_pay/cancel.json", payload)

    async def lookup_bancard_transaction(self, shop_process_id: str) -> dict[str, Any]:
        """GET /api/bancard_transactions/lookup/{shopProcessId}.json.

        Used to verify a Bancard payment's real status directly with BIMS
        instead of trusting the (spoofable) callback body.
        """
        return await self.get(f"/api/bancard_transactions/lookup/{shop_process_id}.json")
