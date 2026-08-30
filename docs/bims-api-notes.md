# BIMS Core REST API — Integration Notes

Source: `bims-openapi.json` (OpenAPI 3, title "BIMS Core REST API", v1.0.0). Base server path is `/` (no absolute host in spec).

## Authentication

- Security scheme: `BearerApiKey`, type `apiKey`, `in: header`, header name **`Authorization`**.
- Format: `Authorization: {tenant}_{api_key}` — plain value, no `Bearer` scheme (verified live against in.bims.app; `Bearer ...` is rejected with 401 "No autorizado"). The tenant identifier is embedded as a prefix of the key; no separate `X-Company-Id` / `X-Agency-Id` header exists. Multi-tenant base URL is `https://in.bims.app`.
- Applied globally: `security: [{"BearerApiKey": []}]` on the whole spec (no per-path override observed for the listed endpoints).
- `company_id`, `agency_id`, `posale_id` appear as **body fields** on write endpoints (e.g. `Sale`, `Posale`), not as auth/routing params. One unrelated endpoint (`/api/bims_preinvoices/lookup/{host_name}.json`, not in scope here) uses `host_name` as a path param — this is the only place `host_name` appears; it is not part of standard tenant identification for the endpoints below.

## Write payload convention

Spec description (info.description): most legacy write endpoints expect `application/x-www-form-urlencoded` using CakePHP 1.3-style keys: `data[Model][field]`, `Model[field]`, or nested list keys `data[ModelItem][0][field]`. Only use raw JSON where an operation explicitly documents `application/json` (e.g. `sales/add.json`, `sales/edit.json` support both).

Generic legacy write schema (used for many "add"/"edit" endpoints with no more specific schema defined): `{"type":"object","additionalProperties": true}` — spec does not enumerate exact fields for these; only the encoding convention is documented.

Generic legacy response envelope (used by most simple write/list endpoints):
```json
{"status":"ok|error|not-found|no-input|expired","code":"200","message":"string|null","data":null,"count":0,"last_update":"date-time|null","validationErrors":{}}
```

---

## Products

### GET /api/products/index.json
Query params (all optional): `mode` (string), `limit` (integer), `offset` (integer), `webpos` (bool), `mpos` (bool), `plain` (bool), `addons` (bool), `components` (bool), `only_master` (bool), `only_main` (bool), `full_images` (bool), `v_stock` (bool).
Response 200:
```json
{"status":"ok","code":"200","data":[{"Product":"<Product>"}],"count":0,"model":"Product","last_update":"date-time"}
```
No `modified_since`/`modified-since` param defined for this endpoint in the spec.

### GET /api/products/view.json
Params: `id` (query, required, integer).
Response 200: `{"status":"ok|error|no-input","code":"200","validationErrors":null,"data":{"Product":"<Product>"}}`. 404: standard error envelope.

### POST /api/products/edit.json
Content-type: `application/x-www-form-urlencoded`. Fields:
`data[Product][id]` (int ≥1), `data[Product][name]` (string 1-255), `data[Product][code]` (string ≤80), `data[Product][ptype_id]` (int ≥1), `data[Product][tax_id]` (int ≥1), `data[Product][sell_price]` (float ≥0), `data[Product][enabled]` (bool), `data[Product][sellable]` (bool), `data[Product][stockable]` (bool), `data[Product][images_url][0]` (string, uri).
Response 200: `{"status":"ok|error|no-input","code":"200","validationErrors":null,"data":{"Product":"<Product>"}}`.

### GET /api/products/deleted.json
Params: `last_update` (query, **required**, string) — this is the modified-since-style filter for this endpoint (returns identifiers of products deleted since `last_update`).
Response 200: `{"status":"ok|error|not-found|no-input","code":"200","message":"string|null","data":null}` (data shape not further typed).

### Product schema (`components.schemas.Product`)
Required: `id`, `name`.
Fields: `id` (int≥1), `code` (string≤80, nullable), `code2` (string≤80, nullable), `name` (string 1-255), `ptype_id` (int≥1, nullable), `tax_id` (int≥1, nullable), `sell_price` (float, nullable), `buy_price` (float, nullable), `enabled` (bool, nullable), `sellable` (bool, nullable), `stockable` (bool, nullable), `image` (string, nullable), `Availability` (object, nullable, `additionalProperties: true` — untyped free-form stock/availability sub-object).

