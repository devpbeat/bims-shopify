"""SQLAlchemy-backed repository for `portal_users` (named portal logins)."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PortalUserModel


class SqlAlchemyPortalUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_tenant_and_email(self, tenant_id: int, email: str) -> PortalUserModel | None:
        result = await self._session.execute(
            select(PortalUserModel).where(
                PortalUserModel.tenant_id == tenant_id,
                PortalUserModel.email == email,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> PortalUserModel | None:
        return await self._session.get(PortalUserModel, user_id)

    async def upsert(
        self,
        *,
        tenant_id: int,
        email: str,
        password_hash: str,
        name: str | None,
    ) -> PortalUserModel:
        """Create the user, or update password/name if the email already exists for this tenant."""
        existing = await self.get_by_tenant_and_email(tenant_id, email)
        if existing is not None:
            existing.password_hash = password_hash
            existing.name = name
            existing.active = True
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        model = PortalUserModel(
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            name=name,
            active=True,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model

    async def record_login(self, user_id: int) -> None:
        model = await self._session.get(PortalUserModel, user_id)
        if model is not None:
            model.last_login_at = datetime.now(UTC)
            await self._session.commit()
