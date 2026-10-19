"""Pure domain core for the direct Bancard vPOS 2.0 integration.

No I/O lives here. The module owns the three things that must never drift:

* the amount string (``VposAmount``): the md5 token and the request body read
  the same instance, so two divergent strings cannot exist;
* the md5 token concatenations, which take a ``VposAmount`` and never a raw
  number or string;
* the ``shop_process_id`` derivation from a Shopify order id.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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


def build_shop_process_id(order_id: object, attempt: object) -> int:
    """``order_id * 100 + attempt``; reversible with ``// 100``."""
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise VposInvalidIdError("attempt must be an integer")
    if not 0 <= attempt <= MAX_ATTEMPT:
        raise VposInvalidIdError("attempt must be between 0 and 99")
    if isinstance(order_id, bool) or not isinstance(order_id, (int, str)):
        raise VposInvalidIdError("order id must be numeric")
    text = str(order_id)
    if not text.isascii() or not text.isdigit():
        raise VposInvalidIdError("order id must be a plain numeric id")
    if len(text) > MAX_INTEGER_DIGITS:
        raise VposInvalidIdError("order id has more than 13 digits")
    spid = int(text) * 100 + attempt
    if spid == PROBE_SHOP_PROCESS_ID:
        raise VposInvalidIdError("shop_process_id collides with the reserved probe id")
    return spid


def order_id_from_shop_process_id(shop_process_id: int) -> str:
    _require_spid(shop_process_id)
    return str(shop_process_id // 100)


def vpos_description(name: str) -> str:
    return name.strip()[:_DESCRIPTION_MAX]


def vpos_base_url(environment: object) -> str:
    if not isinstance(environment, str) or environment not in _VPOS_BASE_URLS:
        raise VposConfigurationError("unknown vPOS environment")
    return _VPOS_BASE_URLS[environment]
