"""`skills` and `brief` show a caller the packs they may read, and nothing else.

The contract (docs/CODEBASE.md "Pack visibility", src/knowledge/visibility.py,
src/mcp_server/dev_profile/pack_access.py):

  * what a person may read is decided on EVERY call from live rows: the role in
    the workspace the token names and the teams of that workspace. Audiences:
    `workspace` (every current member), `teams` (workspace owners and admins,
    the granted teams, the people named), `restricted` (the granted teams and the
    people named only: not owners, not admins, not a global admin). Only the
    superadmin sees everything. A private pack (restricted, no team) is its
    creator's alone;
  * a person who left the workspace sees nothing, whatever `team_members` rows
    they left behind, and a token for another workspace reads that workspace;
  * a project token, an internal token, a refused token, a token whose workspace
    nobody named and a call with no token at all (single_tenant or
    multi_tenant) see no pack: every answer is the one for "nothing visible",
    which is also the one for "nothing matches";
  * a skill that is hidden, missing or another workspace's is answered with the
    same bytes (`skill not found or not accessible`), which repeat nothing the
    caller typed, and the call is audited as denied.

The table of who sees what is written out by hand in `pack_world.VISIBLE`.
"""

from __future__ import annotations

import pytest

from src.knowledge.brief import NO_MATCH
from src.knowledge.visibility import principal_for_sync
from src.mcp_server import identity as _identity
from src.mcp_server.dev_profile import pack_access
from src.mcp_server.dev_profile import tools_skills as ts
from src.mcp_server.dev_profile.emit import error
from src.users.roles import PACK_AUDIENCES
from tests.knowledge.pack_rig import A, B
from tests.mcp_server.dev.pack_world import (
    ABROAD,
    HIT_LINE,
    LIST_LINE,
    PACKS,
    TAG,
    VISIBLE,
    traces,
)

NOT_FOUND = error(None, pack_access.NOT_FOUND)
_REAL_RESOLVER = _identity.resolve_caller
EVERYTHING = frozenset(PACKS)


def _hidden(who: str) -> frozenset[str]:
    return EVERYTHING - VISIBLE[who]


def test_the_table_of_who_sees_what_covers_every_audience():
    assert {a for _s, _m, a, _c in PACKS.values()} == set(PACK_AUDIENCES)
    assert set(VISIBLE) >= {"su", "ga", "owner", "admin", "editor", "editor2", "member", "viewer",
                            "tc", "xm", "outsider", "admin_b"}


async def test_the_skills_list_names_exactly_the_packs_each_person_may_read(pack_world):
    for who, seen in VISIBLE.items():
        pack_world.as_caller(who)
        text, rec = pack_world.call(ts.run)
        listed = {(p, s) for p, s in LIST_LINE.findall(text)}
        assert listed == {(p, PACKS[p][0]) for p in seen}, who
        assert traces(text, _hidden(who) | {"abroad"}) == [], who
        assert (ts.EMPTY in text) == (not seen), who
        assert rec.packs <= {pack_world.ids[p] for p in seen}, who


async def test_a_search_finds_only_what_the_person_may_read(pack_world):
    for who, seen in VISIBLE.items():
        pack_world.as_caller(who)
        text, rec = pack_world.call(ts.run, "invoicing")
        assert {p for p in HIT_LINE.findall(text)} == set(seen), who
        assert traces(text, _hidden(who) | {"abroad"}) == [], who
        assert (ts.NO_HITS in text) == (not seen), who
        assert rec.packs <= {pack_world.ids[p] for p in seen}, who


async def test_a_skill_opens_for_those_who_may_read_it_and_is_denied_to_everyone_else(pack_world):
    for who, seen in VISIBLE.items():
        pack_world.as_caller(who)
        for slug, (skill, marker, _aud, _by) in PACKS.items():
            for name in (f"{slug}/{skill}", skill):
                text, rec = pack_world.call(ts.run, "", name)
                if slug in seen:
                    assert f"skill {slug}/{skill} [{slug}@v1]" in text, (who, name)
                    assert f"Invoicing {marker} steps" in text, (who, name)
                    assert rec.status == "ok", (who, name)
                    assert rec.packs == {pack_world.ids[slug], f"{pack_world.ids[slug]}/{skill}"}
                else:
                    assert text == NOT_FOUND, (who, name)
                    assert rec.status == "denied", (who, name)
                    assert rec.packs == set(), (who, name)


