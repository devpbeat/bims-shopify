"""SQLAlchemy-backed store for the persisted BIMS catalog snapshot.

WRITE STRATEGY (generation swap, not delete-and-reinsert)

A pull never touches the rows readers are using. ``stage`` writes the new pull
as its own ``building`` generation (rows keyed by a fresh ``snapshot_id``,
committed in chunks so one huge transaction never holds the database), and
``promote`` makes it visible by flipping ``is_current`` in ONE short
transaction. Consequences:

* a reader that resolves the current id and reads its rows only ever sees a
  whole snapshot: readers filter on the id of a ``current`` row, and a
  ``building`` generation is never current;
* a failure, or a container killed mid-write, leaves an invisible
  ``building`` orphan that the next ``stage`` deletes; the previous snapshot
  is untouched;
* the previous generation keeps its rows until the NEXT promote, so a reader
  that picked it just before a flip can still finish reading it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bims_shopify.datetime_utils import ensure_aware_utc
from bims_shopify.domain.snapshot import (
    STATUS_BUILDING,
    STATUS_CURRENT,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    CatalogPull,
    CatalogRow,
    CatalogSnapshot,
    SnapshotMeta,
    StockRecord,
)

from .models import (
    CatalogSnapshotModel,
    CatalogSnapshotProductModel,
    CatalogSnapshotStockModel,
)

#: Rows per INSERT/commit while staging.
DEFAULT_CHUNK_SIZE = 2000

#: Metadata rows kept per tenant (rows of old generations are purged far sooner).
META_HISTORY_CAP = 50


def _chunks(items: list[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SqlAlchemySnapshotRepository:
    def __init__(self, session: AsyncSession, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self._session = session
        self._chunk_size = chunk_size

    # -- writing --------------------------------------------------------

    async def stage(self, tenant_id: int, pull: CatalogPull, started_at: datetime) -> SnapshotMeta:
        """Write ``pull`` as an invisible ``building`` generation.

        Any earlier ``building`` generation of the tenant is an orphan (the
        caller serialises pulls per tenant) and is deleted first. If writing
        fails the generation is deleted and marked ``failed`` before the error
        propagates, so nothing of it stays behind.
        """
        await self._delete_generations(tenant_id, STATUS_BUILDING)
        model = CatalogSnapshotModel(
            tenant_id=tenant_id,
            status=STATUS_BUILDING,
            is_current=False,
            started_at=started_at,
            rows_pulled=len(pull.rows),
            stock_rows=len(pull.stock),
            pages=pull.pages,
            reported_count=pull.reported_count,
            complete=pull.complete,
            stop_reason=pull.stop_reason,
            warnings=list(pull.warnings),
            timings=dict(pull.timings),
        )
        self._session.add(model)
        await self._session.commit()
        snapshot_id = model.id
        try:
            for chunk in _chunks(pull.rows, self._chunk_size):
                await self._insert_products(snapshot_id, chunk)
                await self._session.commit()
            entries = [
                (
                    sku,
                    quantity,
                    {str(wid): qty for wid, qty in pull.stock_by_warehouse.get(sku, {}).items()},
                )
                for sku, quantity in pull.stock.items()
            ]
            for chunk in _chunks(entries, self._chunk_size):
                await self._insert_stock(snapshot_id, chunk)
                await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            await self._abort(snapshot_id, f"{type(exc).__name__}: {exc}")
            raise
        return await self._meta_by_id(snapshot_id)

    async def _insert_products(self, snapshot_id: int, rows: list[CatalogRow]) -> None:
        await self._session.execute(
            insert(CatalogSnapshotProductModel),
            [
                {
                    "snapshot_id": snapshot_id,
                    "bims_id": row.bims_id,
                    "sku": row.sku,
                    "code2": row.code2,
                    "name": row.name[:500],
                    "sell_price": row.sell_price,
                    "enabled": row.enabled,
                    "exclude_ecommerce": row.exclude_ecommerce,
                    "company_id": row.company_id,
                }
                for row in rows
            ],
        )

    async def _insert_stock(
        self, snapshot_id: int, entries: list[tuple[str, float, dict[str, float]]]
    ) -> None:
        await self._session.execute(
            insert(CatalogSnapshotStockModel),
            [
                {"snapshot_id": snapshot_id, "sku": sku, "quantity": quantity, "by_warehouse": by_wh}
                for sku, quantity, by_wh in entries
            ],
        )

    async def _abort(self, snapshot_id: int, reason: str) -> None:
        await self._delete_rows([snapshot_id])
        await self._session.execute(
            update(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.id == snapshot_id)
            .values(status=STATUS_FAILED, rejection={"reason": "write_failed", "error": reason[:500]})
        )
        await self._session.commit()

    async def promote(
        self,
        snapshot_id: int,
        finished_at: datetime,
        stats: dict[str, Any] | None = None,
        extra_timings: dict[str, Any] | None = None,
    ) -> SnapshotMeta:
        """Make a staged generation the tenant's current snapshot (atomically)."""
        tenant_id, timings = (
            await self._session.execute(
                select(CatalogSnapshotModel.tenant_id, CatalogSnapshotModel.timings).where(
                    CatalogSnapshotModel.id == snapshot_id
                )
            )
        ).one()
        # One transaction: demote the old current, promote the new one. The
        # partial unique index makes a second current impossible.
        await self._session.execute(
            update(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.tenant_id == tenant_id, CatalogSnapshotModel.is_current.is_(True))
            .values(is_current=False, status=STATUS_SUPERSEDED)
        )
        await self._session.execute(
            update(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.id == snapshot_id)
            .values(
                status=STATUS_CURRENT,
                is_current=True,
                finished_at=finished_at,
                stats=stats,
                timings={**(timings or {}), **(extra_timings or {})},
            )
        )
        await self._session.commit()
        await self._purge(tenant_id)
        return await self._meta_by_id(snapshot_id)

    async def set_stats(self, snapshot_id: int, stats: dict[str, Any]) -> None:
        await self._session.execute(
            update(CatalogSnapshotModel).where(CatalogSnapshotModel.id == snapshot_id).values(stats=stats)
        )
        await self._session.commit()

    async def record_rejected(
        self,
        tenant_id: int,
        pull: CatalogPull,
        *,
        started_at: datetime,
        finished_at: datetime,
        rejection: dict[str, Any],
    ) -> SnapshotMeta:
        """Record a pull that was refused. Stores metadata only, no rows."""
        model = CatalogSnapshotModel(
            tenant_id=tenant_id,
            status=STATUS_REJECTED,
            is_current=False,
            started_at=started_at,
            finished_at=finished_at,
            rows_pulled=len(pull.rows),
            stock_rows=len(pull.stock),
            pages=pull.pages,
            reported_count=pull.reported_count,
            complete=pull.complete,
            stop_reason=pull.stop_reason,
            warnings=list(pull.warnings),
            timings=dict(pull.timings),
            rejection=rejection,
        )
        self._session.add(model)
        await self._session.commit()
        await self._purge(tenant_id)
        return await self._meta_by_id(model.id)

    async def _delete_rows(self, snapshot_ids: list[int]) -> None:
        if not snapshot_ids:
            return
        for model in (CatalogSnapshotProductModel, CatalogSnapshotStockModel):
            await self._session.execute(delete(model).where(model.snapshot_id.in_(snapshot_ids)))
        await self._session.commit()

    async def _delete_generations(self, tenant_id: int, status: str) -> None:
        ids = (
            await self._session.execute(
                select(CatalogSnapshotModel.id).where(
                    CatalogSnapshotModel.tenant_id == tenant_id, CatalogSnapshotModel.status == status
                )
            )
        ).scalars().all()
        await self._delete_rows(list(ids))
        if ids:
            await self._session.execute(
                update(CatalogSnapshotModel)
                .where(CatalogSnapshotModel.id.in_(ids))
                .values(
                    status=STATUS_FAILED,
                    rejection={"reason": "abandoned", "error": "process stopped before the write finished"},
                )
            )
            await self._session.commit()

    async def _purge(self, tenant_id: int) -> None:
        """Drop the rows of every generation except current and the most recent
        previous one; trim the metadata history."""
        ordered = (
            await self._session.execute(
                select(CatalogSnapshotModel.id, CatalogSnapshotModel.status)
                .where(CatalogSnapshotModel.tenant_id == tenant_id)
                .order_by(CatalogSnapshotModel.id.desc())
            )
        ).all()
        keep: set[int] = set()
        previous_kept = False
        for snapshot_id, status in ordered:
            if status == STATUS_CURRENT:
                keep.add(snapshot_id)
            elif status == STATUS_BUILDING:
                keep.add(snapshot_id)  # a write in progress
            elif status == STATUS_SUPERSEDED and not previous_kept:
                keep.add(snapshot_id)
                previous_kept = True
        await self._delete_rows([sid for sid, _ in ordered if sid not in keep])
        for sid in [sid for sid, _ in ordered[META_HISTORY_CAP:]]:
            await self._session.execute(delete(CatalogSnapshotModel).where(CatalogSnapshotModel.id == sid))
        await self._session.commit()

    # -- reading --------------------------------------------------------

    @staticmethod
    def _to_meta(model: CatalogSnapshotModel) -> SnapshotMeta:
        return SnapshotMeta(
            id=model.id,
            tenant_id=model.tenant_id,
            status=model.status,
            started_at=ensure_aware_utc(model.started_at),
            finished_at=ensure_aware_utc(model.finished_at),
            rows_pulled=model.rows_pulled,
            stock_rows=model.stock_rows,
            pages=model.pages,
            reported_count=model.reported_count,
            complete=model.complete,
            stop_reason=model.stop_reason,
            warnings=list(model.warnings or []),
            timings=dict(model.timings or {}),
            stats=dict(model.stats) if model.stats else None,
            rejection=dict(model.rejection) if model.rejection else None,
        )

    async def _meta_by_id(self, snapshot_id: int) -> SnapshotMeta:
        result = await self._session.execute(
            select(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.id == snapshot_id)
            .execution_options(populate_existing=True)
        )
        return self._to_meta(result.scalar_one())

    async def get_current_meta(self, tenant_id: int) -> SnapshotMeta | None:
        result = await self._session.execute(
            select(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.tenant_id == tenant_id, CatalogSnapshotModel.is_current.is_(True))
            .execution_options(populate_existing=True)
        )
        model = result.scalar_one_or_none()
        return self._to_meta(model) if model is not None else None

    async def list_recent_meta(self, tenant_id: int, limit: int = 10) -> list[SnapshotMeta]:
        result = await self._session.execute(
            select(CatalogSnapshotModel)
            .where(CatalogSnapshotModel.tenant_id == tenant_id)
            .order_by(CatalogSnapshotModel.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [self._to_meta(model) for model in result.scalars().all()]

    async def load_current(self, tenant_id: int) -> CatalogSnapshot | None:
        """The current snapshot with all its rows, read by a single snapshot id."""
        meta = await self.get_current_meta(tenant_id)
        return None if meta is None else await self.load(meta)

    async def load(self, meta: SnapshotMeta) -> CatalogSnapshot:
        """Rows of the snapshot ``meta`` describes.

        Callers pick the id first (``get_current_meta``) and read by it, so a
        flip to a newer snapshot in between cannot mix two generations; the
        previous generation keeps its rows until the next promote.
        """
        product_rows = (
            await self._session.execute(
                select(
                    CatalogSnapshotProductModel.bims_id,
                    CatalogSnapshotProductModel.sku,
                    CatalogSnapshotProductModel.code2,
                    CatalogSnapshotProductModel.name,
                    CatalogSnapshotProductModel.sell_price,
                    CatalogSnapshotProductModel.enabled,
                    CatalogSnapshotProductModel.exclude_ecommerce,
                    CatalogSnapshotProductModel.company_id,
                )
                .where(CatalogSnapshotProductModel.snapshot_id == meta.id)
                .order_by(CatalogSnapshotProductModel.id)
            )
        ).all()
        stock_rows = (
            await self._session.execute(
                select(
                    CatalogSnapshotStockModel.sku,
                    CatalogSnapshotStockModel.quantity,
                    CatalogSnapshotStockModel.by_warehouse,
                ).where(CatalogSnapshotStockModel.snapshot_id == meta.id)
            )
        ).all()
        rows = [CatalogRow(*row) for row in product_rows]
        stock = {
            sku: StockRecord(
                quantity=float(quantity),
                by_warehouse={int(wid): float(qty) for wid, qty in (by_wh or {}).items()},
            )
            for sku, quantity, by_wh in stock_rows
        }
        return CatalogSnapshot(meta=meta, rows=rows, stock=stock)

