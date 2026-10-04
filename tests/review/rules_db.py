"""A throwaway SQLite database behind `src.db.session`, for the rules tests.

Not a test module. `rules_db` points both the async session factory (what
`src.review.rules_store` writes through) and DATABASE_URL (what the blocking
loader a review uses reads) at one file, with every table created.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


def _sqlite_booleans(dbapi_conn, _record) -> None:
    dbapi_conn.create_function("true", 0, lambda: 1)
    dbapi_conn.create_function("false", 0, lambda: 0)


@contextlib.asynccontextmanager
async def rules_db(tmp_path: Path, monkeypatch):
    import src.db.session as session_mod
    from src.db.models import Base

    db_file = tmp_path / "rules.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    sync = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync)
    sync.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False,
                                 autoflush=False)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(session_mod, "_session_factory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()