async def test_a_hidden_skill_and_a_missing_one_get_the_same_bytes(pack_world):
    pack_world.as_caller("viewer")                           # reads `open` and `named`
    asks = [
        ("private/diary", ""),                               # hidden skill of a hidden pack
        ("private/nothing", ""),                             # hidden pack, no such skill
        ("diary", ""),                                       # the same, by bare name
        ("vault/ledger", ""),
        ("staff", "oncall/SKILL.md"),                        # a hidden pack, a file of it
        ("private", ""),                                     # a hidden pack, opened as a pack
        ("private", "notes/private-note.md"),
        ("named/nothing", ""),                               # visible pack, no such skill
        ("open/nothing", ""),
        ("nowhere/rollout", ""),                             # no such pack
        ("zzzz", ""),                                        # no such anything
        (f"{ABROAD[0]}", ""),                                # another workspace's skill
        (f"abroad/{ABROAD[0]}", ""),
        ("abroad", ""),
        ("private/diary", "SKILL.md"),
        ("../private/diary", ""),
        ("x" * 500, ""),
    ]
    outputs = []
    for name, path in asks:
        text, rec = pack_world.call(ts.run, "", name, path)
        assert rec.status == "denied", (name, path)
        assert rec.packs == set(), (name, path)
        outputs.append(text)
    assert set(outputs) == {NOT_FOUND}
    assert NOT_FOUND.splitlines()[-1] == "skill not found or not accessible"
    assert "private" not in NOT_FOUND and "diary" not in NOT_FOUND   # nothing typed comes back


async def test_a_visible_skill_with_no_such_file_is_not_a_denial(pack_world):
    pack_world.as_caller("viewer")
    text, rec = pack_world.call(ts.run, "", "open/rollout", "nothing.md")
    assert text != NOT_FOUND and "no such file" in text
    assert rec.status == "ok"


async def test_a_private_pack_is_invisible_to_teammates_admins_and_global_admins(pack_world):
    """Restricted, no team: its creator (editor) and the superadmin, nobody else."""
    ids = pack_world.ids
    for who in ("owner", "admin", "ga", "editor2", "member", "viewer", "tc"):
        pack_world.as_caller(who)
        for text in (pack_world.call(ts.run)[0], pack_world.call(ts.run, "saffron")[0],
                     pack_world.call(ts.run_brief, "invoicing saffron diary")[0]):
            assert "saffron" not in text and "private/" not in text and "private@" not in text, who
        assert pack_world.call(ts.run, "", "private/diary")[0] == NOT_FOUND, who
    for who in ("editor", "su"):
        pack_world.as_caller(who)
        text, rec = pack_world.call(ts.run, "", "private/diary")
        assert "Invoicing saffron steps" in text, who
        assert ids["private"] in rec.packs


async def test_a_restricted_pack_is_hidden_from_the_owner_who_made_it_and_from_admins(pack_world):
    """`vault` is for team ta alone: its creator was not named, so the owner is out."""
    for who in ("owner", "admin", "ga"):
        pack_world.as_caller(who)
        assert pack_world.call(ts.run, "", "vault/ledger")[0] == NOT_FOUND, who
        assert "tamarind" not in pack_world.call(ts.run_brief, "invoicing tamarind")[0], who
    for who in ("editor2", "member", "su"):
        pack_world.as_caller(who)
        assert "Invoicing tamarind steps" in pack_world.call(ts.run, "", "vault/ledger")[0], who


async def test_an_explicit_member_reads_the_pack_that_names_them_and_no_other(pack_world):
    pack_world.as_caller("viewer")
    seen = {p for p, _s in LIST_LINE.findall(pack_world.call(ts.run)[0])}
    assert seen == {"open", "named"}