No standalone `Stock` schema is defined anywhere in `components.schemas`.

---

## Stock / availability endpoints

### GET /api/products_stocks/stock_fenicio.json
Query: `limit`, `offset` (integers, optional), `plain` (bool, optional).
Response: generic legacy envelope (`status`, `code`, `message`, `data` untyped/nullable, `count`, `last_update`, `validationErrors`). 400/401 standard error envelope.

### GET /api/service_tools/stocks.json
Same param set (`limit`, `offset`, `plain`) and same generic response envelope as above.

### POST /api/stocks/add.json, /api/stocks/edit.json, /api/stocks/update.json
Content-type: `application/x-www-form-urlencoded`, body schema is the generic `{"type":"object","additionalProperties":true}` (CakePHP `data[Model][field]` convention noted in description, but exact `Stock` model fields are **not enumerated** in the spec — none of `add`/`edit`/`update` document specific keys). Response: generic legacy envelope.

### POST /api/invads/add.json (inventory adjustment)
Same pattern: `application/x-www-form-urlencoded`, generic untyped object body (no explicit `InvAd` field list in spec), generic response envelope.

---

## Sales

### POST /api/sales/add.json and POST /api/sales/edit.json
Both support two content types:

**`application/x-www-form-urlencoded`** (CakePHP form fields):
- `data[Sale][id]` (int≥1), `data[Sale][_id]` (string≤80 — client-generated idempotency key), `data[Sale][posale_id]` (int≥1), `data[Sale][agency_id]` (int≥1), `data[Sale][company_id]` (int≥1), `data[Sale][contact_id]` (int≥1), `data[Sale][stamping_id]` (int≥1), `data[Sale][currency_id]` (int≥1), `data[Sale][status]` (enum: `pending`, `approved`, `preorder`, `preorder_confirmed`, `preorder_pending`), `data[Sale][issue_date]` (date), `data[Sale][invoice_number]` (string≤50), `data[Sale][amount]` (float≥0), `data[Sale][billed]` (bool — "true=fiscal, false=no fiscal; if omitted, backend assumes true").
- Line items: `data[SalesProduct][0][product_id]` (int≥1), `[quantity]` (float≥0.0001), `[price]` (float≥0), `[discount_amount]` (float≥0, default 0), `[currency_id]` (int≥1).
- Payments: `data[SalesPaymentMethod][0][payment_method_id]` (int≥1), `[amount]` (float≥0), `[delayed]` (bool, default false).

**`application/json`** body: `{"Sale": {...same fields as above without data[] wrapper...}, "SalesProduct": [SaleProduct...], "SalesPaymentMethod": [SalePaymentMethod...]}`.

Response 200 is `oneOf`:
- `SaleMutationResponse`: `{status, code, message, idempotent, retry_count, data}` (required: status, code, data) — success/idempotent-replay case.
- `SaleDeadlockRetryableErrorResponse`: `{status, code, retryable, retry_after_ms, operation_id, message}` (required: status, code, retryable, retry_after_ms, message) — DB deadlock, safe to retry.
- `SalePersistenceUnconfirmedErrorResponse`: same shape as above — write outcome unknown/unconfirmed, caller must verify (e.g. via `sales/verify_synced.json`) before blindly retrying.

400/401: standard error envelope. 422: `BimsErrorResponse` (`status`, `code`, `message`) plus `validationErrors` (object).

### Sale schema
Required: `id`. Fields: `id`, `_id`, `contact_id`, `posale_id`, `agency_id`, `company_id`, `stamping_id`, `currency_id`, `invoice_number`, `issue_date`, `status`, `amount`, `cost`, `void`, `billed`, `pdf_invoice_url`.

### SaleProduct schema
Required: `product_id`, `quantity`, `price`. Fields: `id`, `_id`, `product_id`, `quantity`, `price`, `discount_amount`, `currency_id`, `notes`.

### SalePaymentMethod schema
Required: `amount`. Fields: `id`, `payment_method_id`, `amount`, `currency_id`, `delayed`, `credit`, `manual_pnotes`.

