"""SQLAlchemy-backed TenantRepository implementation."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.domain.tenant import PaymentProvider, ReorderStrategy, Tenant

from .crypto import SecretBox
from .models import TenantModel


class SqlAlchemyTenantRepository:
    def __init__(self, session: AsyncSession, secret_box: SecretBox) -> None:
        self._session = session
        self._secrets = secret_box

    def _to_domain(self, model: TenantModel) -> Tenant:
        return Tenant(
            id=model.id,
            slug=model.slug,
            bims_base_url=model.bims_base_url,
            bims_api_key=self._secrets.decrypt(model.bims_api_key_encrypted),
            shopify_shop_domain=model.shopify_shop_domain,
            shopify_access_token=self._secrets.decrypt(model.shopify_access_token_encrypted),
            shopify_webhook_secret=model.shopify_webhook_secret,
            shopify_location_id=model.shopify_location_id,
            bims_posale_id=model.bims_posale_id,
            bims_warehouse_id=model.bims_warehouse_id,
            bims_warehouse_ids=list(model.bims_warehouse_ids or []),
            bims_company_id=model.bims_company_id,
            bims_currency_id=model.bims_currency_id,
            bims_payment_method_id=model.bims_payment_method_id,
            default_customer_contact_id=model.default_customer_contact_id,
            reorder_threshold=model.reorder_threshold,
            reorder_strategy=ReorderStrategy(model.reorder_strategy),
            bims_timezone=model.bims_timezone,
            payment_provider=PaymentProvider(model.payment_provider) if model.payment_provider else None,
            provider_config=model.provider_config or {},
            field_mappings=model.field_mappings or {},
            active=model.active,
        )

    def _apply_domain(self, model: TenantModel, tenant: Tenant) -> None:
        model.slug = tenant.slug
        model.bims_base_url = tenant.bims_base_url
        model.bims_api_key_encrypted = self._secrets.encrypt(tenant.bims_api_key)
        model.shopify_shop_domain = tenant.shopify_shop_domain
        model.shopify_access_token_encrypted = self._secrets.encrypt(tenant.shopify_access_token)
        model.shopify_webhook_secret = tenant.shopify_webhook_secret
        model.shopify_location_id = tenant.shopify_location_id
        model.bims_posale_id = tenant.bims_posale_id
        model.bims_warehouse_id = tenant.bims_warehouse_id
        model.bims_warehouse_ids = list(tenant.bims_warehouse_ids or [])
        model.bims_company_id = tenant.bims_company_id
        model.bims_currency_id = tenant.bims_currency_id
        model.bims_payment_method_id = tenant.bims_payment_method_id
        model.default_customer_contact_id = tenant.default_customer_contact_id
        model.reorder_threshold = tenant.reorder_threshold
        model.reorder_strategy = tenant.reorder_strategy.value
        model.bims_timezone = tenant.bims_timezone
        model.payment_provider = tenant.payment_provider.value if tenant.payment_provider else None
        model.provider_config = tenant.provider_config
        model.field_mappings = tenant.field_mappings
        model.active = tenant.active

    async def get_by_slug(self, slug: str) -> Tenant | None:
        result = await self._session.execute(select(TenantModel).where(TenantModel.slug == slug))
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_shopify_domain(self, shopify_shop_domain: str) -> Tenant | None:
        result = await self._session.execute(
            select(TenantModel).where(TenantModel.shopify_shop_domain == shopify_shop_domain)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_id(self, tenant_id: int) -> Tenant | None:
        model = await self._session.get(TenantModel, tenant_id)
        return self._to_domain(model) if model else None

    async def list_active(self) -> list[Tenant]:
        result = await self._session.execute(select(TenantModel).where(TenantModel.active.is_(True)))
        return [self._to_domain(model) for model in result.scalars().all()]

    async def list_all(self) -> list[Tenant]:
        result = await self._session.execute(select(TenantModel))
        return [self._to_domain(model) for model in result.scalars().all()]

    async def create(self, tenant: Tenant) -> Tenant:
        model = TenantModel(slug=tenant.slug)
        self._apply_domain(model, tenant)
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return self._to_domain(model)

    async def update(self, tenant: Tenant) -> Tenant:
        model = await self._session.get(TenantModel, tenant.id)
        if model is None:
            raise ValueError(f"Tenant {tenant.id} not found")
        self._apply_domain(model, tenant)
        await self._session.commit()
        await self._session.refresh(model)
        return self._to_domain(model)

    async def delete(self, tenant_id: int) -> None:
        model = await self._session.get(TenantModel, tenant_id)
        if model is not None:
            await self._session.delete(model)
            await self._session.commit()
