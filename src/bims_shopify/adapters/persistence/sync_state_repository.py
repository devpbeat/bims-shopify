"""SQLAlchemy-backed SyncStateRepository implementation."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.application.sync_interval import RECENT_RUNS_WINDOW
from bims_shopify.datetime_utils import ensure_aware_utc

from .models import ProcessedEventModel, SyncStateModel


#: Entries kept in ``sync_duration_history`` PER MODE. Per mode, not overall:
#: the push worker records far more often than the pull and would otherwise
#: evict every pull measurement the pull interval floor is derived from.
DURATION_HISTORY_CAP = 40


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

    async def clear_last_error(self, tenant_id: int, only_prefix: str | None = None) -> None:
        """Clear a stale error after a successful sync so sync-status stops
        surfacing errors from runs that have since succeeded.

        With ``only_prefix`` (``"pull: "`` / ``"push: "``) the error is cleared
        only if it came from that worker: the pull and push share one error
        slot, and a healthy push must not hide a pull that is still failing.
        """
        model = await self._get_or_create_state(tenant_id)
        if only_prefix is not None and not (model.last_error or "").startswith(only_prefix):
            return
        model.last_error = None
        model.last_error_at = None
        await self._session.commit()

    async def set_last_run_summary(self, tenant_id: int, summary: dict) -> None:
        model = await self._get_or_create_state(tenant_id)
        model.last_run_summary = summary
        await self._session.commit()

    async def record_run_duration(
        self, tenant_id: int, seconds: float, mode: str, when: datetime | None = None
    ) -> None:
        """Append a completed run's duration (used to derive the minimum safe
        scheduler interval). Assigns a new list so the JSON column is flagged
        dirty; trims to the most recent ``DURATION_HISTORY_CAP`` entries."""
        model = await self._get_or_create_state(tenant_id)
        entry = {
            "at": (when or datetime.now(UTC)).isoformat(),
            "seconds": float(seconds),
            "mode": mode,
        }
        history = [*(model.sync_duration_history or []), entry]
        kept: list[dict] = []
        per_mode: dict[str, int] = {}
        for item in reversed(history):  # newest first, so the newest survive
            seen = per_mode.get(item.get("mode"), 0)
            if seen < DURATION_HISTORY_CAP:
                per_mode[item.get("mode")] = seen + 1
                kept.append(item)
        model.sync_duration_history = list(reversed(kept))
        await self._session.commit()

    async def get_recent_durations(
        self, tenant_id: int, mode: str, limit: int = RECENT_RUNS_WINDOW
    ) -> list[float]:
        model = await self._get_or_create_state(tenant_id)
        runs = [e["seconds"] for e in (model.sync_duration_history or []) if e.get("mode") == mode]
        return runs[-limit:]

    async def get_status(self, tenant_id: int) -> dict:
        model = await self._get_or_create_state(tenant_id)
        last_run_at = ensure_aware_utc(model.last_run_at)
        last_error_at = ensure_aware_utc(model.last_error_at)
        return {
            "last_run_at": last_run_at.isoformat() if last_run_at else None,
            "last_error": model.last_error,
            "last_error_at": last_error_at.isoformat() if last_error_at else None,
            "last_run_summary": dict(model.last_run_summary or {}),
            "last_pull_outcome": dict(model.last_pull_outcome) if model.last_pull_outcome else None,
            "last_push_outcome": dict(model.last_push_outcome) if model.last_push_outcome else None,
        }

    async def set_last_pull_outcome(self, tenant_id: int, outcome: dict) -> None:
        """What the most recent pull TICK did: ok / rejected / skipped / failed."""
        model = await self._get_or_create_state(tenant_id)
        model.last_pull_outcome = outcome
        await self._session.commit()

    async def set_last_push_outcome(self, tenant_id: int, outcome: dict) -> None:
        """What the most recent push TICK did: ok / unchanged / declined / ..."""
        model = await self._get_or_create_state(tenant_id)
        model.last_push_outcome = outcome
        await self._session.commit()

    async def get_last_push_outcome(self, tenant_id: int) -> dict | None:
        model = await self._get_or_create_state(tenant_id)
        return dict(model.last_push_outcome) if model.last_push_outcome else None

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
