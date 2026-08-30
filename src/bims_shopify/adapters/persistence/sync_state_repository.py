"""SQLAlchemy-backed SyncStateRepository implementation."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.datetime_utils import ensure_aware_utc

from .models import ProcessedEventModel, SyncStateModel


class SqlAlchemySyncStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_or_create_state(self, tenant_id: int) -> SyncStateModel:
        result = await self._session.execute(
            select(SyncStateModel).where(SyncStateModel.tenant_id == tenant_id)
        )
        model = result.scalar_one_or_none()
        if model is None:
            model = SyncStateModel(tenant_id=tenant_id, product_hashes={})
            self._session.add(model)
            await self._session.commit()
            await self._session.refresh(model)
        return model

    async def get_last_run(self, tenant_id: int) -> datetime | None:
        model = await self._get_or_create_state(tenant_id)
        return ensure_aware_utc(model.last_run_at)

    async def set_last_run(self, tenant_id: int, when: datetime) -> None:
        model = await self._get_or_create_state(tenant_id)
        model.last_run_at = when
        await self._session.commit()

    async def set_last_error(self, tenant_id: int, message: str) -> None:
        model = await self._get_or_create_state(tenant_id)
        model.last_error = message[:2000]
        model.last_error_at = datetime.now(UTC)
        await self._session.commit()

    async def set_last_run_summary(self, tenant_id: int, summary: dict) -> None:
        model = await self._get_or_create_state(tenant_id)
        model.last_run_summary = summary
        await self._session.commit()

    async def get_status(self, tenant_id: int) -> dict:
        model = await self._get_or_create_state(tenant_id)
        last_run_at = ensure_aware_utc(model.last_run_at)
        last_error_at = ensure_aware_utc(model.last_error_at)
        return {
            "last_run_at": last_run_at.isoformat() if last_run_at else None,
            "last_error": model.last_error,
            "last_error_at": last_error_at.isoformat() if last_error_at else None,
            "last_run_summary": dict(model.last_run_summary or {}),
        }

    async def get_product_hashes(self, tenant_id: int) -> dict[str, str]:
        model = await self._get_or_create_state(tenant_id)
        return dict(model.product_hashes or {})

    async def set_product_hashes(self, tenant_id: int, hashes: dict[str, str]) -> None:
        model = await self._get_or_create_state(tenant_id)
        model.product_hashes = hashes
        await self._session.commit()

    async def has_processed_event(self, tenant_id: int, source: str, external_id: str) -> bool:
        result = await self._session.execute(
            select(ProcessedEventModel).where(
                ProcessedEventModel.tenant_id == tenant_id,
                ProcessedEventModel.source == source,
                ProcessedEventModel.external_id == external_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_event_status(self, tenant_id: int, source: str, external_id: str) -> str | None:
        result = await self._session.execute(
            select(ProcessedEventModel).where(
                ProcessedEventModel.tenant_id == tenant_id,
                ProcessedEventModel.source == source,
                ProcessedEventModel.external_id == external_id,
            )
        )
        model = result.scalar_one_or_none()
        return model.status if model is not None else None

    async def record_processed_event(
        self, tenant_id: int, source: str, external_id: str, payload_hash: str, status: str
    ) -> None:
        model = ProcessedEventModel(
            tenant_id=tenant_id,
            source=source,
            external_id=external_id,
            payload_hash=payload_hash,
            status=status,
        )
        self._session.add(model)
        await self._session.commit()

    async def try_claim_event(
        self, tenant_id: int, source: str, external_id: str, payload_hash: str
    ) -> bool:
        """Atomically claim (tenant_id, source, external_id) for processing.

        Relies on the DB-level unique constraint on ProcessedEventModel, so
        concurrent deliveries of the same webhook race on the INSERT itself
        instead of a SELECT-then-INSERT check (which is not atomic and would
        let both deliveries through). Returns False if another delivery
        already claimed this event.
        """
        model = ProcessedEventModel(
            tenant_id=tenant_id,
            source=source,
            external_id=external_id,
            payload_hash=payload_hash,
            status="processing",
        )
        self._session.add(model)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return False
        return True

    async def mark_event_status(
        self, tenant_id: int, source: str, external_id: str, status: str
    ) -> None:
        result = await self._session.execute(
            select(ProcessedEventModel).where(
                ProcessedEventModel.tenant_id == tenant_id,
                ProcessedEventModel.source == source,
                ProcessedEventModel.external_id == external_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is not None:
            model.status = status
            await self._session.commit()

    async def delete_event(self, tenant_id: int, source: str, external_id: str) -> None:
        result = await self._session.execute(
            select(ProcessedEventModel).where(
                ProcessedEventModel.tenant_id == tenant_id,
                ProcessedEventModel.source == source,
                ProcessedEventModel.external_id == external_id,
            )
        )
        model = result.scalar_one_or_none()
        if model is not None:
            await self._session.delete(model)
            await self._session.commit()


def hash_payload(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
