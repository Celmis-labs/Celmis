"""The operator's two commands for the default-deny rollout, and one non-leak.

  * `analyzer access bootstrap` gives a team the repositories nobody had a rule
    for (dry run by default), so members keep working after the upgrade;
  * `analyzer mcp issue-token` needs an explicit person, repo list and workspace;
  * access requests are about WORKSPACES, never repositories: nothing in the
    request, in the admin's list or in "my access" can name a repo.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from src.cli import app
from tests.security.mcp_world import (
    GRANTED,
    RULED,
    WILD,
    WS_A,
)

runner = CliRunner()


def _readable(world, who: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(world.self_token(who)[0])
    _caller, access = caller_access([GRANTED, RULED, WILD])
    return {s for s, d in access.items() if d.researchable}


def _team_for(world, who: str) -> None:
    from src.db.models import Team, TeamMember

    with world.session() as s:
        s.add_all([Team(id="team-n", name="newcomers", workspace_id=WS_A),
                   TeamMember(team_id="team-n", user_id=world.users[who])])
        s.commit()


def test_bootstrap_without_a_flag_lists_the_unruled_repos_and_writes_nothing(mcp_world):
    _team_for(mcp_world, "mn")
    result = runner.invoke(app, ["access", "bootstrap", "--team", "newcomers",
                                 "--workspace", WS_A])
    assert result.exit_code == 0, result.output
    assert WILD in result.output and GRANTED not in result.output and RULED not in result.output
    assert "Dry run" in result.output
    assert _readable(mcp_world, "mn") == set()


def test_bootstrap_cli_restores_access_for_the_named_team_only(mcp_world):
    _team_for(mcp_world, "mn")
    result = runner.invoke(app, ["access", "bootstrap", "--team", "newcomers",
                                 "--visibility", "code", "--workspace", WS_A,
                                 "--all-unruled"])
    assert result.exit_code == 0, result.output
    assert _readable(mcp_world, "mn") == {WILD}
    assert _readable(mcp_world, "mg") == {GRANTED}, "another team gains nothing"
    again = runner.invoke(app, ["access", "bootstrap", "--team", "newcomers",
                                "--workspace", WS_A])
    assert WILD not in again.output, "the repo has a rule now"


def test_bootstrap_cli_at_metadata_does_not_open_the_code(mcp_world):
    _team_for(mcp_world, "mn")
    result = runner.invoke(app, ["access", "bootstrap", "--team", "newcomers",
                                 "--visibility", "metadata", "--workspace", WS_A,
                                 "--all-unruled"])
    assert result.exit_code == 0, result.output
    from src.mcp_server.identity import caller_access

    mcp_world.as_(mcp_world.self_token("mn")[0])
    _caller, access = caller_access([WILD])
    assert not access[WILD].code_visible


def test_bootstrap_cli_refuses_a_bad_visibility_and_an_unknown_team(mcp_world):
    bad = runner.invoke(app, ["access", "bootstrap", "--team", "x", "--visibility", "all"])
    assert bad.exit_code == 1
    ghost = runner.invoke(app, ["access", "bootstrap", "--team", "no-such-team",
                                "--workspace", WS_A])
    assert ghost.exit_code == 1 and "no such team" in ghost.output


def test_bootstrap_cli_does_not_reach_into_another_workspaces_team(mcp_world):
    from src.db.models import Team

    with mcp_world.session() as s:
        s.add(Team(id="team-b", name="bravo-team", workspace_id="wsid-b"))
        s.commit()
    result = runner.invoke(app, ["access", "bootstrap", "--team", "bravo-team",
                                 "--workspace", WS_A, "--all-unruled"])
    assert result.exit_code == 1


def test_issue_token_cli_prints_only_the_token_and_it_works(mcp_world):
    from src.mcp_server.auth import JwtTokenVerifier

    result = runner.invoke(app, ["mcp", "issue-token", "--user", "mn@acme.io",
                                 "--repos", WILD, "--workspace", WS_A])
    assert result.exit_code == 0, result.exception
    token = result.stdout.strip()
    assert re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", token), "stdout is the token and nothing else"
    import asyncio

    assert asyncio.run(JwtTokenVerifier().verify_token(token)) is not None
    assert _readable_with(mcp_world, token) == {WILD}


def _readable_with(world, token: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(token)
    _caller, access = caller_access([GRANTED, RULED, WILD])
    return {s for s, d in access.items() if d.researchable}


def test_issue_token_cli_needs_user_repos_and_workspace(mcp_world):
    for missing in ("--user", "--repos", "--workspace"):
        args = {"--user": "mn@acme.io", "--repos": WILD, "--workspace": WS_A}
        del args[missing]
        flat = [x for kv in args.items() for x in kv]
        result = runner.invoke(app, ["mcp", "issue-token", *flat])
        assert result.exit_code != 0, missing


def test_issue_token_cli_refuses_an_unknown_person_a_non_member_and_a_dev_write(mcp_world):
    base = ["mcp", "issue-token", "--repos", WILD, "--workspace", WS_A]
    assert runner.invoke(app, [*base, "--user", "nobody@acme.io"]).exit_code == 1
    assert runner.invoke(app, [*base, "--user", "out@acme.io"]).exit_code == 1
    assert runner.invoke(app, [*base, "--user", "mn@acme.io", "--write"]).exit_code == 1


def test_an_access_request_carries_no_repository_anywhere():
    """Requests are for a workspace. The row, the person's view and the admin's
    list have no field a repository (or its slug) could ride in."""
    from src.api.routers import access_requests as ar
    from src.db.models import AccessRequest

    shapes = {
        "row": {c.name for c in AccessRequest.__table__.columns},
        "mine": set(ar.MyRequestOut.model_fields) | set(ar.MyAccessOut.model_fields),
        "admin": set(ar.AdminRequestOut.model_fields) | set(ar.GrantOut.model_fields),
        "in": set(ar.AccessRequestIn.model_fields) | set(ar.ApproveIn.model_fields),
    }
    for where, fields in shapes.items():
        leaky = [f for f in fields if re.search(r"repo|slug_of_repo|project", f, re.I)]
        assert not leaky, f"{where}: {leaky}"