### POST /api/sales/cancel/{id}.json
Path param `id` (required, integer). No body documented. Response 200: `{"status":"ok|error|not-found|no-input","code":"200","message":"string|null","data":null}`. 400/401/404 standard error envelope.

### POST /api/sales/verify_synced.json
Content-type: `application/x-www-form-urlencoded`. Fields: `data[Sale][invoice_number]` (string 1-50), `data[Sale][posale_billing_code]` (string 1-50), `data[Sale][agency_billing_code]` (string 1-50), `data[Sale][stamping_code]` (string 1-50), `data[Sale][stamping_id]` (int≥1), `data[Sale][billed]` (bool).
Response 200: `{"status":"ok|no-input","code":"200","data":{"already_synced": bool, "Sale":"<Sale>"}}` — this is the recommended way to reconcile a Sale write whose outcome was unconfirmed (`SalePersistenceUnconfirmedErrorResponse`).

---

## Purchase orders / restocking

### POST /api/purchase_orders/add.json
Content-type: `application/x-www-form-urlencoded`. Fields: `data[PurchaseOrder][id]` (int≥1), `data[PurchaseOrder][contact_id]` (int≥1), `data[PurchaseOrder][issue_date]` (date), `data[PurchaseOrder][status]` (string≤40); line items: `data[PurchaseOrderProduct][0][product_id]` (int≥1), `[quantity]` (float≥0.0001), `[price]` (float≥0).
Response 200: `{"status":"ok|error|no-input","code":"200","data":{"PurchaseOrder":"<PurchaseOrder>"}}`.

### PurchaseOrder schema
Required: `id`. Fields: `id`, `contact_id`, `issue_date`, `status`, `amount`. (No standalone `PurchaseOrderProduct` schema defined — its shape is only implied by the `add.json` form fields above: `product_id`, `quantity`, `price`.)

### POST /api/purchase_orders/generate.json
Content-type: `application/x-www-form-urlencoded`, generic untyped body (`additionalProperties: true`, no specific fields documented).
Response 200: `{"status":"ok|error|no-input","code":"200","data":{"PurchaseOrder":"<PurchaseOrder>"}}`.

### POST /api/restockings/request.json
Content-type: `application/x-www-form-urlencoded`, generic untyped body. Response: generic legacy envelope (`status`, `code`, `message`, `data`, `count`, `last_update`, `validationErrors`).

---

## Reference/lookup lists

### GET /api/warehouses/index.json
Query: `limit`, `offset` (int, optional), `plain` (bool, optional). Response: generic legacy envelope.

### GET /api/posales/index.json
Query: `plain` (bool, optional), `set_current_invoice_number` (bool, optional). Response 200: `{"status":"ok","code":"200","data":[{"Posale":"<Posale>"}],"last_update":"date-time"}`.
**Posale schema** (required: `id`): `id`, `name`, `agency_id`, `company_id`, `stamping_id`, `finvoice_id`, `credit_interest`.

### GET /api/sales_payment_methods/index.json
Query: `limit`, `offset`, `plain` (all optional). Response: generic legacy envelope.

### GET /api/currencies/index.json
Query: `limit`, `offset`, `plain` (all optional). Response: generic legacy envelope.

None of the reference-list endpoints above document a `modified_since`/`last_update` filter param — only `products/deleted.json` explicitly requires `last_update`, and several generic envelopes echo `last_update` back in the response (indicating server tracks it) without exposing it as a request filter in this spec.

---

## Contacts

### PUT /api/contacts/edit.json and POST /api/contacts/edit.json (same endpoint accepts both verbs)
Query: `id` (optional, integer) on PUT variant.
Content-type: `application/x-www-form-urlencoded`. Accepts three equivalent key styles (documented explicitly as legacy fallbacks): `data[Contact][field]`, flat `Contact[field]`, and bare `field` (when no model container is sent). Fields: `id` (int≥1), `name` (string 1-255), `document_id` (string≤80), `document_type` (enum: `ci`, `ruc`, `passport`, `dni`, `other`), `emails`, `phones`, `mobile`, `address` (strings), `company_id` (int≥1, `data[Contact]` form only), `enabled_sales` (bool, `data[Contact]` form only).
Response 200: `{"status":"ok|error|not-found|no-input","code":"200","message":"string|null","data":{"Contact":"<Contact>"}}`. 400: `BimsErrorResponse` + `validationErrors` object.

