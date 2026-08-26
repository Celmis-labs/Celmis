"""Where a conversation lives so that a deploy cannot take it away.

A session is an asyncio task holding a CLI subprocess, and the CLI keeps its
transcript under `CLAUDE_CONFIG_DIR` inside the container. Both die with the
container. That was tolerable while a session was one prompt; it is not
tolerable now that the workflow is "drop the logs, go for coffee, come back
tomorrow and carry on" — the coming back is the part that did not exist.

The SDK offers exactly the hook needed: `ClaudeAgentOptions.session_store`
takes a duck-typed adapter with `append` and `load`, and mirrors every
transcript entry to it as the conversation happens. Passing `resume=<id>` on a
later run replays from the same adapter. So the durable copy goes to Postgres,
beside everything else about the session, and the container keeps only a
scratch copy it is welcome to lose.

Two decisions worth stating, because both could reasonably have gone the other
way:

  * The SDK's `project_key` is IGNORED. It is derived from the working
    directory, and ours is a per-session clone under a path that changes every
    run — keying on it would make yesterday's transcript unfindable today.
    Keyed on the Celmis session id instead, which is stable by construction and
    which we also mint as the CLI's own `--session-id`.

  * Tenancy is never taken from the key. The adapter is built for one session
    and closes over its id; nothing the SDK passes can widen that. A key that
    arrives naming a different session is a bug, and it is refused rather than
    written.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Entries are appended in batches as the conversation runs. Nothing here is
#: interpreted — the SDK writes JSON objects and expects the same ones back, in
#: order — so the column is JSONB and the ordering is the identity column.
MAX_ENTRY_BYTES = 512 * 1024


class PostgresTranscriptStore:
    """`session_store` adapter: mirrors transcript entries to Postgres.

    Duck-typed on purpose. The SDK probes for methods rather than using
    `isinstance`, so this deliberately does not subclass `SessionStore` — the
    protocol's default methods raise NotImplementedError to mark themselves
    absent, and inheriting them would advertise capabilities we do not have.
    Only `append` and `load` are implemented; deletion is retention's job (see
    `prune_transcripts`), not the SDK's.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def _mine(self, key: Any) -> bool:
        """Is this key for the session this adapter was built for?

        The SDK has no reason to hand us anybody else's, but an adapter that
        writes whatever it is given is one bug away from mixing two tenants'
        conversations in one table.
        """
        got = (key or {}).get("session_id")
        if got and got != self._session_id:
            logger.error(
                "transcript_key_mismatch adapter=%s key=%s — refusing to write",
                self._session_id, got,
            )
            return False
        return True

    async def append(self, key: Any, entries: list[dict]) -> None:
        """Mirror a batch. Called after the CLI's own local write succeeded."""
        if not entries or not self._mine(key):
            return
        from sqlalchemy import insert

        from src.db import async_session
        from src.db.models import AgentSessionTranscript

        subpath = (key or {}).get("subpath") or ""
        rows = []
        for entry in entries:
            blob = json.dumps(entry, ensure_ascii=False)
            if len(blob) > MAX_ENTRY_BYTES:
                # A single enormous entry is a tool result nobody will read;
                # dropping it keeps the conversation resumable, which losing
                # the whole batch would not.
                logger.warning("transcript_entry_too_large session=%s bytes=%d",
                               self._session_id, len(blob))
                continue
            rows.append({
                "session_id": self._session_id,
                "subpath": subpath,
                "entry": entry,
            })
        if not rows:
            return
        try:
            async with async_session() as s:
                await s.execute(insert(AgentSessionTranscript), rows)
                await s.commit()
        except Exception as exc:  # noqa: BLE001
            # Never take the conversation down over its own archive. A failed
            # mirror costs resumability, which is worth strictly less than the
            # turn the user is waiting on.
            logger.error("transcript_append_failed session=%s err=%s",
                         self._session_id, exc)

    async def load(self, key: Any) -> list[dict] | None:
        """Every entry, in the order it was written. None when there is none."""
        if not self._mine(key):
            return None
        from sqlalchemy import select

        from src.db import async_session
        from src.db.models import AgentSessionTranscript

        subpath = (key or {}).get("subpath") or ""
        try:
            async with async_session() as s:
                res = await s.execute(
                    select(AgentSessionTranscript.entry)
                    .where(AgentSessionTranscript.session_id == self._session_id)
                    .where(AgentSessionTranscript.subpath == subpath)
                    # The identity column IS the order. A timestamp would tie
                    # on a fast batch and reorder the conversation.
                    .order_by(AgentSessionTranscript.id)
                )
                entries = [row[0] for row in res.all()]
        except Exception as exc:  # noqa: BLE001
            logger.error("transcript_load_failed session=%s err=%s",
                         self._session_id, exc)
            return None
        if not entries:
            return None
        logger.info("transcript_loaded session=%s entries=%d",
                    self._session_id, len(entries))
        return entries


async def has_transcript(session_id: str) -> bool:
    """Is there anything to resume from? Cheap enough for a list endpoint."""
    from sqlalchemy import func, select

    from src.db import async_session
    from src.db.models import AgentSessionTranscript

    async with async_session() as s:
        res = await s.execute(
            select(func.count())
            .select_from(AgentSessionTranscript)
            .where(AgentSessionTranscript.session_id == session_id)
        )
        return bool(res.scalar_one_or_none())


__all__ = ["PostgresTranscriptStore", "has_transcript"]
