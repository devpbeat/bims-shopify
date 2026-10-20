"""Pure domain core for the direct Bancard vPOS 2.0 integration.

No I/O lives here. The module owns the three things that must never drift:

* the amount string (``VposAmount``): the md5 token and the request body read
  the same instance, so two divergent strings cannot exist;
* the md5 token concatenations, which take a ``VposAmount`` and never a raw
  number or string;
* the ``shop_process_id`` derivation from the ``payment_intents`` row id;
* the order metafields written back after a confirmed payment.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum

from bims_shopify.domain.payment import OrderMetafield

CURRENCY = "PYG"
MAX_INTEGER_DIGITS = 13
MAX_ATTEMPT = 99
# Reserved id used by credential probes; real operations must never use it.
PROBE_SHOP_PROCESS_ID = 999999999999999

_TWO_PLACES = Decimal("0.01")
_VPOS_BASE_URLS = {
    "staging": "https://vpos.infonet.com.py:8888",
    "production": "https://vpos.infonet.com.py",
}
_DESCRIPTION_MAX = 20


class VposError(Exception):
    """Base class for vPOS domain errors."""


class VposInvalidAmountError(VposError):
    """The amount cannot be represented as a valid vPOS amount."""


class VposInvalidIdError(VposError):
    """The order id or attempt cannot form a valid shop_process_id."""


class VposConfigurationError(VposError):
    """Tenant-level vPOS configuration is missing or invalid."""


class VposCallbackShapeError(VposError):
    """The inbound confirm callback is malformed."""


class VposAttemptsExhaustedError(VposError):
    """No attempt numbers (00..99) are left for this order."""


class VposApiError(VposError):
    """Bancard answered with an error message."""

    def __init__(self, key: str, level: str, dsc: str) -> None:
        super().__init__(f"{key}: {dsc}")
        self.key = key
        self.level = level
        self.dsc = dsc


@dataclass(frozen=True)
class VposAmount:
    """One amount for one operation.

    ``text`` is the exact string hashed into tokens and sent on the wire;
    ``value`` is the numeric value used for equality checks. Build instances
    only through the classmethods.
    """

    text: str
    value: Decimal

    @classmethod
    def from_order_total(cls, raw: object) -> VposAmount:
        """Build an outbound amount (rounded half-up to two decimals)."""
        return cls._build(_to_decimal(raw), strict=False)

    @classmethod
    def from_vpos_payload(cls, raw: object) -> VposAmount:
        """Build an inbound amount; rejects sub-cent precision instead of rounding."""
        return cls._build(_to_decimal(raw), strict=True)

    @classmethod
    def _build(cls, number: Decimal, *, strict: bool) -> VposAmount:
        try:
            quantized = number.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise VposInvalidAmountError("amount out of range") from exc
        if strict and quantized != number:
            raise VposInvalidAmountError("amount has more than two decimals")
        if quantized <= 0:
            raise VposInvalidAmountError("amount must be positive")
        if len(str(int(quantized))) > MAX_INTEGER_DIGITS:
            raise VposInvalidAmountError("amount has too many integer digits")
        return cls(text=format(quantized, "f"), value=quantized)


def _to_decimal(raw: object) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise VposInvalidAmountError("amount must be a number")
    try:
        if isinstance(raw, Decimal):
            number = raw
        elif isinstance(raw, float):
            number = Decimal(str(raw))
        elif isinstance(raw, (int, str)):
            number = Decimal(raw.strip() if isinstance(raw, str) else raw)
        else:
            raise VposInvalidAmountError("amount must be a number")
    except InvalidOperation as exc:
        raise VposInvalidAmountError("amount is not numeric") from exc
    if not number.is_finite():
        raise VposInvalidAmountError("amount must be finite")
    return number


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _require_amount(amount: object) -> VposAmount:
    if not isinstance(amount, VposAmount):
        raise TypeError("amount must be a VposAmount")
    return amount


def _require_spid(shop_process_id: object) -> str:
    if isinstance(shop_process_id, bool) or not isinstance(shop_process_id, int):
        raise TypeError("shop_process_id must be an int")
    return str(shop_process_id)


def single_buy_token(
    private_key: str, shop_process_id: int, amount: VposAmount, currency: str = CURRENCY
) -> str:
    return _md5(private_key + _require_spid(shop_process_id) + _require_amount(amount).text + currency)


def confirm_token(
    private_key: str, shop_process_id: int, amount: VposAmount, currency: str = CURRENCY
) -> str:
    return _md5(
        private_key
        + _require_spid(shop_process_id)
        + "confirm"
        + _require_amount(amount).text
        + currency
    )


def get_confirmation_token(private_key: str, shop_process_id: int) -> str:
    return _md5(private_key + _require_spid(shop_process_id) + "get_confirmation")


def rollback_token(private_key: str, shop_process_id: int) -> str:
    return _md5(private_key + _require_spid(shop_process_id) + "rollback" + "0.00")


def require_plain_order_id(order_id: object) -> str:
    """A Shopify order id is a plain digit string. Its size is irrelevant to vPOS.

    The order id is never encoded into ``shop_process_id``, so there is no length
    limit here; only malformed ids (gids, signs, blanks) are refused.
    """
    if isinstance(order_id, bool) or not isinstance(order_id, (int, str)):
        raise VposInvalidIdError("order id must be numeric")
    text = str(order_id)
    if not text.isascii() or not text.isdigit():
        raise VposInvalidIdError("order id must be a plain numeric id")
    return text


def build_shop_process_id(row_id: object, attempt: object) -> int:
    """``payment_intents row id * 100 + attempt``.

    The row id comes from a database sequence, so the result stays far below the
    15 digits ``shop_process_id`` allows no matter how large Shopify order ids get.
    Callers resolve the order back through the stored ``provider_reference``,
    never by arithmetic on this value.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise VposInvalidIdError("attempt must be an integer")
    if not 0 <= attempt <= MAX_ATTEMPT:
        raise VposInvalidIdError("attempt must be between 0 and 99")
    if isinstance(row_id, bool) or not isinstance(row_id, (int, str)):
        raise VposInvalidIdError("row id must be numeric")
    text = str(row_id)
    if not text.isascii() or not text.isdigit() or int(text) == 0:
        raise VposInvalidIdError("row id must be a positive plain numeric id")
    if len(text.lstrip("0")) > MAX_INTEGER_DIGITS:
        raise VposInvalidIdError("row id has more than 13 digits")
    spid = int(text) * 100 + attempt
    if spid == PROBE_SHOP_PROCESS_ID:
        raise VposInvalidIdError("shop_process_id collides with the reserved probe id")
    return spid


