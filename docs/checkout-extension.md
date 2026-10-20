# Bancard pay link on the Thank you page

The `bancard-pay-link` checkout UI extension (`extensions/bancard-pay-link`)
shows a "Pay with card" button on the Thank you page while a vPOS order is unpaid.

## How it works

- Target: `purchase.thank-you.block.render`, API version `2026-10`.
- The extension reads `shopify.orderConfirmation.value.order.id`, gets a session
  token with `shopify.sessionToken.get()` and calls
  `POST /checkout-extension/pay-link` with `Authorization: Bearer <token>`.
- The backend verifies the HS256 token with `SHOPIFY_API_SECRET` (audience
  `SHOPIFY_API_KEY`, expiry, issuer/destination match), resolves the tenant by the
  stored `shopify_shop_domain`, and answers `{"status":"pay","pay_url":"..."}` only
  while the vPOS intent is `pending` or `failed`. Anything else is
  `{"status":"none"}` and the extension renders nothing.
- Limits: 60 requests per minute per client IP and per shop (in-memory).

## Operator steps

1. Set `client_id` in `shopify.app.toml` (Dev Dashboard, app Settings, Client ID)
   and make sure `SHOPIFY_API_KEY` on the server is the same value; `SHOPIFY_API_SECRET`
   must be the app's client secret.
2. In the Dev Dashboard, request network access for checkout UI extensions
   (approved automatically), then `npm install` at the repo root.
3. `shopify app deploy` and release the version.
4. Shopify admin > Settings > Checkout > Customize > Thank you page: add the
   "Bancard pay link" app block, save and publish.
