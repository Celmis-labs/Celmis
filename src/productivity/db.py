"""One process-wide sync engine for the productivity tables.

The same lazy shape as `src.review.issues._engine`: built on first use, from
the configured database URL, with the async driver name swapped for the sync
one. Every function in this package takes `engine=None` so a test can hand in
a SQLite engine instead.
"""

from __future__ import annotations

import threading

_ENGINE = None
_LOCK = threading.Lock()


def get_engine(engine=None):
    global _ENGINE
    if engine is not None:
        return engine
    if _ENGINE is not None:
        return _ENGINE
    with _LOCK:
        if _ENGINE is None:
            from sqlalchemy import create_engine

            from src.db.session import get_database_url

            url = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
            _ENGINE = create_engine(
                url, pool_pre_ping=True, pool_size=2, max_overflow=3, pool_recycle=1800,
            )
    return _ENGINE
