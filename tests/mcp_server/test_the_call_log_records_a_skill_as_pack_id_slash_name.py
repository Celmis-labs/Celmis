"""What `mcp_call_log.packs` may hold, and how much of it.

The contract (src/mcp_server/callctx.py, audit.py; docs/CODEBASE.md "Usage"):

  * an entry is `<pack uuid>` or `<pack uuid>/<skill name>`, nothing else: the
    skill part must be a name ingest accepts (lowercase letters, digits, `.`,
    `_`, `-`, at most 64, starting with a letter or digit), so a path, a title or
    a query that reaches the log by mistake is dropped, not stored;
  * an id is written in its canonical lowercase dashed form, whatever it came as;
  * `note_skill(pack, skill)` records the pack AND the skill entry, so every
    skill entry has its pack beside it;
  * the row keeps at most 50 plain ids and at most 10 skill entries, sorted, so a
    call that touched many packs does not push the skills it opened out of the
    row, and a row never grows without bound.
"""

from __future__ import annotations

import uuid

import pytest

from src.mcp_server import audit, callctx

P = str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _no_call_left_open():
    yield
    callctx.reset()


@pytest.mark.parametrize("value", [
    P, P.upper(), P.replace("-", ""), f"{P}/billing", f"{P.upper()}/billing", f"{P}/a",
    f"{P}/a.b_c-d", f"{P}/0x", f"{P}/{'a' * 64}",
])
def test_an_id_and_an_id_with_a_valid_skill_name_are_entries(value):
    entry = callctx.pack_entry(value)
    assert entry is not None and entry.startswith(P)
    assert uuid.UUID(entry.split("/")[0]) == uuid.UUID(P)


@pytest.mark.parametrize("value", [
    "", "billing", "notes/a.md", f"{P}/", f"{P}/Billing", f"{P}/-a", f"{P}/.a", f"{P}/a b",
    f"{P}/a/b", f"{P}/../x", f"{P}/{'a' * 65}", f"{P}/a\n", f"{P}/é", f"x{P}", f"{P}x",
    f"{P} /a", None, 7, b"x", ("a",),
])
def test_anything_else_is_not_an_entry(value):
    assert callctx.pack_entry(value) is None


def test_entries_are_canonical_and_a_repeat_collapses():
    rec = callctx.begin("skills")
    callctx.note_packs(P.upper(), P.replace("-", ""), f"{P.upper()}/billing", f"{P}/billing")
    assert rec.packs == {P, f"{P}/billing"}


def test_noting_a_skill_records_the_pack_beside_it():
    rec = callctx.begin("skills")
    callctx.note_skill(P, "billing")
    assert rec.packs == {P, f"{P}/billing"}


def test_noting_a_skill_with_a_bad_name_records_the_pack_alone_and_a_bad_pack_nothing():
    rec = callctx.begin("skills")
    callctx.note_skill("a title", "billing")
    callctx.note_skill(P, None)
    callctx.note_skill(None, "billing")
    assert rec.packs == set()
    callctx.note_skill(P, "Not A Name")
    assert rec.packs == {P}                                  # the pack was read; no skill entry


def test_noting_with_no_call_open_is_silent():
    callctx.reset()
    callctx.note_packs(P)
    callctx.note_skill(P, "billing")
    assert callctx.current() is None


def test_a_row_keeps_fifty_ids_and_ten_skills_and_sorts_them():
    ids = sorted(str(uuid.uuid4()) for _ in range(80))
    skills = sorted(f"{ids[0]}/s{i:02d}" for i in range(15))
    kept = audit._pack_entries(set(ids) | set(skills))
    assert kept == sorted(ids[:50] + skills[:10])
    assert audit._pack_entries(set()) == [] and audit._pack_entries([]) == []


def test_the_row_a_call_leaves_carries_the_skill_entries(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)
    rec = callctx.begin("skills")
    callctx.note_skill(P, "billing")
    callctx.note_packs("a title that must not be logged", "notes/a.md")
    audit.record_call(rec)
    assert queued[0]["packs"] == sorted([P, f"{P}/billing"])
    assert "must not be logged" not in repr(queued) and "notes/a.md" not in repr(queued)
