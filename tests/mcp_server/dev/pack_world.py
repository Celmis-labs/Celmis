"""A workspace full of knowledge packs and a cast of callers — a helper, not a test module.

Built on the pack rig (`tests/knowledge/pack_rig.py`: workspaces `wsid-a` and
`wsid-b`, a user store, real membership and team rows), plus two people the
`skills` / `brief` tests need:

  tc   a member of workspace A in a team of its own (no pack is granted to it)
  xm   NOT a member of workspace A any more, but a `team_members` row for team
       ta is still there (the leftover of a removal)

Five packs live in workspace A, each with one skill, one checklist and one note
that share the word "invoicing" (so any task about it ranks all of them) and a
marker word of their own (so a test can tell whose text it is looking at):

  pack     audience     granted / named                    who sees it
  open     workspace    -                                  su ga owner admin editor editor2 member viewer tc
  staff    teams        team ta                            su ga owner admin editor2 member
  vault    restricted   team ta (its creator is not named) su editor2 member
  private  restricted   nobody but its creator, editor     su editor
  named    restricted   its creator, owner, and viewer     su owner viewer

and one pack, `abroad`, in workspace B. `VISIBLE` below is that table, written
out by hand: it is the oracle, not something computed from the rule under test.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

from src.mcp_server import callctx, identity
from tests.knowledge.pack_rig import TEAM_A, A, B, rig, upload

TEAM_C = "33333333-3333-4333-8333-333333333333"

#: slug -> (skill name, marker word, audience, creator)
PACKS: dict[str, tuple[str, str, str, str]] = {
    "open": ("rollout", "marigold", "workspace", "owner"),
    "staff": ("oncall", "juniper", "teams", "owner"),
    "vault": ("ledger", "tamarind", "restricted", "owner"),
    "private": ("diary", "saffron", "restricted", "editor"),
    "named": ("roster", "bergamot", "restricted", "owner"),
}
ABROAD = ("bridge", "quince")

#: caller -> the packs of workspace A they may read, by hand.
VISIBLE: dict[str, frozenset[str]] = {
    "su": frozenset(PACKS),
    "ga": frozenset({"open", "staff"}),
    "owner": frozenset({"open", "staff", "named"}),
    "admin": frozenset({"open", "staff"}),
    "editor": frozenset({"open", "private"}),
    "editor2": frozenset({"open", "staff", "vault"}),
    "member": frozenset({"open", "staff", "vault"}),
    "viewer": frozenset({"open", "named"}),
    "tc": frozenset({"open"}),
    "xm": frozenset(),
    "outsider": frozenset(),
    "admin_b": frozenset(),
}

SKILL_MD = ("---\nname: {skill}\ndescription: Invoicing {marker} routine for the {slug} pack.\n"
            "repos: [{repos}]\n---\n"
            "# {skill}\n\nInvoicing {marker} steps. Run the {marker} check first.\n")
CHECK_MD = ("---\ndescription: Invoicing {marker} checklist\n---\n# {slug} check\n\n"
            "Invoicing {marker}: look twice.\n")
NOTE_MD = ("---\ndescription: Invoicing {marker} note\nrepos: [{repos}]\n---\n# {slug} note\n\n"
           "Invoicing {marker} lives next to the billing code.\n")


def contents(slug: str, skill: str, marker: str, repos: str = "") -> list[tuple[str, str]]:
    fill = {"slug": slug, "skill": skill, "marker": marker, "repos": repos}
    return [(f"{skill}/SKILL.md", SKILL_MD.format(**fill)),
            (f"checklists/{slug}-check.md", CHECK_MD.format(**fill)),
            (f"notes/{slug}-note.md", NOTE_MD.format(**fill))]


@dataclass
class PackWorld:
    rig: Any
    mp: pytest.MonkeyPatch
    ids: dict[str, str] = field(default_factory=dict)         # slug -> pack id
    abroad_id: str = ""

    def uid(self, who: str) -> str:
        return self.rig.uid(who) if who in self.rig.users else f"u-{who}"

    def caller(self, who: str, *, ws: str = A, kind: str = "pat", **over: Any):
        base = dict(user_id=self.uid(who), is_admin=False, workspace_id=ws, scopes=("read:code",),
                    authenticated=True, workspace_resolved=True, refused="", token_id=None,
                    kind=kind, repo_patterns=None, allow_write=False)
        base.update(over)
        return identity.McpCaller(**base)

    def as_caller(self, who: str, **kw: Any):
        """Run the following calls as ``who`` (a name of the cast) holding a token."""
        caller = self.caller(who, **kw)
        self.mp.setattr(identity, "resolve_caller", lambda: caller)
        return caller

    def readable(self, readable: tuple[str, ...] = (), metadata: tuple[str, ...] = (),
                 deny: dict[str, tuple[str, ...]] | None = None) -> None:
        """Who may read which repository as code, for the repository pointers of a pack."""
        from src.access.resolver import RepoAccessDecision, _RuleView
        from src.mcp_server.dev_profile import access

        deny = deny or {}
        decisions: dict[str, Any] = {}
        for slug in readable:
            globs = deny.get(slug, ())
            decisions[slug] = (RepoAccessDecision.full(slug) if not globs else RepoAccessDecision(
                repo_slug=slug, visibility="code", open_default=False,
                rules=(_RuleView("code", (), globs, ()),), deny_globs=globs))
        for slug in metadata:
            decisions[slug] = RepoAccessDecision(
                repo_slug=slug, visibility="metadata", open_default=False,
                rules=(_RuleView("metadata", (), (), ()),), deny_globs=())
        self.mp.setattr(access, "mcp_scope", lambda candidates=None: access.DevScope(
            decisions=dict(decisions), user_id="u", workspace_id=A))

    def call(self, fn: Callable[..., str], *args: Any, tool: str = "skills"):
        """``(text, record)`` of one call, with a call record open like the envelope's."""
        rec = callctx.begin(tool)
        try:
            return fn(*args), rec
        finally:
            callctx.reset()


