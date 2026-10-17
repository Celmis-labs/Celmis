"""A pack's usage is a count and a time, over 30 days, and never a person.

The contract (docs/CODEBASE.md, "Verifying that skills are installed and used";
`src/knowledge/usage.py`): the MCP call log's `packs` column holds `<pack id>` for
every pack a call touched and `<pack id>/<skill>` for a skill whose body it
returned. The usage object is `{calls_30d, last_used_at, skills: [{skill,
opens_30d, last_used_at}]}`:

  * a call counts once for a pack however many of its entries it holds; a skill
    counts once per call that opened it;
  * only the last 30 days, only this workspace, only this pack (an id that merely
    appears inside another string counts for nothing);
  * a skill of the current version is listed even when nobody opened it; one the
    log knows but the version no longer has is listed too;
  * ordered by opens, then name; no user, token, client or repository in it;
  * only a maintainer asks for it, and the community module imports nothing from
    `src.ee`.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.db.models import McpCallLog
from src.knowledge import store, usage
from src.knowledge.visibility import PackNotAccessible
from tests.knowledge.pack_rig import A, B, upload

SKILL = "---\nname: {name}\ndescription: Does {name}.\n---\n# {name}\n\nBody.\n"
NOW = datetime.now(UTC)
PID = "6b1f0c5e-0000-4000-8000-000000000aaa"
OTHER = "6b1f0c5e-0000-4000-8000-000000000bbb"


def at(**delta) -> datetime:
    return NOW - timedelta(**delta)


def rows(*items):
    return [(ts, packs) for ts, packs in items]


# ─── the arithmetic ──────────────────────────────────────────────────


def test_nothing_in_the_log_is_zero_calls_and_no_time():
    assert usage.summarise([], PID) == {"calls_30d": 0, "last_used_at": None, "skills": []}


def test_a_call_counts_once_however_many_entries_it_holds():
    got = usage.summarise(rows((at(hours=1), [PID, f"{PID}/a", f"{PID}/b"])), PID)
    assert got["calls_30d"] == 1
    assert {s["skill"]: s["opens_30d"] for s in got["skills"]} == {"a": 1, "b": 1}


def test_a_skill_counts_once_per_call_that_opened_it_and_is_ordered_by_opens_then_name():
    log = rows((at(hours=3), [PID, f"{PID}/zeta"]), (at(hours=2), [PID, f"{PID}/zeta", f"{PID}/beta"]),
               (at(hours=1), [PID, f"{PID}/alpha"]), (at(minutes=5), [PID]))
    got = usage.summarise(log, PID)
    assert got["calls_30d"] == 4
    assert [(s["skill"], s["opens_30d"]) for s in got["skills"]] == [("zeta", 2), ("alpha", 1), ("beta", 1)]
    assert got["last_used_at"] == at(minutes=5).isoformat()
    assert next(s for s in got["skills"] if s["skill"] == "zeta")["last_used_at"] == at(hours=2).isoformat()


def test_another_pack_and_look_alike_entries_count_for_nothing():
    log = rows((at(hours=1), [OTHER, f"{OTHER}/a"]), (at(hours=1), [f"x{PID}", f"{PID}x/a"]),
               (at(hours=1), [f"{PID}/"]), (at(hours=1), ["not a pack"]), (at(hours=1), None),
               (at(hours=1), "plain string"), (at(hours=1), {"packs": [PID]}), (at(hours=1), [7, None, [PID]]))
    assert usage.summarise(log, PID) == {"calls_30d": 0, "last_used_at": None, "skills": []}


def test_a_skill_of_the_current_version_is_listed_with_no_opens_and_an_old_one_is_listed_too():
    log = rows((at(hours=1), [PID, f"{PID}/gone"]))
    got = usage.summarise(log, PID, skills=["kept", "quiet"])
    assert [(s["skill"], s["opens_30d"], s["last_used_at"]) for s in got["skills"]] == [
        ("gone", 1, at(hours=1).isoformat()), ("kept", 0, None), ("quiet", 0, None)]


def test_a_naive_time_from_the_database_is_read_as_utc():
    naive = at(hours=1).replace(tzinfo=None)
    assert usage.summarise(rows((naive, [PID])), PID)["last_used_at"] == naive.replace(tzinfo=UTC).isoformat()


def test_the_object_has_exactly_these_keys_and_no_person():
    got = usage.summarise(rows((at(hours=1), [PID, f"{PID}/a"])), PID)
    assert set(got) == {"calls_30d", "last_used_at", "skills"}
    assert set(got["skills"][0]) == {"skill", "opens_30d", "last_used_at"}


def test_the_number_of_skills_listed_is_capped():
    names = [f"s{i:04d}" for i in range(usage.MAX_SKILLS + 40)]
    assert len(usage.summarise([], PID, skills=names)["skills"]) == usage.MAX_SKILLS


# ─── from the database ───────────────────────────────────────────────


async def make_pack(world, who="editor", audience="teams", skills=("billing", "refunds")):
    async with world.session() as s:
        created = await store.create_pack(s, world.principal(who), A, slug="p", title="P", audience=audience)
        pid = created.pack.id
    async with world.session() as s:
        view = await store.get_pack(s, world.principal(who), A, pid)
        await store.add_version(s, view, upload(*((f"{n}/SKILL.md", SKILL.format(name=n)) for n in skills)), "")
    return pid


async def log(world, pid, *entries, ws=A, ts=None, **extra):
    async with world.session() as s:
        s.add(McpCallLog(ts=ts or at(minutes=1), workspace_id=ws, user_id=extra.pop("user_id", "u-member"),
                         tool="skills", profile="dev", packs=list(entries) if entries else None, **extra))
        await s.commit()


async def usage_of(world, pid, who="editor"):
    async with world.session() as s:
        return await store.usage(s, await store.get_pack(s, world.principal(who), A, pid))


async def test_the_database_figure_counts_this_pack_this_workspace_and_thirty_days(world):
    pid = await make_pack(world)
    await log(world, pid, pid, f"{pid}/billing", ts=at(hours=2))
    await log(world, pid, pid, f"{pid}/billing", f"{pid}/refunds", ts=at(hours=1))
    await log(world, pid, pid, ts=at(days=29, hours=20))                       # inside the window
    await log(world, pid, pid, f"{pid}/billing", ts=at(days=31))               # outside it
    await log(world, pid, pid, f"{pid}/billing", ws=B)                         # another workspace
    await log(world, pid, OTHER, f"{OTHER}/billing")                           # another pack
    await log(world, pid)                                                      # a call that read no pack (NULL)
    got = await usage_of(world, pid)
    assert got["calls_30d"] == 3
    assert [(s["skill"], s["opens_30d"]) for s in got["skills"]] == [("billing", 2), ("refunds", 1)]
    assert got["last_used_at"] == (at(hours=1)).isoformat()


async def test_a_pack_nobody_used_lists_its_skills_with_zero(world):
    pid = await make_pack(world)
    got = await usage_of(world, pid)
    assert got == {"calls_30d": 0, "last_used_at": None, "skills": [
        {"skill": "billing", "opens_30d": 0, "last_used_at": None},
        {"skill": "refunds", "opens_30d": 0, "last_used_at": None}]}


async def test_the_answer_holds_no_user_token_client_or_repository(world):
    pid = await make_pack(world)
    await log(world, pid, pid, f"{pid}/billing", user_id="u-member", token_id="tok-secret-1",
              client_id="client-secret-2", repos=["acme/secret-repo"], args_hash="h" * 16)
    text = json.dumps(await usage_of(world, pid))
    for leak in ("u-member", "tok-secret-1", "client-secret-2", "acme/secret-repo", "member@acme-corp.io", "h" * 16):
        assert leak not in text


async def test_the_scan_is_bounded(world, monkeypatch):
    pid = await make_pack(world)
    monkeypatch.setattr(usage, "MAX_ROWS_SCANNED", 3)
    for i in range(5):
        await log(world, pid, pid, ts=at(hours=10 - i))
    got = await usage_of(world, pid)
    assert got["calls_30d"] == 3                                    # the newest three
    assert got["last_used_at"] == at(hours=6).isoformat()


@pytest.mark.parametrize("who", ["member", "viewer", "owner", "admin"])
async def test_only_a_maintainer_may_ask(world, who):
    pid = await make_pack(world, audience="restricted", skills=("billing",))
    async with world.session() as s:
        named = await store.get_pack(s, world.principal("editor"), A, pid)
        assert named.pack.id == pid
    with pytest.raises(PackNotAccessible):
        await usage_of(world, pid, who)                              # a private pack: hidden, so not "forbidden"


async def test_a_reader_who_may_see_the_pack_is_refused_not_hidden(world):
    from src.knowledge.store import NotMaintainer

    pid = await make_pack(world, audience="workspace")
    with pytest.raises(NotMaintainer):
        await usage_of(world, pid, "member")
    assert (await usage_of(world, pid, "editor"))["calls_30d"] == 0


# ─── the boundary ────────────────────────────────────────────────────


def test_the_community_module_imports_nothing_from_the_enterprise_tree():
    root = Path(usage.__file__).resolve().parents[2]
    offenders = []
    for path in [*(root / "src" / "knowledge").glob("*.py"), *(root / "src" / "api" / "routers").glob("codebase*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            if any(n == "src.ee" or n.startswith("src.ee.") for n in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_codebase_screens_import_nothing_from_the_enterprise_folder():
    web = Path(usage.__file__).resolve().parents[2] / "web"
    files = [*(web / "components" / "codebase").rglob("*.ts*"), *(web / "app" / "(app)" / "codebase").rglob("*.ts*"),
             web / "lib" / "codebase.ts"]
    assert len(files) > 5
    assert [p.name for p in files if "/ee" in p.read_text(encoding="utf-8")
            and ("@/ee" in p.read_text(encoding="utf-8") or "web/ee" in p.read_text(encoding="utf-8"))] == []