async def test_the_brief_quotes_only_packs_the_person_may_read(pack_world):
    for who, seen in VISIBLE.items():
        pack_world.as_caller(who)
        text, rec = pack_world.call(ts.run_brief, "invoicing routine", tool="brief")
        tags = set(TAG.findall(text))
        assert tags <= set(seen), (who, tags)
        assert bool(tags) == bool(seen), who
        assert traces(text, _hidden(who) | {"abroad"}) == [], who
        assert rec.packs <= {pack_world.ids[p] for p in seen}, who
        if seen:
            assert rec.packs, who


async def test_a_brief_with_nothing_visible_reads_like_a_brief_nothing_matches(pack_world):
    pack_world.as_caller("su")
    nothing_matches, _ = pack_world.call(ts.run_brief, "zzqqxx wwvvyy", tool="brief")
    assert nothing_matches.endswith(NO_MATCH)
    for who in ("xm", "outsider", "admin_b"):
        pack_world.as_caller(who)
        text, rec = pack_world.call(ts.run_brief, "invoicing routine", tool="brief")
        assert text == nothing_matches, who
        assert rec.packs == set()


async def test_a_person_who_left_the_workspace_loses_the_packs_whatever_the_token_says(pack_world):
    from sqlalchemy import delete

    from src.db.models import WorkspaceMember

    pack_world.as_caller("editor2")
    assert LIST_LINE.findall(pack_world.call(ts.run)[0])
    async with pack_world.rig.session() as s:
        await s.execute(delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == A, WorkspaceMember.user_id == pack_world.uid("editor2")))
        await s.commit()
    # the team_members row is still there; the same token, the next call
    text, rec = pack_world.call(ts.run)
    assert LIST_LINE.findall(text) == [] and ts.EMPTY in text
    assert pack_world.call(ts.run, "", "vault/ledger")[0] == NOT_FOUND
    assert rec.packs == set()


@pytest.mark.parametrize("kind", ["project", "internal"])
async def test_a_project_or_internal_token_reads_no_pack_even_with_a_superadmins_name_on_it(
        pack_world, kind):
    for who in ("su", "owner", "editor2"):
        pack_world.as_caller(who, kind=kind)
        assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)
        assert pack_world.call(ts.run, "invoicing")[0].endswith(ts.NO_HITS)
        text, rec = pack_world.call(ts.run, "", "open/rollout")
        assert text == NOT_FOUND and rec.status == "denied" and rec.packs == set()
        assert TAG.findall(pack_world.call(ts.run_brief, "invoicing routine", tool="brief")[0]) == []


async def test_a_refused_token_reads_no_pack(pack_world):
    for who in ("su", "owner"):
        pack_world.as_caller(who, refused="This MCP token was revoked.")
        assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)
        assert pack_world.call(ts.run, "", "open/rollout")[0] == NOT_FOUND
        assert TAG.findall(pack_world.call(ts.run_brief, "invoicing", tool="brief")[0]) == []


async def test_a_token_whose_workspace_nobody_named_reads_no_pack(pack_world):
    pack_world.as_caller("owner", workspace_resolved=False)
    assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)
    pack_world.as_caller("owner", workspace_id="")
    assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)
    pack_world.as_caller("owner", user_id="")
    assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)


async def _the_local_trusted_principals_pack(pack_world) -> None:
    """Single tenant answers an unidentified caller as user `default` of workspace
    `default`. Give that person a workspace and a pack, so only the rule keeps it shut."""
    import src.users.store as users_mod
    from src.db.models import Workspace, WorkspaceMember
    from src.knowledge import store
    from src.users import User
    from tests.knowledge.pack_rig import upload
    from tests.mcp_server.dev.pack_world import contents

    users_mod.get_user_store().create(
        User(id="default", email="local@acme-corp.io", name="local", is_admin=True))
    async with pack_world.rig.session() as s:
        s.add(Workspace(id="default", name="Local", slug="local", description=""))
        s.add(WorkspaceMember(workspace_id="default", user_id="default", role="owner"))
        await s.commit()
        who = principal_for_sync("default", "default")
        made = await store.create_pack(s, who, "default", slug="local", title="Local",
                                       audience="workspace")
        view = await store.get_pack(s, who, "default", made.pack.id)
        await store.add_version(s, view, upload(*contents("local", "tidy", "cumin")))