### GET /api/contacts/view.json
Params: `id` (query, required, integer). Response 200: `{"status":"ok|error|not-found|no-input","code":"200","message":"string|null","data":{"Contact":"<Contact>"}}`. 404: standard error envelope.

### Contact schema
Required: `id`, `name`. Fields: `id`, `name`, `document_id`, `document_type`, `emails`, `phones`, `mobile`, `address`, `company_id`, `enabled_sales`, `credit_line`, `total_points`.

---

## Bancard transactions (`/api/bancard_transactions/*`)

No dedicated `BancardTransaction` schema exists in `components.schemas`; all bodies/responses use the generic untyped envelope.

- **POST /api/bancard_transactions/add.json** — `application/x-www-form-urlencoded`, generic untyped body. Response: generic legacy envelope.
- **GET /api/bancard_transactions/view/{id}.json** — path `id` (string, required); query `limit`, `offset`, `plain` (optional). Generic legacy envelope.
- **POST /api/bancard_transactions/edit/{id}.json** — path `id` (string, required); body `application/x-www-form-urlencoded`, generic untyped. Generic legacy envelope.
- **GET /api/bancard_transactions/by_type.json** — query `limit`, `offset`, `plain` (optional). Generic legacy envelope.
- **GET /api/bancard_transactions/lookup/{shopProcessId}.json** — path `shopProcessId` (string, required); query `limit`, `offset`, `plain`. Generic legacy envelope.
- **GET /api/bancard_transactions/pending_old.json** — query `limit`, `offset`, `plain`. Generic legacy envelope.
- **GET /api/bancard_transactions/mark_notified/{id}.json** — path `id` (string, required); query `limit`, `offset`, `plain`. (Note: despite "marking" semantics this is defined as a `GET`, not `POST`, in the spec.) Generic legacy envelope.

## BIMS Pay (`/api/bims_pay/*`)

All endpoints use the same generic untyped request/response pattern (`application/x-www-form-urlencoded` for writes, generic legacy envelope for responses). No dedicated schema is defined for BIMS Pay payment/transaction objects.

- **POST /api/bims_pay/add.json** — create payment, generic untyped body.
- **POST /api/bims_pay/add_old.json** — legacy create-payment variant, generic untyped body.
- **POST /api/bims_pay/cancel.json** — cancel payment, generic untyped body.
- **POST /api/bims_pay/create_link.json** — create payment link, generic untyped body.
- **POST /api/bims_pay/cancel_link.json** — cancel payment link, generic untyped body.
- **GET /api/bims_pay/gw_callback/{gateway}/{test_code}.json** — path `gateway` (string, required), `test_code` (string, required); query `limit`, `offset`, `plain`. This is the payment-gateway webhook/callback endpoint, exposed as `GET`.
- **GET /api/bims_pay/proc_collection.json** — query `limit`, `offset`, `plain`.
- **GET /api/bims_pay/proc_wallet.json** — query `limit`, `offset`, `plain`.
- **GET /api/bims_pay/transaction.json** — query `limit`, `offset`, `plain`.
- **GET /api/bims_pay/get_link_by_transaction_code/{transaction_code}.json** — path `transaction_code` (string, required); query `limit`, `offset`, `plain`.

All bims_pay/bancard endpoints share the same generic error responses:
- 400/401: `{"status":"error|not-found|no-input","code":"400","message":"string"}`.

---

## Common response envelope fields (generic legacy endpoints)

```json
{
  "status": "ok | error | not-found | no-input | expired",
  "code": "200",
  "message": "string|null",
  "data": null,
  "count": 0,
  "last_update": "date-time|null",
  "validationErrors": {}
}
```
This exact shape is reused verbatim across `products_stocks/stock_fenicio`, `service_tools/stocks`, `stocks/add|edit|update`, `invads/add`, `restockings/request`, `warehouses/index`, `sales_payment_methods/index`, `currencies/index`, and all `bancard_transactions/*` and `bims_pay/*` endpoints — none of these enumerate a specific model schema for `data`.

---

## Empirical findings (live-verified, tenant mystore, 2026-08-29)

### Working per-warehouse stock endpoint

