"""Shared FastAPI dependencies: DB session, tenant repository, admin auth."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.adapters.persistence.crypto import SecretBox
from bims_shopify.adapters.persistence.tenant_repository import (
    SqlAlchemyTenantRepository,
)
from bims_shopify.config import Settings


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_tenant_repository(
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> SqlAlchemyTenantRepository:
    return SqlAlchemyTenantRepository(session, SecretBox(settings.fernet_key))


async def require_admin(
    authorization: str = Header(default=""), settings: Settings = Depends(get_settings)
) -> None:
    expected = f"Bearer {settings.admin_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
