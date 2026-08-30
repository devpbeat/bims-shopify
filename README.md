# bims-shopify

A multi-tenant Python microservice that synchronizes the BIMS ERP (Paraguay)
with Shopify: inventory, product catalog, order-to-sale creation, and
hosted-checkout payment flows.

## Architecture

Hexagonal / screaming architecture. Business logic (`domain`, `application`)
has no framework or IO dependency; everything else plugs in through `ports`.

```
                         +-------------------+
                         |     api/          |  FastAPI routers
                         |  (HTTP boundary)   |
                         +---------+---------+
                                   |
                         +---------v---------+
                         |   application/     |  use cases
                         |  (orchestration)   |
                         +---------+---------+
                                   |
                    +--------------+--------------+
                    |                             |
             +------v------+              +-------v------+
             |   ports/     |<-------------|   domain/    |
             | (Protocols)  |   entities   | (pure logic) |
             +------+------+               +--------------+
                    |
   +----------------+-----------------+------------------+
   |                |                 |                  |
+--v---+       +----v-----+     +-----v------+     +------v------+
| bims |       | shopify  |     | payments   |     | persistence |
| ERPPort      | Storefront     | PaymentPort |    | Tenant/SyncState
+------+       +----------+     +------------+     +-------------+
```

- `domain/` — `Tenant`, `ProductSnapshot`, `InventoryDelta`, `SaleOrder`, `PaymentIntent`.
- `ports/` — `ERPPort`, `StorefrontPort`, `PaymentProviderPort`, `TenantRepository`, `SyncStateRepository`.
- `adapters/bims/` — `BIMSClient` (httpx), CakePHP `data[Model][field]` form encoder, `BIMSERPAdapter`.
- `adapters/shopify/` — `ShopifyClient` (Admin GraphQL 2025-07), webhook HMAC verification.
- `adapters/payments/` — `PagoparProvider` (optional `pagopar-sdk` extra), `BancardViaBIMSProvider`.
- `adapters/persistence/` — SQLAlchemy 2 async models + repositories, Fernet encryption for secrets.
- `application/` — `SyncInventoryToShopify`, `SyncProductsToShopify`, `ProcessShopifyOrder`,
  `MaybeCreatePurchaseOrder`, `CreatePaymentLink`, `HandlePaymentCallback`.
- `api/` — FastAPI routers for health, tenants, webhooks, payments, manual sync.
- `scheduler.py` — `AsyncIOScheduler` running every `SYNC_INTERVAL_MINUTES`, per-tenant locked.

## Quickstart

```bash
cp env.example .env   # sandbox tooling may block writing .env directly; rename after copying
docker compose up --build
```

The API is then available at `http://localhost:8000`. Check `GET /health`.

## Tenant onboarding

```bash
curl -X POST http://localhost:8000/tenants \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "slug": "acme",
    "bims_base_url": "https://acme.bims-erp.com",
    "bims_api_key": "acme_theirapikey",
    "shopify_shop_domain": "acme.myshopify.com",
    "shopify_access_token": "shpat_xxx",
    "shopify_webhook_secret": "whsec_xxx",
    "shopify_location_id": "gid://shopify/Location/123",
    "bims_posale_id": 1,
    "bims_warehouse_id": 1,
    "bims_company_id": 1,
    "bims_currency_id": 1,
    "bims_payment_method_id": 1,
    "default_customer_contact_id": 1,
    "reorder_threshold": 5,
    "reorder_strategy": "purchase_order",
    "payment_provider": "bancard",
    "field_mappings": {
      "product_sku_field": "code",
      "product_stock_field": "stock",
      "stock_write_endpoint": "/api/stocks/update.json"
    }
  }'
```

## Shopify app / OAuth install

BIMS Sync is a single, multi-tenant Shopify app (config-only, no Remix
frontend) hosted at `https://mystoresync.ignitesolutions.click`. Merchants
install it through the standard OAuth authorization code grant; the service
provisions a `Tenant` row automatically and an operator finishes BIMS
configuration afterwards via `PUT /tenants/{slug}`.

### Environment variables

Set these in `.env` (see `env.example`):

- `SHOPIFY_API_KEY` — the app's client ID from the Partner/Dev Dashboard.
- `SHOPIFY_API_SECRET` — the app's client secret. Used to verify the OAuth
  callback HMAC, exchange the authorization code for an offline token, and
  verify app-level/compliance webhook HMACs.
- `PUBLIC_BASE_URL` — defaults to `https://mystoresync.ignitesolutions.click`;
  used to build the OAuth `redirect_uri`.

