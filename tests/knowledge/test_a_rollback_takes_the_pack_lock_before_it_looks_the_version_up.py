"""A rollback and an upload of ONE pack take turns.

The contract (src/knowledge/store.py `_lock_pack`, `add_version`, `activate_version`):

  * `add_version` and `activate_version` both take the pack row's lock
    (`SELECT ... FOR UPDATE` on PostgreSQL) FIRST, and look the version up only
    after it: a rollback cannot choose a version that an upload running at the same
    moment is pruning, and cannot leave `current_version_id` on a row that is gone;
  * the lock is a statement on the pack row alone;
  * a rollback to a version that was pruned meanwhile is the ordinary refusal.

SQLite takes no row locks, so what is tested here is that the lock statement is
issued, is a `FOR UPDATE` once compiled for PostgreSQL, and comes before the lookup.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from src.knowledge import store
from tests.knowledge.pack_rig import A, upload


def _watch(world) -> list[tuple[str, str]]:
    """``(kind, text)`` of each SELECT run on the async engine: kind is "lock" for
    a statement with FOR UPDATE, else "select"."""
    seen: list[tuple[str, str]] = []
    engine = world.factory.kw["bind"].sync_engine

    def before(_conn, clause, _multi, _params, _options):
        if isinstance(clause, Select):
            locked = getattr(clause, "_for_update_arg", None) is not None
            seen.append(("lock" if locked else "select",
                         str(clause.compile(dialect=postgresql.dialect()))))

    event.listen(engine, "before_execute", before)
    return seen


async def _pack_with_two_versions(world):
    async with world.session() as s:
        view = await store.create_pack(s, world.principal("editor"), A, slug="p", title="P",
                                       audience="workspace")
        await store.add_version(s, view, upload(("notes/a.md", "# one\n")), "")
        await store.add_version(s, view, upload(("notes/a.md", "# two\n")), "")
        return view.pack.id


async def test_a_rollback_locks_the_pack_before_it_looks_the_version_up(world):
    pid = await _pack_with_two_versions(world)
    seen = _watch(world)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        seen.clear()
        await store.activate_version(s, view, 1)
    kinds = [k for k, _t in seen]
    assert "lock" in kinds
    first_lock = kinds.index("lock")
    lookups = [i for i, (_k, t) in enumerate(seen) if "FROM knowledge_pack_versions" in t]
    assert lookups and first_lock < min(lookups)
    lock_text = seen[first_lock][1]
    assert "FOR UPDATE" in lock_text and "FROM knowledge_packs" in lock_text
    assert "knowledge_pack_versions" not in lock_text


async def test_an_upload_takes_the_same_lock(world):
    pid = await _pack_with_two_versions(world)
    seen = _watch(world)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        seen.clear()
        await store.add_version(s, view, upload(("notes/a.md", "# three\n")), "")
    assert [t for k, t in seen if k == "lock"] and "FOR UPDATE" in [t for k, t in seen if k == "lock"][0]


async def test_a_rollback_still_lands_on_the_version_asked_for(world):
    pid = await _pack_with_two_versions(world)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.activate_version(s, view, 1)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        current = await store.current_version(s, view)
        assert current.number == 1
        with pytest.raises(store.PackError):
            await store.activate_version(s, view, 99)
