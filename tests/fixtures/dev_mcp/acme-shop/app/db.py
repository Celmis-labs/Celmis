"""Database engine for the shop service."""

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine

from app.config import get_settings


def build_engine() -> Engine:
    """One pooled SQLAlchemy engine, built from the DB_* environment variables."""
    s = get_settings()
    url = URL.create(
        "postgresql+psycopg",
        username=s.db_user,
        password=s.db_password,
        host=s.db_host,
        port=s.db_port,
        database=s.db_name,
    )
    return create_engine(url, pool_size=10, pool_pre_ping=True)
