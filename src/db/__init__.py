"""Postgres backend for Celmis — projects, chats, messages.

SQLAlchemy 2.0 async via asyncpg. Alembic for migrations.

Usage:
    from src.db import async_session

    async with async_session() as session:
        result = await session.execute(select(Project))
        ...

Environment:
    DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db
"""

from src.db.base import Base
from src.db.session import (
    async_engine,
    async_session,
    get_async_session,
    get_database_url,
    init_engine,
)

__all__ = [
    "Base",
    "async_engine",
    "async_session",
    "get_async_session",
    "get_database_url",
    "init_engine",
]
