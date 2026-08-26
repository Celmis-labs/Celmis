"""Alembic env.py — async setup для Celmis Postgres."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection

from alembic import context

# Завантажуємо .env (для DATABASE_URL) якщо ще не у env
_dotenv_path = Path(__file__).resolve().parent.parent / ".env"
if _dotenv_path.exists():
    for raw in _dotenv_path.read_text().splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


# Імпортуємо наші моделі — Base.metadata треба для autogenerate
from src.db.base import Base  # noqa: E402
from src.db.models import Chat, Message, Project, ProjectRepo  # noqa: E402,F401

target_metadata = Base.metadata


config = context.config

# Override sqlalchemy.url з env (бо у .ini може бути placeholder)
db_url = os.environ.get("DATABASE_URL", "").strip()
if db_url:
    # Alembic в online mode використовує sync engine для DDL — переключаємо
    # asyncpg → psycopg sync якщо це async URL
    if db_url.startswith("postgresql+asyncpg://"):
        sync_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://"):
        sync_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
    else:
        sync_url = db_url
    config.set_main_option("sqlalchemy.url", sync_url)


if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Generate SQL без підключення (`alembic upgrade head --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Sync mode для psycopg (Alembic DDL операції)."""
    from sqlalchemy import create_engine

    cfg = config.get_section(config.config_ini_section, {}) or {}
    url = cfg.get("sqlalchemy.url") or config.get_main_option("sqlalchemy.url")

    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
