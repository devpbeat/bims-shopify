"""Async SQLAlchemy engine/session setup."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bims_shopify.config import Settings


def create_engine_and_sessionmaker(settings: Settings):
    engine = create_async_engine(settings.database_url, echo=False, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory
