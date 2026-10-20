import "@shopify/ui-extensions/preact";
import { render } from "preact";
import { useEffect, useState } from "preact/hooks";

import { requestPayLink } from "./payLink.js";

export default function extension() {
  render(<PayLinkBlock />, document.body);
}

function PayLinkBlock() {
  const [payUrl, setPayUrl] = useState(null);
  const orderId = shopify.orderConfirmation.value?.order?.id;

  useEffect(() => {
    let cancelled = false;
    requestPayLink({
      fetchFn: fetch,
      getToken: () => shopify.sessionToken.get(),
      orderId,
    }).then((url) => {
      if (!cancelled) {
        setPayUrl(url);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [orderId]);

  // Nothing owed, still loading, or any failure: render nothing at all.
  if (!payUrl) {
    return null;
  }

  return (
    <s-stack gap="base">
      <s-heading>{shopify.i18n.translate("title")}</s-heading>
      <s-text>{shopify.i18n.translate("description")}</s-text>
      <s-button variant="primary" href={payUrl}>
        {shopify.i18n.translate("payButton")}
      </s-button>
    </s-stack>
  );
}
