"""build_provider maps each configured provider name to its adapter."""
from __future__ import annotations

import pytest

from bims_shopify.adapters.payments.bancard_via_bims_provider import BancardViaBIMSProvider
from bims_shopify.adapters.payments.bancard_vpos_provider import BancardVposProvider
from bims_shopify.adapters.payments.factory import UnknownPaymentProviderError, build_provider
from bims_shopify.adapters.payments.pagopar_provider import PagoparProvider
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import VposConfigurationError


def test_pagopar(tenant):
    assert isinstance(build_provider("pagopar", tenant), PagoparProvider)


def test_legacy_bancard_is_not_vpos(tenant):
    provider = build_provider("bancard", tenant)
    assert isinstance(provider, BancardViaBIMSProvider)
    assert not isinstance(provider, BancardVposProvider)


def test_vpos(tenant):
    provider = build_provider("bancard_vpos", tenant, settings=Settings(), audit=object())
    assert isinstance(provider, BancardVposProvider)


def test_vpos_requires_settings(tenant):
    with pytest.raises(VposConfigurationError):
        build_provider("bancard_vpos", tenant)


def test_unknown(tenant):
    with pytest.raises(UnknownPaymentProviderError):
        build_provider("nope", tenant)
