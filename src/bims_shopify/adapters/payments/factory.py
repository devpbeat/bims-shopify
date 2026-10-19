"""Single place that maps a configured provider name to its adapter."""
from __future__ import annotations

from typing import Any

from bims_shopify.adapters.bims.client import BIMSClient
from bims_shopify.adapters.payments.bancard_via_bims_provider import BancardViaBIMSProvider
from bims_shopify.adapters.payments.bancard_vpos_provider import AuditPort, BancardVposProvider
from bims_shopify.adapters.payments.pagopar_provider import PagoparProvider
from bims_shopify.config import Settings
from bims_shopify.domain.bancard_vpos import VposConfigurationError
from bims_shopify.domain.tenant import PaymentProvider, Tenant


class UnknownPaymentProviderError(ValueError):
    """The provider name does not match any supported payment adapter."""


def build_provider(
    provider_name: str,
    tenant: Tenant,
    *,
    settings: Settings | None = None,
    audit: AuditPort | None = None,
) -> Any:
    """Build the adapter for ``provider_name``; vPOS also needs settings."""
    if provider_name == PaymentProvider.PAGOPAR.value:
        return PagoparProvider()
    if provider_name == PaymentProvider.BANCARD.value:
        return BancardViaBIMSProvider(BIMSClient(tenant))
    if provider_name == PaymentProvider.BANCARD_VPOS.value:
        if settings is None:
            raise VposConfigurationError("settings are required to build the vPOS provider")
        return BancardVposProvider(settings, audit=audit)
    raise UnknownPaymentProviderError(f"Unknown payment provider: {provider_name}")
