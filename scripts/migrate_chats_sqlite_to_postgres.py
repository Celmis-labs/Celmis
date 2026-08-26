"""One-shot migration: SQLite chats.db → Postgres chats/messages.

Реюзує `src/chat/store.py` (sync SQLite) для читання + `src/db/models.py` (async PG)
для запису. Idempotent: якщо chat з тим самим UUID уже існує — пропускає.

Usage:
    set -a && source .env && set +a
    python -m scripts.migrate_chats_sqlite_to_postgres

Опц.:
    --dry-run             — show what would be migrated, no writes
    --sqlite-path PATH    — override path (default з config: ~/code-analysis/chats.db
                            або docker /workspace/data/chats.db)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


async def migrate(sqlite_path: Path, *, dry_run: bool = False) -> dict:
    """Повертає stats. Standalone — читає SQLite через sqlite3, без src.chat."""
    import json
    import sqlite3

    from sqlalchemy import select

    from src.db import async_session
    from src.db.models import Chat, Message

    if not sqlite_path.exists():
        logger.error("sqlite file not found: %s", sqlite_path)
        return {"error": "file not found"}

    # Read SQLite напряму (схема — фіксована з src/chat/store.py яка зараз видалена)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    src_chats = list(conn.execute(
        "SELECT id, repo, name, created_at, updated_at FROM chats"
    ).fetchall())
    logger.info("source: %d chats у %s", len(src_chats), sqlite_path)

    stats = {
        "chats_total": len(src_chats),
        "chats_migrated": 0,
        "chats_skipped": 0,
        "messages_total": 0,
    }

    async with async_session() as pg:
        existing_ids = set(
            (await pg.execute(select(Chat.id))).scalars().all()
        )
        logger.info("postgres: %d existing chats", len(existing_ids))

        for row in src_chats:
            cid = row["id"]
            if cid in existing_ids:
                stats["chats_skipped"] += 1
                continue

            msgs = list(conn.execute(
                "SELECT role, content, timestamp, meta FROM messages "
                "WHERE chat_id = ? ORDER BY id",
                (cid,),
            ).fetchall())

            if dry_run:
                logger.info("[DRY] %s repo=%s msgs=%d", cid[:8], row["repo"], len(msgs))
                stats["chats_migrated"] += 1
                stats["messages_total"] += len(msgs)
                continue

            pg_chat = Chat(
                id=cid,
                repo_slug=row["repo"],
                project_id=None,
                name=row["name"] or None,
                owner_user_id=None,
            )
            for m in msgs:
                meta = json.loads(m["meta"]) if m["meta"] else None
                pg_chat.messages.append(
                    Message(role=m["role"], content=m["content"], meta=meta)
                )
            pg.add(pg_chat)
            stats["chats_migrated"] += 1
            stats["messages_total"] += len(msgs)
            logger.info("migrated %s msgs=%d", cid[:8], len(msgs))

        if not dry_run:
            await pg.commit()

    conn.close()
    return stats


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=None,
        help="Path to chats.db (default з settings)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Завантажити .env
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        import os
        for line in env_file.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    if args.sqlite_path:
        sqlite_path = args.sqlite_path
    else:
        from src.config import get_settings
        sqlite_path = get_settings().chats_db_path
    logger.info("SQLite source: %s", sqlite_path)

    stats = asyncio.run(migrate(sqlite_path, dry_run=args.dry_run))
    print()
    print("=" * 50)
    for k, v in stats.items():
        print(f"  {k:25s} {v}")
    print("=" * 50)
    if "error" in stats:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
