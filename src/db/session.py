"""Async engine + session factory.

`get_async_session()` — FastAPI dependency injection.
`async_session()` — context manager for scripts/CLI.

Engine is lazy-initialized — `init_engine()` is called by the first request
or explicitly in tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    """Reads DATABASE_URL from env. Raises ValueError if it is not set.

    Expected format:
        postgresql+asyncpg://user:pass@host:5432/db
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ValueError(
            "DATABASE_URL is not set. Export it in .env: "
            "DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db"
        )
    # If someone specified a sync URL — convert it to async
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def init_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Creates the engine (idempotent). Called on first use."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    final_url = url or get_database_url()
    _engine = create_async_engine(
        final_url,
        echo=echo,
        pool_pre_ping=True,         # checks the connection before use
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,           # recycle after an hour (PG idle timeouts)
    )
    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,      # don't leave ObjectsExpired after commit
        autoflush=False,             # we control flush manually
    )
    return _engine


def async_engine() -> AsyncEngine:
    """Returns (or creates) the engine."""
    return init_engine()


@asynccontextmanager
async def async_session() -> AsyncIterator[AsyncSession]:
    """Context manager for CLI/scripts:

        async with async_session() as session:
            ...
            await session.commit()
    """
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency:

        @app.get(...)
        async def handler(session: AsyncSession = Depends(get_async_session)):
            ...
    """
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose() -> None:
    """Closes the engine. Called on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