### Install flow

1. Merchant (or the Dev Dashboard's "Test on development store") hits
   `GET /shopify/install?shop={shop}.myshopify.com`. The service validates
   the shop domain, persists a short-lived (10 min) CSRF state nonce, and
   302-redirects to `https://{shop}/admin/oauth/authorize` requesting
   `read_products,write_products,read_inventory,write_inventory,read_orders,write_orders,read_locations`.
2. Shopify redirects back to `GET /shopify/callback` with `code`, `hmac`,
   `shop`, `state`, `timestamp`. The service verifies the `hmac` (per
   Shopify's authorization-code-grant algorithm: drop `hmac`/`signature`,
   sort remaining params, hex-HMAC-SHA256 with `SHOPIFY_API_SECRET`),
   consumes (and invalidates) the `state` nonce, exchanges `code` for an
   **offline** access token via `POST /admin/oauth/access_token`, and fetches
   the shop's primary location id via the Admin GraphQL API.
3. The tenant is upserted (slug = shop subdomain) with the encrypted access
   token and `shopify_location_id`, but left `active=false` until BIMS
   fields (`bims_base_url`, `bims_api_key`, `bims_posale_id`, etc.) are
   filled in via `PUT /tenants/{slug}`.

### Webhooks

Two kinds of webhook delivery exist side by side:

- **Per-tenant** (legacy, still supported): `POST /webhooks/shopify/{tenant_slug}`,
  signed with that tenant's own `shopify_webhook_secret`.
- **App-level** (declared in `shopify.app.toml`, delivered for every
  installed shop): `POST /webhooks/shopify/app` for `orders/paid` and
  `orders/cancelled`. These are signed with the single app-wide
  `SHOPIFY_API_SECRET` (standard Shopify behavior — webhook HMACs are
  always computed with the app's client secret, not a per-shop secret) and
  the shop is resolved from the `X-Shopify-Shop-Domain` header rather than
  a path segment. Deliveries for shops with no matching tenant return `200
  {"status": "ignored"}` so Shopify does not retry indefinitely.
- **GDPR compliance** (mandatory for all public apps):
  `POST /webhooks/shopify/compliance/{topic}` (or a single shared
  `/webhooks/shopify/compliance/` endpoint keyed off `X-Shopify-Topic`)
  handles `customers/data_request`, `customers/redact`, and `shop/redact`.
  All three verify HMAC and return `200`; `shop/redact` additionally
  deletes the tenant's row (this app stores no Shopify customer PII, so
  `customers/*` topics are no-ops beyond the required 200 response).

### Shopify CLI setup (Dev Dashboard → deploy)

1. In the [Partner Dashboard](https://partners.shopify.com), create an app,
   copy its **Client ID** and **Client secret** into `SHOPIFY_API_KEY` /
   `SHOPIFY_API_SECRET`, and paste the Client ID into `client_id` in
   `shopify.app.toml`.
2. From the repo root, link the local config to that app:
   ```bash
   shopify app config link
   ```
3. Review `shopify.app.toml` (application URL, scopes, redirect URLs,
   webhook subscriptions) and push it to Shopify:
   ```bash
   shopify app deploy
   ```
4. Install on a dev store by visiting
   `https://mystoresync.ignitesolutions.click/shopify/install?shop=your-dev-store.myshopify.com`,
   or via the Dashboard's "Select store" test-install flow.
5. After install, configure BIMS fields for the new tenant:
   ```bash
   curl -X PUT https://mystoresync.ignitesolutions.click/tenants/{slug} \
     -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"bims_base_url": "...", "bims_api_key": "...", "active": true, ...}'
   ```

## Payment flow

Shopify does not allow arbitrary third-party payment gateways at checkout
without becoming a registered Payments Partner app. This integration instead
uses a **hosted-checkout redirect**: `POST /payments/{provider}/{tenant_slug}/checkout`
creates a link at Pagopar or Bancard (via BIMS `bims_pay`/`bancard_transactions`
endpoints), the customer is redirected there to pay, and once the provider
calls back `/payments/{provider}/{tenant_slug}/callback` (POST or GET,
depending on the provider), the service re-verifies the payment status
directly with the provider — the callback body is never trusted on its own —
and marks the Shopify order as paid via the `orderMarkAsPaid` Admin API
mutation. This is **not** a native Shopify checkout payment method — it is
an after-the-fact reconciliation flow. Callback delivery is deduplicated
with the same atomic `try_claim_event` pattern used for Shopify webhooks, so
redelivered callbacks never re-verify or double-mark an order as paid.

### Pagopar

Uses the [`pagopar-sdk`](https://pypi.org/project/pagopar-sdk/) package
(install with `uv sync --extra pagopar`). `tenant.provider_config`:

```json
{
  "public_key": "your-pagopar-public-key",
  "private_key": "your-pagopar-private-key",
  "base_url": "https://api.pagopar.com",
  "payment_method_id": 1,
  "description": "Order from Acme Shopify store",
  "buyer": {
    "nombre": "Cliente",
    "email": "cliente@example.com",
    "documento": "1234567",
    "telefono": "0981000000",
    "ciudad": "1"
  }
}
```

`create_checkout` builds a `pagopar_sdk.StartTransactionRequest` and calls
`client.commerce.create_transaction(...)` (`POST
/api/comercios/2.0/iniciar-transaccion`); the SDK computes the SHA1 token
itself. Per Pagopar's docs the response is `{"respuesta": true, "resultado":
[{"data": "<hash_pedido>", "pedido": "<pagopar_order_number>"}]}` —
`resultado[0]["data"]` **is** the `hash_pedido` string, not a nested object.
That value becomes the `provider_reference`, and the hosted checkout page is
`https://www.pagopar.com/pagos/{hash_pedido}`.

`verify_payment` calls `client.commerce.get_order(hash_pedido)` (`POST
/api/pedidos/1.1/traer`) and only trusts the `pagado`/`cancelado` boolean
fields from that authenticated lookup — never the callback body — because
Pagopar's documented response has **no `estado` field**:
`resultado[0] = {"pagado": bool, "cancelado": bool, "hash_pedido": str,
"numero_pedido": str, "monto": str, ...}`. `pagado: true` means CONFIRMED,
`cancelado: true` means FAILED, otherwise PENDING.

Pagopar's confirmation callback shares that same `resultado[0]` shape and
never echoes the merchant's own order id (only its internal
`numero_pedido` and the shared `hash_pedido` are present), so the merchant
order is always resolved from `hash_pedido`/`provider_reference`, not from
the callback payload. If the callback includes a `token` field, it is
checked against `pagopar_sdk.build_token(private_key, hash_pedido)`
(SHA1(private_key + hash_pedido)) before any lookup happens.

Source: Pagopar's "API - Integración de medios de pagos" integration guide,
https://soporte.pagopar.com/portal/es/kb/articles/api-integracion-medios-pagos
(retrieved 2026-08). Pagopar does not document a separate sandbox host —
"test mode" uses a distinct public/private key pair against the same
`https://api.pagopar.com` endpoint — so `base_url` remains overridable via
`provider_config` for completeness, not because a documented sandbox host
exists.

### Bancard (via BIMS)

The BIMS OpenAPI spec documents `bims_pay`/`bancard_transactions` endpoints
as generic, untyped `application/x-www-form-urlencoded` bodies — it does not
enumerate Bancard's actual field names. Configure them per tenant:

```json
{
  "create_link_fields": {
    "monto": "{amount}",
    "moneda": "{currency}",
    "id_pedido": "{order_id}",
    "descripcion": "{description}",
    "url_retorno": "{return_url}"
  },
  "return_url": "https://acme.myshopify.com/pages/thank-you",
  "confirmed_status_values": ["confirmed", "paid"],
  "pending_status_values": ["pending", "processing"]
}
```

`create_checkout` renders `create_link_fields` with the `{amount}`,
`{currency}`, `{order_id}`, `{description}` and `{return_url}` placeholders
and POSTs them to `/api/bims_pay/create_link.json`. The Shopify order
number is used directly as `shop_process_id`. `verify_payment` calls `GET
/api/bancard_transactions/lookup/{shop_process_id}.json` and only treats the
lookup's `status` (or `confirmed: true`) as authoritative — checked against
the tenant's configurable `confirmed_status_values` /
`pending_status_values`, since the real Bancard status vocabulary isn't
documented in the spec.

## Known BIMS API gaps

- `GET /api/products/index.json` has no modified-since filter; only
  `GET /api/products/deleted.json` accepts a required `last_update` param
  (for deletions only). Full-catalog polling is therefore required for
  product/price sync; the sync use cases mitigate this by diffing snapshots.
- Stock write endpoints (`stocks/add|edit|update`, `invads/add`,
  `products_stocks/stock_fenicio`, `service_tools/stocks`) have no enumerated
  field schema in the OpenAPI spec (`additionalProperties: true`). Configure
  the actual field names your BIMS instance expects via a tenant's
  `field_mappings` JSON column.

## Development

```bash
uv sync --extra dev
uv run pytest
```

## Database Migrations

Schema changes are managed with Alembic. **Never use
`Base.metadata.create_all` in production** — it silently ignores schema
drift (columns added to `models.py` never get applied to an existing
database, which previously caused `UndefinedColumnError` 500s). The app
now runs `alembic upgrade head` automatically on startup
(`_run_migrations` in `src/bims_shopify/main.py`), so every boot brings the
connected database to the latest schema. This is idempotent and safe to
run on every startup.

### Creating a new migration

After changing a model in `src/bims_shopify/adapters/persistence/models.py`:

```bash
make revision msg="add foo column to bar"
```

Review the generated file under `alembic/versions/` before committing —
autogenerate doesn't always get constraints/indexes exactly right. A CI
test (`tests/test_schema_migrations.py`) fails the build if `models.py`
ever drifts from the migration head, so a forgotten migration is caught
before merge.

### Applying migrations manually

```bash
make migrate
```

### One-off: adopting Alembic on an existing (pre-Alembic) database

Databases that were bootstrapped via the old `create_all` path already
have all the tables but may be missing columns added later (e.g.
`sync_states.last_error`, `last_error_at`, `last_run_summary`), and have no
`alembic_version` table. `alembic upgrade head` from scratch would try to
`CREATE TABLE` tables that already exist and fail. To adopt Alembic on such
a database:

1. Stamp the database to the baseline revision so Alembic considers the
   original `CREATE TABLE`s already applied, without running them:

   ```bash
   ALEMBIC_DATABASE_URL="<your-real-database-url>" uv run alembic stamp 6376be5c801c
   ```

2. Run the normal upgrade, which will now only apply migrations after the
   baseline — in particular `0002_sync_states_cols`, which adds any missing
   `sync_states` columns (guarded by an inspector check, so it's safe even
   if some columns already exist):

   ```bash
   ALEMBIC_DATABASE_URL="<your-real-database-url>" uv run alembic upgrade head
   ```

After this one-off step, the app's automatic `alembic upgrade head` on
startup keeps the database current going forward.

## Database Migrations

Schema is managed exclusively by Alembic (`alembic/`). **Never use
`Base.metadata.create_all` in production** — it silently ignores schema
drift: if a column is added to a model but the running database already
has the table, `create_all` does nothing, and the app crashes at runtime
with `UndefinedColumnError` instead of failing fast at startup.

The app runs `alembic upgrade head` automatically on startup (see
`create_app()`'s lifespan in `src/bims_shopify/main.py`), against the
`DATABASE_URL` from Settings. This is idempotent — safe to run on every
boot, including when already at head.

### Creating a new migration

After changing a model in `src/bims_shopify/adapters/persistence/models.py`:

```bash
make revision msg="add foo column to bar"
```

This runs `alembic revision --autogenerate -m "..."` against the app's
configured DB. Always review the generated file before committing —
autogenerate does not reliably detect things like column type changes,
some constraint renames, or server-side defaults.

### Applying migrations manually

```bash
make migrate
```

Equivalent to `uv run alembic upgrade head`. Not required in normal
operation (the app does this on startup) — useful for CI, one-off ops, or
inspecting migration state with `uv run alembic current` / `uv run alembic history`.

### Existing pre-Alembic databases

Any database that was created by the old `Base.metadata.create_all` path
(before Alembic was introduced) already has the `tenants`, `sync_states`,
and `processed_events` tables, but has no `alembic_version` table and is
missing columns that were added to models after the table was first
created (e.g. `sync_states.last_error`, `last_error_at`,
`last_run_summary`).

Running `alembic upgrade head` against such a database without
preparation would fail: revision `6376be5c801c` ("initial schema") tries
to `CREATE TABLE` on tables that already exist.

Fix, one-time, per legacy database:

```bash
# 1. Tell Alembic the DB is already at the "initial schema" baseline
#    (tables already exist, so skip 0001's create_table calls).
uv run alembic stamp 6376be5c801c

# 2. Apply everything after the baseline — this picks up 0002, which
#    inspects sync_states and adds only the columns that are missing
#    (idempotent: safe even if some/all columns already exist).
uv run alembic upgrade head
```

After this one-time stamp, the database behaves like any other Alembic-managed
database and the app's automatic `alembic upgrade head` on startup keeps it
current going forward.

### Schema drift is a test failure

`tests/test_schema_migrations.py` runs Alembic's own autogenerate
comparison (`compare_metadata`) against a database migrated to head. If
`models.py` changes without a matching migration, this test fails in CI
instead of surfacing as a production 500.
