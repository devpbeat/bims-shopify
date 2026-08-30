"""Tests for Shopify webhook HMAC verification."""
import base64
import hashlib
import hmac

from bims_shopify.adapters.shopify.webhook_auth import verify_shopify_hmac

SECRET = "whsecret"
BODY = b'{"id": 12345, "total_price": "10.00"}'


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def test_valid_signature_passes():
    signature = _sign(SECRET, BODY)
    assert verify_shopify_hmac(SECRET, BODY, signature) is True


def test_invalid_signature_fails():
    assert verify_shopify_hmac(SECRET, BODY, "not-a-real-signature") is False


def test_signature_with_wrong_secret_fails():
    signature = _sign("wrong-secret", BODY)
    assert verify_shopify_hmac(SECRET, BODY, signature) is False


def test_empty_signature_fails():
    assert verify_shopify_hmac(SECRET, BODY, "") is False
