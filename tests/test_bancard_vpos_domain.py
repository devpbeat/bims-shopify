"""Pure-domain tests for the Bancard vPOS amount, token and id helpers."""
from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

import bims_shopify.domain.bancard_vpos as vpos

from bims_shopify.domain.bancard_vpos import (
    PROBE_SHOP_PROCESS_ID,
    VposAmount,
    VposInvalidAmountError,
    VposInvalidIdError,
    VposConfigurationError,
    build_shop_process_id,
    confirm_token,
    get_confirmation_token,
    rollback_token,
    single_buy_token,
    vpos_base_url,
    vpos_description,
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- T2.1 amounts -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (Decimal("130"), "130.00"),
        (130, "130.00"),
        ("130.0", "130.00"),
        ("130.5", "130.50"),
        ("1234567.1", "1234567.10"),
        (Decimal("0.005"), "0.01"),  # ROUND_HALF_UP
        (Decimal("130.125"), "130.13"),
        (130.1, "130.10"),  # float goes through Decimal(str(x))
    ],
)
def test_amount_text_is_two_decimals(raw, expected):
    amount = VposAmount.from_order_total(raw)
    assert amount.text == expected
    assert amount.value == Decimal(expected)


@pytest.mark.parametrize("raw", [0, Decimal("0.00"), -1, "-5", Decimal("0.004")])
def test_amount_must_be_positive(raw):
    with pytest.raises(VposInvalidAmountError):
        VposAmount.from_order_total(raw)


@pytest.mark.parametrize("raw", ["abc", "", None, True, Decimal("NaN"), Decimal("Infinity")])
def test_amount_rejects_garbage(raw):
    with pytest.raises(VposInvalidAmountError):
        VposAmount.from_order_total(raw)


def test_amount_rejects_more_than_13_integer_digits():
    VposAmount.from_order_total("9999999999999.99")
    with pytest.raises(VposInvalidAmountError):
        VposAmount.from_order_total("10000000000000")


# --- T2.2 tokens ------------------------------------------------------------

K = "secret-key"


def test_single_buy_token_matches_concatenation():
    amount = VposAmount.from_order_total("130")
    assert single_buy_token(K, 3332134, amount, "PYG") == _md5(K + "3332134" + "130.00" + "PYG")


def test_pdf_example_shape():
    amount = VposAmount.from_order_total("130")
    assert single_buy_token(K, 3332134, amount, "130.00") == _md5(K + "3332134" + "130.00" + "130.00")


def test_confirm_token_matches_concatenation():
    amount = VposAmount.from_order_total("10100")
    assert confirm_token(K, 12313, amount, "PYG") == _md5(K + "12313" + "confirm" + "10100.00" + "PYG")


def test_get_confirmation_token_matches_concatenation():
    assert get_confirmation_token(K, 12313) == _md5(K + "12313" + "get_confirmation")


def test_rollback_token_uses_literal_zero_amount():
    assert rollback_token(K, 12313) == _md5(K + "12313" + "rollback" + "0.00")


@pytest.mark.parametrize("bad", ["130.00", 130.0, Decimal("130.00"), 130, None])
def test_builders_reject_non_vpos_amount(bad):
    with pytest.raises(TypeError):
        single_buy_token(K, 1, bad, "PYG")
    with pytest.raises(TypeError):
        confirm_token(K, 1, bad, "PYG")


def test_token_differs_for_unnormalised_amount_string():
    good = single_buy_token(K, 1, VposAmount.from_order_total("130"), "PYG")
    assert good != _md5(K + "1" + "130" + "PYG")


@pytest.mark.parametrize("bad", ["123", 1.0, True, None])
def test_shop_process_id_in_tokens_must_be_int(bad):
    with pytest.raises(TypeError):
        get_confirmation_token(K, bad)
    with pytest.raises(TypeError):
        rollback_token(K, bad)


# --- T2.3 ids / description / urls -----------------------------------------


def test_build_shop_process_id_combines_row_id_and_attempt():
    assert build_shop_process_id(42, 3) == 4203
    assert build_shop_process_id(1, 0) == 100
    assert build_shop_process_id(7, 99) == 799


def test_build_accepts_numeric_string_row_id():
    assert build_shop_process_id("42", 1) == 4201


def test_widest_row_id_still_fits_fifteen_digits():
    spid = build_shop_process_id(1234567890123, 5)
    assert spid == 123456789012305
    assert len(str(spid)) == 15


def test_order_id_is_never_derived_from_the_shop_process_id():
    # The order is resolved through the stored provider_reference, not by arithmetic.
    assert not hasattr(vpos, "order_id_from_shop_process_id")


@pytest.mark.parametrize(
    "row_id, attempt",
    [
        ("abc", 0),
        ("gid://shopify/Order/123", 0),
        ("", 0),
        (0, 0),  # a persisted row id is always positive
        ("0", 0),
        ("12345678901234", 0),  # 14 digits: the id would exceed 15 digits
        ("-5", 0),
        (True, 0),
        (1.5, 0),
        (1000, 100),
        (1000, -1),
        (1000, True),
        (9999999999999, 99),  # collides with the probe id
    ],
)
def test_build_rejects_invalid_input(row_id, attempt):
    with pytest.raises(VposInvalidIdError):
        build_shop_process_id(row_id, attempt)


def test_no_row_id_scheme_value_can_equal_the_probe_id():
    # The probe id is 13 nines + attempt 99; the only row id that could reach it is refused.
    with pytest.raises(VposInvalidIdError):
        build_shop_process_id(9999999999999, 99)
    assert build_shop_process_id(9999999999999, 98) != PROBE_SHOP_PROCESS_ID


def test_probe_id_is_fifteen_digits():
    assert len(str(PROBE_SHOP_PROCESS_ID)) == 15


def test_description_truncates_to_twenty_chars():
    assert len(vpos_description("x" * 40)) == 20
    assert vpos_description("Order #1") == "Order #1"
    assert vpos_description("  padded  ") == "padded"


def test_base_url_per_environment():
    assert vpos_base_url("staging") == "https://vpos.infonet.com.py:8888"
    assert vpos_base_url("production") == "https://vpos.infonet.com.py"


@pytest.mark.parametrize("env", ["sandbox", "", None, "Production"])
def test_base_url_unknown_raises(env):
    with pytest.raises(VposConfigurationError):
        vpos_base_url(env)


@pytest.mark.parametrize("order_id", ["1001", 18943384158340, "189433841583401", "9" * 30])
def test_require_plain_order_id_has_no_length_limit(order_id):
    assert vpos.require_plain_order_id(order_id) == str(order_id)


@pytest.mark.parametrize("order_id", ["", "abc", "gid://shopify/Order/1", "-5", "1 2", True, 1.5, None])
def test_require_plain_order_id_refuses_malformed_ids(order_id):
    with pytest.raises(VposInvalidIdError):
        vpos.require_plain_order_id(order_id)
