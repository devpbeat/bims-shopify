import assert from "node:assert/strict";
import { test } from "node:test";

import { PAY_LINK_URL, parsePayLink, requestPayLink } from "./payLink.js";

const response = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

test("parsePayLink returns the https url only for a pay response", () => {
  assert.equal(parsePayLink({ status: "pay", pay_url: "https://x.test/p" }), "https://x.test/p");
  assert.equal(parsePayLink({ status: "none" }), null);
  assert.equal(parsePayLink({ status: "pay" }), null);
  assert.equal(parsePayLink({ status: "pay", pay_url: "javascript:alert(1)" }), null);
  assert.equal(parsePayLink({ status: "pay", pay_url: "http://x.test/p" }), null);
  assert.equal(parsePayLink(null), null);
});

test("requestPayLink sends the order id with the session token", async () => {
  let call;
  const fetchFn = async (url, init) => {
    call = { url, init };
    return response(200, { status: "pay", pay_url: "https://x.test/p" });
  };

  const url = await requestPayLink({
    fetchFn,
    getToken: async () => "tok",
    orderId: "gid://shopify/Order/1",
    attempts: 1,
  });

  assert.equal(url, "https://x.test/p");
  assert.equal(call.url, PAY_LINK_URL);
  assert.equal(call.init.method, "POST");
  assert.equal(call.init.headers.Authorization, "Bearer tok");
  assert.deepEqual(JSON.parse(call.init.body), { order_id: "gid://shopify/Order/1" });
});

test("requestPayLink retries a none response because the intent may not exist yet", async () => {
  const replies = [{ status: "none" }, { status: "pay", pay_url: "https://x.test/p" }];
  const fetchFn = async () => response(200, replies.shift());

  const url = await requestPayLink({
    fetchFn,
    getToken: async () => "tok",
    orderId: "1",
    attempts: 3,
    delayMs: 0,
  });

  assert.equal(url, "https://x.test/p");
});

test("requestPayLink gives up quietly after the attempts and on errors", async () => {
  const none = async () => response(200, { status: "none" });
  const broken = async () => {
    throw new Error("network down");
  };
  const unauthorized = async () => response(401, { error: "unauthorized" });
  const args = { getToken: async () => "tok", orderId: "1", attempts: 2, delayMs: 0 };

  assert.equal(await requestPayLink({ ...args, fetchFn: none }), null);
  assert.equal(await requestPayLink({ ...args, fetchFn: broken }), null);
  assert.equal(await requestPayLink({ ...args, fetchFn: unauthorized }), null);
});

test("requestPayLink does nothing without an order id", async () => {
  let called = false;
  const fetchFn = async () => {
    called = true;
    return response(200, { status: "none" });
  };

  assert.equal(await requestPayLink({ fetchFn, getToken: async () => "t", orderId: undefined }), null);
  assert.equal(called, false);
});