`POST /api/products_stocks/stock_fenicio.json` — despite living under the
generic `products_stocks` write-shaped namespace, this is a read-only
aggregation lookup: the controller only reads `Availability.total` per SKU
and returns it, it does not accept or process any write fields.

Request (raw JSON body, not the CakePHP `data[Model][field]` form encoding
used by other legacy endpoints):

```json
{
  "_idSolicitud": "any-client-supplied-string-or-null",
  "skus": ["123072", "123073"],
  "warehouse_ids": [25]
}
```

- `skus` is required and must be an array (omitting it, or sending a bare
  array as the body, returns `{"status":"ERROR","mensaje":"Request inválido: debe incluir “_idSolicitud” y “skus” como arreglo."}`).
- `warehouse_ids` is optional (`number[]`, or per MCP docs also accepted as a
  comma-separated string). Empirically verified to actually filter: passing
  different single warehouse ids (20/24/25/26/27) for the same SKUs returned
  different stock numbers per warehouse, confirming real per-warehouse
  scoping (not ignored).
- **Important:** this endpoint's response envelope uses `status: "OK"/"ERROR"`
  (uppercase), NOT the lowercase `"ok"/"error"` convention documented for the
  rest of the legacy API and assumed in the "Common response envelope fields"
  section above. Client code must check for both.

Response:

```json
{
  "status": "OK",
  "mensaje": null,
  "_idSolicitud": "echoed-back",
  "data": {
    "stockPorSku": [
      {"sku": "123072", "stock": 4},
      {"sku": "123073", "stock": -7}
    ]
  },
  "last_update": "2026-08-29 02:56:31"
}
```

`stock` can be negative (oversell / not yet reconciled) — treat as signed,
not clamp to zero, when computing deltas; only clamp when pushing an
absolute quantity to Shopify if the storefront doesn't accept negatives.

Implemented as `BIMSClient.stock_fenicio(skus, warehouse_ids, request_id)` in
`src/bims_shopify/adapters/bims/client.py`, consumed by
`BIMSERPAdapter.fetch_stock_levels` (batches of 200 SKUs per call).

### SKU / variant mapping decision

- `Product.code` is null on every sampled row (confirmed again on mystore).
- `Product.code2` is present and near-unique (~94% non-null on samples of
  300+ live rows) — confirmed as the correct Shopify-matching SKU field.
  Default `field_mappings["product_sku_field"]` is now `code2` (was `code`).
- Size/color variants (e.g. "BERMUDA CABALLERO CARGO AZUL (28)") are modeled
  by BIMS as **separate flat product rows**, each with its own unique
  `code2`, NOT via `main_product_id`/`ProductsVariant`. Both of those fields
  exist in the schema but were empty (`null`/`[]`) for every sampled product
  on mystore's company_id=6 catalog. Decision: no parent/variant grouping is
  implemented; each BIMS product row maps 1:1 to a Shopify variant matched
  by SKU. Re-evaluate if a future tenant's data does populate
  `main_product_id`.

### Filtering used for the product pull

`GET /api/products/index.json?mode=full&limit=250&offset=N[&last_update=...]`,
paged to exhaustion (stop when a page returns fewer than `limit` items).
Rows are dropped when: `enabled` is falsy, `exclude_ecommerce` is truthy, or
`company_id` doesn't match `tenant.bims_company_id`, or `code2` is empty.
`enabled`/`exclude_ecommerce` can come back as real booleans or as string
`"0"/"1"` depending on field — both are normalized via a truthy-string check.

### Gotchas

- Cloudflare blocks the default Python `urllib`/`requests` user-agent
  (`Error 1010: browser_signature_banned`). Always send an explicit
  `User-Agent` header (the codebase's `BIMSClient` already does this via
  `USER_AGENT` in `client.py`) — this applies to ad hoc scripts/curl too.
- `products/index.json` full-mode `Availability` (capital A) is unreliable/
  null as previously noted; `stock_by_warehouse` on `Product` is a boolean
  capability flag, not the actual per-warehouse quantities — don't confuse
  it with real stock data.
- No observed rate-limiting during ~110 sequential calls (49 product pages +
  ~60 stock batches) pulling the full mystore catalog (~12,177 products),
  but no documented rate limit was found either — keep the adapter's paging
  sequential (not concurrent) to stay conservative.
