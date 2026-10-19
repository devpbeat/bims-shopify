"""Signed, expiring checkout-link tokens (HCP-1)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bims_shopify.checkout_link import (
    CheckoutTokenError,
    derive_link_secret,
    sign_checkout_token,
    sign_navigation_token,
    verify_token,
)
from bims_shopify.config import Settings

SECRET = "unit-test-secret"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TTL = timedelta(hours=1)


def _nav(slug="acme", order_id="1001", now=NOW):
    return sign_navigation_token(SECRET, slug, order_id, ttl=TTL, now=now)


def test_navigation_roundtrip():
    claims = verify_token(SECRET, _nav(), "acme", now=NOW)
    assert claims.order_id == "1001"
    assert claims.slug == "acme"
    assert claims.process_id is None


def test_checkout_roundtrip_with_process_id():
    token = sign_checkout_token(SECRET, "acme", "1001", "100100", ttl=TTL, now=NOW)
    claims = verify_token(SECRET, token, "acme", require_process_id=True, now=NOW)
    assert (claims.order_id, claims.process_id) == ("1001", "100100")


def test_missing_process_id_rejected_when_required():
    with pytest.raises(CheckoutTokenError):
        verify_token(SECRET, _nav(), "acme", require_process_id=True, now=NOW)


def test_every_flipped_character_is_rejected():
    token = _nav()
    for i in range(len(token)):
        flipped = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
        with pytest.raises(CheckoutTokenError):
            verify_token(SECRET, flipped, "acme", now=NOW)


def test_expired_token_rejected_with_expired_flag():
    with pytest.raises(CheckoutTokenError) as exc:
        verify_token(SECRET, _nav(), "acme", now=NOW + TTL + timedelta(seconds=1))
    assert exc.value.expired is True


def test_token_valid_up_to_expiry():
    verify_token(SECRET, _nav(), "acme", now=NOW + TTL)


def test_cross_slug_replay_rejected():
    with pytest.raises(CheckoutTokenError) as exc:
        verify_token(SECRET, _nav(slug="acme"), "other", now=NOW)
    assert exc.value.expired is False


def test_wrong_secret_rejected():
    with pytest.raises(CheckoutTokenError):
        verify_token("another-secret", _nav(), "acme", now=NOW)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b", "a.b.c", ".", "é.é", "x" * 500])
def test_malformed_tokens_raise_token_error(garbage):
    with pytest.raises(CheckoutTokenError):
        verify_token(SECRET, garbage, "acme", now=NOW)


def test_signature_is_checked_before_payload_is_parsed(monkeypatch):
    import bims_shopify.checkout_link as mod

    order = []
    real_digest = mod.hmac.compare_digest
    real_loads = mod.json.loads
    monkeypatch.setattr(mod.hmac, "compare_digest", lambda a, b: (order.append("sig"), real_digest(a, b))[1])
    monkeypatch.setattr(mod.json, "loads", lambda *a, **k: (order.append("json"), real_loads(*a, **k))[1])
    verify_token(SECRET, _nav(), "acme", now=NOW)
    assert order == ["sig", "json"]


def test_forged_payload_with_valid_looking_signature_is_rejected():
    token = _nav()
    body, sig = token.split(".")
    forged = sign_navigation_token(SECRET, "acme", "9999", ttl=TTL, now=NOW).split(".")[0]
    with pytest.raises(CheckoutTokenError):
        verify_token(SECRET, f"{forged}.{sig}", "acme", now=NOW)


def test_empty_secret_cannot_sign_or_verify():
    with pytest.raises(ValueError):
        sign_navigation_token("", "acme", "1", ttl=TTL, now=NOW)
    with pytest.raises(ValueError):
        verify_token("", _nav(), "acme", now=NOW)


def test_token_does_not_contain_secret():
    assert SECRET not in _nav()


def test_derive_link_secret_prefers_explicit_and_falls_back_to_fernet():
    assert derive_link_secret(Settings(checkout_link_secret="explicit", fernet_key="f")) == "explicit"
    derived = derive_link_secret(Settings(checkout_link_secret="", fernet_key="fernet"))
    assert derived and derived != "fernet"
    assert derived != derive_link_secret(Settings(checkout_link_secret="", fernet_key="other"))
    assert derive_link_secret(Settings(checkout_link_secret="", fernet_key="")) == ""
