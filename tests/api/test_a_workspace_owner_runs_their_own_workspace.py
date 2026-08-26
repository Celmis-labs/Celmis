"""Teams and access rules were the installation operator's job, not the owner's.

Two adjacent workspace-scoped operations, two different gates. Invites used
`require_workspace_admin`; creating a team, granting a repository to one and
writing an access rule all used `require_admin` — the INSTALLATION's admin. So
a workspace owner could invite a colleague and then could not put them in a
team or limit what they see. In a multi-tenant install every tenant had to
email the operator to organise their own people.

That it was an oversight rather than a decision is visible in the code:
`upsert_rule` already refuses a team from another workspace
(`team.workspace_id != ws_id`), which is exactly the check a workspace-scoped
gate needs and pointless under a global one.

AND THE GATE WAS CARRYING THE SCOPING. Every route taking a `team_id` from the
path read it without asking whose team it was — safe only while it demanded
the installation's admin. Two of them demanded nothing at all:

    GET /api/teams/{team_id}/members  → 200, another tenant's user ids
    GET /api/teams/{team_id}/repos    → 200

Reproduced against production from an unrelated account before this change. So
lowering the gate without adding the check would have handed every workspace
admin every other workspace's teams. Both halves land together, and the tests
below are mostly about the second.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEAMS = (ROOT / "src/api/routers/teams.py").read_text(encoding="utf-8")
ACCESS = (ROOT / "src/api/routers/access.py").read_text(encoding="utf-8")


def _routes(src: str):
    """(path, function node) for every decorated route in a router module."""
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) \
               and d.func.attr in {"get", "post", "put", "delete"} and d.args:
                yield d.args[0].value, n


# ─── the owner runs their own workspace ──────────────────────────────


def test_no_route_in_either_router_demands_the_installation_admin():
    """`require_admin` is the operator. Nothing here is the operator's job."""
    assert "require_admin" not in TEAMS.replace("require_workspace_admin", "")
    assert "require_admin" not in ACCESS.replace("require_workspace_admin", "")


def test_the_write_routes_are_gated_at_all():
    """The other way to pass the test above would have been to remove the
    gates entirely."""
    written = 0
    for path, fn in _routes(TEAMS):
        src = ast.get_source_segment(TEAMS, fn) or ""
        head = src.split("->")[0]
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {"post", "put", "delete"}:
                assert "require_workspace_admin" in head, f"{path} has no gate"
                written += 1
    assert written >= 5, "the write surface shrank — check this test still covers it"


# ─── and cannot reach anybody else's ─────────────────────────────────


def test_every_team_id_route_checks_the_workspace():
    """The check the gate used to stand in for. A `team_id` is a uuid in a
    path; nothing about holding one proves it is yours."""
    missing = []
    for path, fn in _routes(TEAMS):
        if "{team_id}" not in path:
            continue
        src = ast.get_source_segment(TEAMS, fn) or ""
        if "_team_in_workspace" not in src:
            missing.append(f"{path} ({fn.name})")
    assert not missing, f"team routes that never ask whose team it is: {missing}"


def test_every_team_id_route_takes_a_workspace():
    """It cannot check what it was never given."""
    missing = []
    for path, fn in _routes(TEAMS):
        if "{team_id}" not in path:
            continue
        head = (ast.get_source_segment(TEAMS, fn) or "").split("->")[0]
        if "current_workspace_id" not in head:
            missing.append(f"{path} ({fn.name})")
    assert not missing, f"no workspace in scope: {missing}"


def test_the_two_readable_routes_are_covered_too():
    """`GET /{team_id}/members` and `GET /{team_id}/repos` had no gate AND no
    check — any authenticated user, any team, 200 and the user ids."""
    import src.api.routers.teams as t

    for fn in (t.list_members, t.list_team_repos):
        src = inspect.getsource(fn)
        assert "_team_in_workspace" in src, fn.__name__


def test_a_stranger_is_told_the_team_does_not_exist():
    """404, not 403: asking about a team you do not own should not tell you it
    is there. Same choice `_load_owned` makes for groups.

    Read off the AST, not the text. The first draft grepped the source for
    "403" — and matched the docstring that explains why there is no 403,
    failing on the very comment documenting the behaviour it was checking.
    That is a recurring mistake in this repository and it has its own note."""
    import src.api.routers.teams as t

    tree = ast.parse(inspect.getsource(t._team_in_workspace).lstrip())
    codes = {
        kw.value.value
        for n in ast.walk(tree) if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "HTTPException"
        for kw in n.keywords
        if kw.arg == "status_code" and isinstance(kw.value, ast.Constant)
    }
    assert codes == {404}, f"raises {codes or 'nothing'}; 403 confirms the team exists"


def test_the_access_rule_still_refuses_a_foreign_team():
    """This check predates the gate change and is what made it safe: a
    workspace admin naming somebody else's team gets nothing."""
    import src.api.routers.access as a

    src = inspect.getsource(a.upsert_rule)
    assert "team.workspace_id != ws_id" in src
