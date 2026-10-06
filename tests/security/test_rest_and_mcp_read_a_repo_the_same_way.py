"""One question, two doors: may this person read the code of this repository?

The REST gate (`enforce_repo_permission`) and the MCP resolver
(`resolve_access`) answer it from the same tables. They must give the same
answer for every shape of grant and rule, in particular when a research rule
exists that names only ANOTHER team: a rule of team C does not open the
repository to team A because A holds a read grant on it.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from tests.api.rbac_world import A_REPO, world


@pytest.fixture(autouse=True)
def _sync(tmp_path, monkeypatch):
    from src.access import resolver

    eng = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    monkeypatch.setattr(resolver, "_ENGINE", eng)
    yield
    eng.dispose()


async def _rest_reads(w, who: str) -> bool:
    from src.api.deps import enforce_repo_permission

    try:
        await enforce_repo_permission(A_REPO, w.users[who], "read", w.ws["ws-a"])
    except HTTPException:
        return False
    return True


async def _mcp_reads(w, who: str) -> bool:
    from src.access.resolver import resolve_access

    dec = await asyncio.to_thread(
        resolve_access, user_id=w.uid(who), is_admin=False,
        workspace_id=w.ws["ws-a"], repos=[A_REPO])
    return dec[A_REPO].visibility == "code"


async def _rules(w, *rules: tuple[str, str]) -> None:
    from src.db.models import RepoAccessRule, Team

    async with w.factory() as s:
        if any(t == "team-c" for t, _ in rules):
            s.add(Team(id="team-c", name="c", description="", workspace_id=w.ws["ws-a"]))
        for i, (team, visibility) in enumerate(rules):
            s.add(RepoAccessRule(id=f"rule-{i}", workspace_id=w.ws["ws-a"], team_id=team,
                                 repo_slug=A_REPO, visibility=visibility))
        await s.commit()


@pytest.mark.parametrize(("rules", "expected"), [
    ((), True),                                          # a grant alone is code
    ((("team-c", "code"),), False),                      # a rule of another team only
    ((("team-c", "metadata"),), False),
    ((("team-a", "code"),), True),                       # a rule of mine that reads
    ((("team-a", "metadata"),), False),                  # a rule of mine that narrows
    ((("team-a", "none"), ("team-c", "code")), False),
    ((("team-a", "code"), ("team-c", "none")), True),
], ids=["grant-only", "other-team-code", "other-team-metadata", "mine-code",
        "mine-metadata", "mine-none-other-code", "mine-code-other-none"])
async def test_a_grant_holder_gets_one_answer_from_both_doors(tmp_path, monkeypatch, rules, expected):
    async with world(tmp_path, monkeypatch) as w:
        await _rules(w, *rules)
        rest = await _rest_reads(w, "editor_a")
        mcp = await _mcp_reads(w, "editor_a")
        assert rest == mcp == expected, f"REST={rest} MCP={mcp}"


async def test_a_workspace_admin_is_never_narrowed_by_a_rule_of_another_team(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _rules(w, ("team-c", "none"))
        assert await _rest_reads(w, "admin_a") and await _mcp_reads(w, "admin_a")