async def _add_cast(r: Any) -> None:
    import src.users.store as users_mod
    from src.db.models import Team, TeamMember, WorkspaceMember
    from src.users import User

    store = users_mod.get_user_store()
    for name in ("tc", "xm"):
        store.create(User(id=f"u-{name}", email=f"{name}@acme-corp.io", name=name, is_admin=False))
    async with r.session() as s:
        s.add(Team(id=TEAM_C, name="tc", description="", workspace_id=A))
        s.add(WorkspaceMember(workspace_id=A, user_id="u-tc", role="member"))
        s.add(TeamMember(team_id=TEAM_C, user_id="u-tc", role="member"))
        s.add(TeamMember(team_id=TEAM_A, user_id="u-xm", role="member"))     # no workspace row
        await s.commit()


async def _make_pack(r: Any, who: str, ws: str, slug: str, audience: str,
                     teams: tuple[tuple[str, str], ...], files: list[tuple[str, str]]) -> str:
    from src.knowledge import store

    async with r.session() as s:
        view = await store.create_pack(s, r.principal(who, ws), ws, slug=slug, title=slug.title(),
                                       audience=audience, teams=teams)
        view = await store.get_pack(s, r.principal(who, ws), ws, view.pack.id)
        await store.add_version(s, view, upload(*files))
        return view.pack.id


#: Pointers every skill and note carries: one readable repository (a file and a
#: line, and a path a deny rule hides), one the callers only know by name, one nobody has.
POINTERS = ("shop-api:app/db.py:12, shop-api:app/private/keys.py, "
            "payments-core:src/x.py, ghost-repo:README.md")


async def build(r: Any, mp: pytest.MonkeyPatch, repos: str = POINTERS) -> PackWorld:
    from sqlalchemy import delete

    from src.db.models import KnowledgePackMember
    from src.knowledge import store

    await _add_cast(r)
    world = PackWorld(r, mp)
    for slug, (skill, marker, audience, creator) in PACKS.items():
        teams = ((TEAM_A, "read"),) if slug in ("staff", "vault") else ()
        world.ids[slug] = await _make_pack(r, creator, A, slug, audience, teams,
                                           contents(slug, skill, marker, repos))
    async with r.session() as s:
        view = await store.get_pack(s, r.principal("owner"), A, world.ids["named"])
        await store.add_member(s, view, "viewer@acme-corp.io", "read")
        # The creator of `vault` is named by creating it; the pack is for team ta alone.
        await s.execute(delete(KnowledgePackMember).where(
            KnowledgePackMember.pack_id == world.ids["vault"],
            KnowledgePackMember.user_id == r.uid("owner")))
        await s.commit()
    world.abroad_id = await _make_pack(r, "admin_b", B, "abroad", "workspace", (),
                                       contents("abroad", ABROAD[0], ABROAD[1], repos))
    return world


@pytest_asyncio.fixture
async def pack_world(tmp_path, monkeypatch):
    from src.config import get_settings
    from src.deployment import reset_mode_cache

    async with rig(tmp_path, monkeypatch) as r:
        monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
        get_settings.cache_clear()
        reset_mode_cache()
        world = await build(r, monkeypatch)
        world.readable()                       # no repository is readable until a test says so
        yield world
    get_settings.cache_clear()
    reset_mode_cache()


# ─── reading what a tool printed ─────────────────────────────────────


#: `pack/skill — description` (the skills list)
LIST_LINE = re.compile(r"^([a-z]+)/([a-z]+) — ", re.M)
#: `pack/path [kind]` (a search hit)
HIT_LINE = re.compile(r"^([a-z]+)/\S+ \[(?:skill|checklist|api|note|doc|file)\]", re.M)
#: `[pack@vN]` (a brief entry)
TAG = re.compile(r"\[([a-z]+)@v\d+\]")


def traces(text: str, slugs: set[str] | frozenset[str]) -> list[str]:
    """Whatever in ``text`` shows that pack ``slugs`` exist: their marker word, their
    skill name or their ``slug/`` / ``slug@`` form."""
    found = []
    for slug in sorted(slugs):
        skill, marker = PACKS[slug][:2] if slug in PACKS else (ABROAD[0], ABROAD[1])
        for needle in (marker, skill, f"{slug}/", f"{slug}@"):
            if needle in text:
                found.append(f"{slug}:{needle}")
    return found


def indexed(monkeypatch: pytest.MonkeyPatch, *slugs: str, sha: str = "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e",
            age_h: float = 2.5) -> None:
    """Say these repositories are indexed at ``sha`` (what the `idx:` line reads)."""
    from datetime import UTC, datetime, timedelta

    from src.repos import index_state

    def fake(wanted):
        now = datetime.now(UTC)
        return {s: index_state.RepoIndexInfo(
            repo_slug=s, last_indexed_sha=sha, last_indexed_at=now - timedelta(hours=age_h),
            last_full_rebuild_at=None, last_indexed_files=0, last_error=None, last_error_at=None,
            last_checked_at=now - timedelta(minutes=5), last_remote_sha=sha, last_check_error=None,
            indexed_branch="develop") for s in wanted if s in slugs}

    monkeypatch.setattr(index_state, "read_index_states", fake)
