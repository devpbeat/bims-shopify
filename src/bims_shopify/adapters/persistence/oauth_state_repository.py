"""DB-backed store for Shopify OAuth state nonces (CSRF protection)."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OAuthStateModel

STATE_TTL_SECONDS = 600


def _utcnow_naive() -> datetime:
    """Timezone-aware "now", stripped to naive UTC for storage.

    The `oauth_states` table uses a plain (non-timezone-aware) DateTime
    column, and SQLite round-trips any stored datetime as naive (tzinfo is
    dropped on read). Building the value via `datetime.now(UTC)` avoids the
    deprecated `datetime.utcnow()` while keeping what we persist and later
    compare naive-UTC on both sides, so comparisons never mix aware and
    naive datetimes.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class SqlAlchemyOAuthStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, shop: str, ttl_seconds: int = STATE_TTL_SECONDS) -> str:
        """Generate and persist a fresh state nonce for `shop`, returning it."""
        state = secrets.token_urlsafe(32)
        now = _utcnow_naive()
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
        return not model.expires_at < _utcnow_naive()
