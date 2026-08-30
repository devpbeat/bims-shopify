"""DB-backed store for Shopify OAuth state nonces (CSRF protection)."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.datetime_utils import ensure_aware_utc

from .models import OAuthStateModel

STATE_TTL_SECONDS = 600


class SqlAlchemyOAuthStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, shop: str, ttl_seconds: int = STATE_TTL_SECONDS) -> str:
        """Generate and persist a fresh state nonce for `shop`, returning it."""
        state = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        model = OAuthStateModel(
            shop=shop,
            state=state,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._session.add(model)
        await self._session.commit()
        return state

    async def consume(self, shop: str, state: str) -> bool:
        """Validate and delete a state nonce; returns True iff it was valid.

        Valid means: it exists, matches `shop`, and has not expired. The
        nonce is deleted either way it is found, so it can never be replayed.
        """
        result = await self._session.execute(
            select(OAuthStateModel).where(OAuthStateModel.state == state)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return False

        await self._session.execute(delete(OAuthStateModel).where(OAuthStateModel.id == model.id))
        await self._session.commit()

        if model.shop != shop:
            return False
        expires_at = ensure_aware_utc(model.expires_at)
        return not expires_at < datetime.now(UTC)
