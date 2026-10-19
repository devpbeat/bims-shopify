"""Inbound callback coercion (BVP-9), token check and result classifiers."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from bims_shopify.domain.bancard_vpos import (
    ConfirmationOutcome,
    RollbackOutcome,
    VposAmount,
    VposApiError,
    VposAttemptsExhaustedError,
    VposCallbackShapeError,
    VposError,
    classify_response_code,
    classify_rollback_key,
    confirm_token,
    parse_confirm_operation,
    verify_confirm_token,
)

K = "secret-key"


def _token(amount: str = "10100.00") -> str:
    return hashlib.md5(
        f"{K}12313confirm{amount}PYG".encode()
    ).hexdigest()


def _string_typed() -> dict:
    # Mirrors the vendor PDF worked example (p.49): every numeric as a string.
    return {
        "token": _token(),
        "shop_process_id": "12313",
        "response": "S",
        "response_details": "Procesado Correctamente",
        "amount": "10100.00",
        "currency": "PYG",
        "authorization_number": "123456",
        "ticket_number": "123456789123456",
        "iva_amount": "1100.0",
    }


def _number_typed() -> dict:
    # Parsed from raw JSON text, as a FastAPI/json body would arrive.
    raw = (
        '{"token": "%s", "shop_process_id": 12313, "amount": 10100.00, '
        '"currency": "PYG", "ticket_number": 123456789123456, "iva_amount": 1100.0}'
        % _token()
    )
    return json.loads(raw)


@pytest.mark.parametrize("payload", [_string_typed(), _number_typed()], ids=["str", "num"])
def test_both_encodings_parse_identically(payload):
    cb = parse_confirm_operation(payload)
    assert cb.shop_process_id == 12313
    assert cb.amount.value == Decimal("10100.00")
    assert cb.amount.text == "10100.00"
    assert cb.iva_amount == Decimal("1100.0")
    assert cb.ticket_number == 123456789123456
    assert cb.currency == "PYG"
    assert verify_confirm_token(K, cb) is True


def test_amount_without_decimals_verifies_against_normalised_token():
    payload = _string_typed() | {"amount": 10100}
    cb = parse_confirm_operation(payload)
    assert cb.amount.text == "10100.00"
    assert verify_confirm_token(K, cb)


def test_decimal_values_are_accepted_for_json_parsed_with_decimal():
    payload = _string_typed() | {"amount": Decimal("10100.00"), "iva_amount": Decimal("1100.0")}
    assert parse_confirm_operation(payload).iva_amount == Decimal("1100.0")


def test_ticket_number_keeps_full_precision():
    cb = parse_confirm_operation(_string_typed() | {"ticket_number": 999999999999999})
    assert cb.ticket_number == 999999999999999


@pytest.mark.parametrize(
    "field, value",
    [
        ("shop_process_id", "abc"),
        ("shop_process_id", None),
        ("shop_process_id", True),
        ("shop_process_id", 12.5),
        ("shop_process_id", "12 3"),
        ("amount", "abc"),
        ("amount", None),
        ("amount", False),
        ("amount", "1e3"),
        ("amount", "10100.005"),
        ("amount", "-5.00"),
        ("iva_amount", "x"),
        ("iva_amount", None),
        ("ticket_number", "1.5"),
        ("ticket_number", [1]),
    ],
)
def test_malformed_numeric_fields_raise_shape_error(field, value):
    with pytest.raises(VposCallbackShapeError):
        parse_confirm_operation(_string_typed() | {field: value})


@pytest.mark.parametrize("missing", ["token", "shop_process_id", "amount", "iva_amount", "ticket_number", "currency"])
def test_missing_fields_raise_shape_error(missing):
    payload = _string_typed()
    del payload[missing]
    with pytest.raises(VposCallbackShapeError):
        parse_confirm_operation(payload)


@pytest.mark.parametrize("payload", [None, [], "x", 5])
def test_non_mapping_payload_raises_shape_error(payload):
    with pytest.raises(VposCallbackShapeError):
        parse_confirm_operation(payload)


def test_shape_error_is_a_vpos_error():
    assert issubclass(VposCallbackShapeError, VposError)


def test_wrong_token_of_equal_length_is_rejected():
    cb = parse_confirm_operation(_string_typed())
    flipped = _string_typed() | {"token": ("0" if cb.token[0] != "0" else "1") + cb.token[1:]}
    assert len(flipped["token"]) == len(cb.token)
    assert verify_confirm_token(K, parse_confirm_operation(flipped)) is False


def test_token_over_different_amount_is_rejected():
    cb = parse_confirm_operation(_string_typed() | {"amount": "10100.01"})
    assert verify_confirm_token(K, cb) is False


def test_verify_uses_compare_digest(monkeypatch):
    calls = []
    real = __import__("hmac").compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("bims_shopify.domain.bancard_vpos.hmac.compare_digest", spy)
    verify_confirm_token(K, parse_confirm_operation(_string_typed()))
    assert len(calls) == 1


def test_non_string_token_never_verifies():
    cb = parse_confirm_operation(_string_typed() | {"token": 123})
    assert verify_confirm_token(K, cb) is False


def test_outbound_formatting_unaffected_by_inbound_parsing():
    parse_confirm_operation(_string_typed())
    assert VposAmount.from_order_total("130").text == "130.00"
    assert confirm_token(K, 12313, VposAmount.from_order_total(10100), "PYG") == _token()


# --- T2.5 classifiers / taxonomy -------------------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("00", ConfirmationOutcome.APPROVED),
        ("05", ConfirmationOutcome.DECLINED),
        ("12", ConfirmationOutcome.DECLINED),
        ("15", ConfirmationOutcome.DECLINED),
        ("51", ConfirmationOutcome.DECLINED),
        ("99", ConfirmationOutcome.UNKNOWN),
        ("0", ConfirmationOutcome.UNKNOWN),
        (None, ConfirmationOutcome.UNKNOWN),
        ("", ConfirmationOutcome.UNKNOWN),
        (0, ConfirmationOutcome.UNKNOWN),
    ],
)
def test_response_code_classification(code, expected):
    assert classify_response_code(code) is expected


@pytest.mark.parametrize(
    "key, expected",
    [
        ("RollbackSuccessful", RollbackOutcome.BENIGN),
        ("PaymentNotFoundError", RollbackOutcome.BENIGN),
        ("AlreadyRollbackedError", RollbackOutcome.BENIGN),
        ("TransactionAlreadyConfirmed", RollbackOutcome.OPERATOR_ACTION),
        ("PosCommunicationError", RollbackOutcome.OTHER),
        ("InvalidTokenError", RollbackOutcome.OTHER),
        ("SomethingNew", RollbackOutcome.OTHER),
        (None, RollbackOutcome.OTHER),
    ],
)
def test_rollback_key_classification(key, expected):
    assert classify_rollback_key(key) is expected


def test_api_error_carries_key_level_dsc_and_taxonomy():
    err = VposApiError("InvalidTokenError", "error", "bad token")
    assert (err.key, err.level, err.dsc) == ("InvalidTokenError", "error", "bad token")
    assert isinstance(err, VposError)
    assert issubclass(VposAttemptsExhaustedError, VposError)