@pytest.mark.parametrize("mode", ["single_tenant", "multi_tenant"])
async def test_a_call_with_no_token_reads_no_pack_in_either_deployment_mode(
        pack_world, monkeypatch, mode):
    """Single tenant lets an unidentified local caller research code; a pack is
    never read for one, because it belongs to a workspace and a person. The pack
    below is one that very principal could read if it came with a token."""
    from src.config import get_settings
    from src.deployment import reset_mode_cache
    from src.mcp_server import identity

    await _the_local_trusted_principals_pack(pack_world)
    pack_world.as_caller("default", ws="default", user_id="default")
    control = pack_world.call(ts.run)[0]
    assert "local/tidy" in control                           # with a token the pack is theirs
    monkeypatch.setattr(identity, "resolve_caller", _REAL_RESOLVER)
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", mode)
    get_settings.cache_clear()
    reset_mode_cache()
    caller = identity.resolve_caller()
    assert caller.authenticated is False
    assert (caller.workspace_id, caller.user_id) == (("default", "default")
                                                      if mode == "single_tenant" else ("", "anonymous"))
    assert pack_world.call(ts.run)[0].endswith(ts.EMPTY)
    assert pack_world.call(ts.run, "invoicing")[0].endswith(ts.NO_HITS)
    assert pack_world.call(ts.run, "tidy")[0].endswith(ts.NO_HITS)
    text, rec = pack_world.call(ts.run, "", "local/tidy")
    assert text == NOT_FOUND and rec.status == "denied"
    assert TAG.findall(pack_world.call(ts.run_brief, "invoicing routine", tool="brief")[0]) == []
    assert rec.packs == set()


async def test_a_token_for_another_workspace_reads_that_workspaces_packs_only(pack_world):
    pack_world.as_caller("admin_b", ws=B)
    listed = LIST_LINE.findall(pack_world.call(ts.run)[0])
    assert listed == [("abroad", ABROAD[0])]
    assert pack_world.call(ts.run, "", "open/rollout")[0] == NOT_FOUND
    pack_world.as_caller("su", ws=B)                         # even the superadmin reads ONE workspace
    assert {p for p, _s in LIST_LINE.findall(pack_world.call(ts.run)[0])} == {"abroad"}
    pack_world.as_caller("su", ws=A)
    assert pack_world.call(ts.run, "", f"abroad/{ABROAD[0]}")[0] == NOT_FOUND
    assert pack_world.call(ts.run, "", ABROAD[0])[0] == NOT_FOUND


async def test_a_skill_name_that_two_visible_packs_share_is_not_guessed(pack_world):
    """A bare name is opened only when exactly one visible pack has it; with two
    the answer is the one for "not found", never an arbitrary pick."""
    from src.knowledge import store
    from tests.knowledge.pack_rig import upload
    from tests.mcp_server.dev.pack_world import contents

    async with pack_world.rig.session() as s:
        made = await store.create_pack(s, pack_world.rig.principal("owner"), A, slug="twin",
                                       title="Twin", audience="workspace")
        view = await store.get_pack(s, pack_world.rig.principal("owner"), A, made.pack.id)
        await store.add_version(s, view, upload(*contents("twin", "rollout", "cardamom")))
    pack_world.as_caller("owner")
    assert pack_world.call(ts.run, "", "rollout")[0] == NOT_FOUND
    assert "Invoicing marigold steps" in pack_world.call(ts.run, "", "open/rollout")[0]
    assert "Invoicing cardamom steps" in pack_world.call(ts.run, "", "twin/rollout")[0]