def vpos_description(name: str) -> str:
    return name.strip()[:_DESCRIPTION_MAX]


def vpos_base_url(environment: object) -> str:
    if not isinstance(environment, str) or environment not in _VPOS_BASE_URLS:
        raise VposConfigurationError("unknown vPOS environment")
    return _VPOS_BASE_URLS[environment]


# --- Inbound confirm callback (string-or-number tolerant, BVP-9) ------------

_DIGITS = re.compile(r"[0-9]+")
_DECIMAL_TEXT = re.compile(r"[0-9]+(\.[0-9]+)?")


@dataclass(frozen=True)
class VposConfirmCallback:
    shop_process_id: int
    amount: VposAmount
    iva_amount: Decimal
    ticket_number: int
    currency: str
    token: object


def _coerce_int(raw: object, field: str) -> int:
    if isinstance(raw, bool) or raw is None:
        raise VposCallbackShapeError(f"{field} must be an integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and _DIGITS.fullmatch(raw):
        value = int(raw)
    elif isinstance(raw, (float, Decimal)):
        number = Decimal(str(raw))
        if not number.is_finite() or number != number.to_integral_value():
            raise VposCallbackShapeError(f"{field} must be an integer")
        value = int(number)
    else:
        raise VposCallbackShapeError(f"{field} must be an integer")
    if value < 0:
        raise VposCallbackShapeError(f"{field} must not be negative")
    return value


def _coerce_decimal(raw: object, field: str) -> Decimal:
    if isinstance(raw, bool) or raw is None:
        raise VposCallbackShapeError(f"{field} must be a number")
    if isinstance(raw, str):
        if not _DECIMAL_TEXT.fullmatch(raw):
            raise VposCallbackShapeError(f"{field} must be a number")
        return Decimal(raw)
    if isinstance(raw, (int, float, Decimal)):
        number = Decimal(str(raw))
        if not number.is_finite() or number < 0:
            raise VposCallbackShapeError(f"{field} must be a non-negative number")
        return number
    raise VposCallbackShapeError(f"{field} must be a number")


def parse_confirm_operation(payload: object) -> VposConfirmCallback:
    """Coerce the callback ``operation`` object; numbers may be strings or numbers."""
    if not isinstance(payload, Mapping):
        raise VposCallbackShapeError("operation must be an object")
    for field in ("token", "shop_process_id", "amount", "iva_amount", "ticket_number", "currency"):
        if field not in payload:
            raise VposCallbackShapeError(f"missing field: {field}")
    currency = payload["currency"]
    if not isinstance(currency, str) or not currency:
        raise VposCallbackShapeError("currency must be a string")
    amount_number = _coerce_decimal(payload["amount"], "amount")
    try:
        amount = VposAmount.from_vpos_payload(amount_number)
    except VposInvalidAmountError as exc:
        raise VposCallbackShapeError("amount is not a valid vPOS amount") from exc
    return VposConfirmCallback(
        shop_process_id=_coerce_int(payload["shop_process_id"], "shop_process_id"),
        amount=amount,
        iva_amount=_coerce_decimal(payload["iva_amount"], "iva_amount"),
        ticket_number=_coerce_int(payload["ticket_number"], "ticket_number"),
        currency=currency,
        token=payload["token"],
    )


def verify_confirm_token(private_key: str, callback: VposConfirmCallback) -> bool:
    """Constant-time check of the callback token over the normalised amount."""
    if not isinstance(callback.token, str):
        return False
    expected = confirm_token(
        private_key, callback.shop_process_id, callback.amount, callback.currency
    )
    return hmac.compare_digest(expected.encode("utf-8"), callback.token.encode("utf-8"))


# --- Result classification ---------------------------------------------------


class ConfirmationOutcome(Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    UNKNOWN = "unknown"


_DECLINED_CODES = frozenset({"05", "12", "15", "51"})


def classify_response_code(code: object) -> ConfirmationOutcome:
    """Only ``"00"`` approves; unknown or absent codes are never paid."""
    if code == "00":
        return ConfirmationOutcome.APPROVED
    if isinstance(code, str) and code in _DECLINED_CODES:
        return ConfirmationOutcome.DECLINED
    return ConfirmationOutcome.UNKNOWN


class RollbackOutcome(Enum):
    BENIGN = "benign"
    OPERATOR_ACTION = "operator_action"
    OTHER = "other"


_BENIGN_ROLLBACK_KEYS = frozenset(
    {"RollbackSuccessful", "PaymentNotFoundError", "AlreadyRollbackedError"}
)


def classify_rollback_key(key: object) -> RollbackOutcome:
    """Classify by message key, never by the envelope status."""
    if not isinstance(key, str):
        return RollbackOutcome.OTHER
    if key in _BENIGN_ROLLBACK_KEYS:
        return RollbackOutcome.BENIGN
    if key == "TransactionAlreadyConfirmed":
        return RollbackOutcome.OPERATOR_ACTION
    return RollbackOutcome.OTHER


class RollbackVerdict(str, Enum):
    """What an explicit rollback actually did, as the operator needs to read it."""

    SUCCEEDED = "succeeded"
    NO_OP = "no_op"
    MANUAL_ANNULMENT_REQUIRED = "manual_annulment_required"
    TRANSPORT_FAILURE = "transport_failure"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class RollbackResult:
    """Classified answer of one rollback call; ``key`` is Bancard's message key, never a secret."""

    verdict: RollbackVerdict
    key: str | None = None


def classify_rollback_verdict(key: object) -> RollbackVerdict:
    """Finer-grained sibling of ``classify_rollback_key`` for the explicit rollback.

    ``PaymentNotFoundError`` and ``AlreadyRollbackedError`` are expected, benign
    no-ops; only ``RollbackSuccessful`` means a charge was actually reversed.
    """
    if key == "RollbackSuccessful":
        return RollbackVerdict.SUCCEEDED
    if key in {"PaymentNotFoundError", "AlreadyRollbackedError"}:
        return RollbackVerdict.NO_OP
    if key == "TransactionAlreadyConfirmed":
        return RollbackVerdict.MANUAL_ANNULMENT_REQUIRED
    return RollbackVerdict.UNEXPECTED


# --- Reconciliation metafields -------------------------------------------------

METAFIELD_NAMESPACE = "bims_bancard"
_METAFIELD_TEXT_MAX = 255
EXTRA_RESPONSE_ATTRIBUTES = (
    "payment_card_type",
    "payment_card_brand",
    "payment_masked_card_number",
    "payment_origin",
)
_CARD_ATTRIBUTES = ("payment_masked_card_number", "payment_card_brand", "payment_card_type")


def _metafield_text(raw: object) -> str | None:
    """One bounded line of text, or None when there is nothing worth writing."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    text = " ".join(str(raw).split())[:_METAFIELD_TEXT_MAX]
    return text or None


def confirmation_metafields(
    shop_process_id: int, confirmation: Mapping[str, object], confirmed_at: datetime
) -> list[OrderMetafield]:
    """Order metafields for an APPROVED ``get_confirmation`` answer.

    Built only from the out-of-band confirmation, never from the callback body.
    Optional values are written only when Bancard sent them. Nothing here is
    secret: the masked card number is already masked by Bancard, and no token
    or key is ever copied.
    """

    def make(key: str, type_: str, value: str) -> OrderMetafield:
        return OrderMetafield(namespace=METAFIELD_NAMESPACE, key=key, type=type_, value=value)

    text_type = "single_line_text_field"
    result = [make("shop_process_id", "number_integer", str(shop_process_id))]
    try:
        ticket = _coerce_int(confirmation.get("ticket_number"), "ticket_number")
    except VposCallbackShapeError:
        ticket = None
    if ticket is not None:
        result.append(make("ticket_number", "number_integer", str(ticket)))
    for key in ("response_code", "authorization_number"):
        value = _metafield_text(confirmation.get(key))
        if value is not None:
            result.append(make(key, text_type, value))
    extras = confirmation.get("extra_attributes")
    for key in _CARD_ATTRIBUTES:
        raw = extras.get(key) if isinstance(extras, Mapping) else None
        value = _metafield_text(raw if raw is not None else confirmation.get(key))
        if value is not None:
            result.append(make(key, text_type, value))
    security = confirmation.get("security_information")
    source = _metafield_text(security.get("card_source")) if isinstance(security, Mapping) else None
    if source is not None:
        result.append(make("card_source", text_type, source))
    stamp = confirmed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    result.append(make("confirmed_at", "date_time", stamp))
    return result
