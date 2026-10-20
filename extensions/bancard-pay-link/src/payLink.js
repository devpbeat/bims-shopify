// Pure helpers for the pay-link request, kept free of the `shopify` global so
// they can be unit tested with `node --test`.

// Same origin as `application_url` in shopify.app.toml.
export const PAY_LINK_URL = "https://mystoresync.ignitesolutions.click/checkout-extension/pay-link";

const DEFAULT_ATTEMPTS = 3;
const DEFAULT_DELAY_MS = 3000;

/** The pay URL from a backend reply, or null when nothing is owed or it looks unsafe. */
export function parsePayLink(body) {
  if (!body || body.status !== "pay" || typeof body.pay_url !== "string") {
    return null;
  }
  return body.pay_url.startsWith("https://") ? body.pay_url : null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Ask the backend for the card-payment link of an order.
 *
 * The payment intent is created asynchronously after the order, so a "nothing
 * to pay" reply is retried a few times before giving up. Every failure resolves
 * to null: the buyer never sees an error.
 */
export async function requestPayLink({
  fetchFn,
  getToken,
  orderId,
  attempts = DEFAULT_ATTEMPTS,
  delayMs = DEFAULT_DELAY_MS,
}) {
  if (!orderId) {
    return null;
  }
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (attempt > 0) {
      await sleep(delayMs);
    }
    try {
      const token = await getToken();
      const reply = await fetchFn(PAY_LINK_URL, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ order_id: orderId }),
      });
      if (!reply.ok) {
        return null;
      }
      const url = parsePayLink(await reply.json());
      if (url) {
        return url;
      }
    } catch {
      return null;
    }
  }
  return null;
}
