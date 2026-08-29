"""The write half of the automation surface: what it refuses.

Until now every non-human caller could only read. These verbs let an external
Claude Code register the repositories someone listed in a ticket, audit them,
and read the findings back — the loop that turns "check these four services"
into an answer without a person clicking through four pages.

Writing costs money and touches a tenant's data, so the tests here are about
refusals rather than happy paths: an unregistered slug, a repository that
belongs to someone else, a second audit while one is running, a fan-out with no
ceiling. The happy path is exercised against production; these pin the rules
that keep it from being a way to spend another workspace's budget.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.automation.actions import MAX_AUDIT_REPOS, Actor


def test_an_actor_carries_its_own_workspace():
    """No ambient request context. A connector draining a queue has no request,
    and a function that guesses the workspace is how one tenant's automation
    reaches another's repositories."""
    actor = Actor(user_id="u1", email="a@b.c", workspace_id="ws-1", label="jira")
    assert actor.workspace_id == "ws-1"
    assert actor.label == "jira"
    with pytest.raises(dataclasses.FrozenInstanceError):
        actor.workspace_id = "ws-2"  # frozen — an actor cannot drift mid-run


def test_the_fan_out_has_a_ceiling():
    """An automated caller must not be able to queue an unbounded sweep."""
    assert 0 < MAX_AUDIT_REPOS <= 100


@pytest.mark.parametrize("engine", ["none", "api", "claude_code"])
def test_the_report_engines_are_the_three_the_api_accepts(engine: str):
    """Same set as StartAuditIn's pattern — a fourth value would be accepted
    here and rejected by the worker, which is a failure with no error."""
    import re
    from pathlib import Path

    deps = (Path(__file__).resolve().parents[2]
            / "src" / "api" / "routers" / "deps.py").read_text()
    pattern = re.search(r'report_engine: str = Field\(default="none", pattern="([^"]+)"', deps)
    assert pattern, "report_engine pattern not found in StartAuditIn"
    assert re.match(pattern.group(1), engine), engine


def test_every_action_takes_an_explicit_actor():
    """A signature without an actor is a signature that will read one from
    somewhere ambient the first time someone is in a hurry."""
    import inspect

    from src.automation import actions

    for name in ("register_repo", "start_dep_audit", "get_dep_audit",
                 "list_dep_findings"):
        fn = getattr(actions, name)
        first = list(inspect.signature(fn).parameters)[0]
        assert first == "actor", f"{name} takes {first!r} first, not an actor"


AUTOMATION_TOOLS = ("add_repo", "start_dep_audit", "get_dep_audit",
                    "list_dep_findings")


def test_the_http_builder_actually_registers_the_automation_tools():
    """Executed, not grepped.

    The first version of this test looked for `name="add_repo"` in the file
    and passed while the whole block sat AFTER a `return` — dead code that
    registered nothing, so production listed thirteen tools and none of them.
    A text search cannot tell reachable code from unreachable, so this runs
    the registration against a stub and asks what it was handed.
    """
    import types
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "mcp_server"
              / "http_app.py").read_text()
    start = source.find("def _register_tools(")
    end = source.find("\ndef _run_async(coro):")
    assert 0 < start < end, "could not isolate _register_tools"

    seen: list[str] = []

    class Stub:
        def tool(self, name=None, description=None, **kw):
            seen.append(name)
            return lambda f: f

    ns = {"Any": object, "logger": types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)}
    exec(compile(source[start:end], "http_app", "exec"), ns)
    ns["_register_tools"](Stub(), types.SimpleNamespace())

    missing = [t for t in AUTOMATION_TOOLS if t not in seen]
    assert not missing, f"registered {len(seen)} tools, missing {missing}"


def test_the_stdio_builder_carries_them_too():
    """server.py backs `analyzer mcp serve`; http_app.py backs the HTTP mount.
    A tool in one and not the other is a feature that works on a laptop and
    not in production."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "src" / "mcp_server"
            / "server.py").read_text()
    for tool in AUTOMATION_TOOLS:
        assert f'name="{tool}"' in text, f"server.py is missing {tool}"


def test_the_http_builder_gates_the_write_tools_by_scope():
    """http_app filters tools/list by scope rather than decorating each body,
    so the gate lives in the map — a tool missing from it is visible to
    everyone."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "src" / "mcp_server"
            / "http_app.py").read_text()
    table = text[text.find("_TOOL_SCOPES"):]
    table = table[:table.find("}") + 1]
    assert '"add_repo": "write:repos"' in table
    assert '"start_dep_audit": "write:repos"' in table
    assert '"get_dep_audit": "read:graph"' in table
    assert '"list_dep_findings": "read:graph"' in table


def test_the_write_tools_require_a_scope_no_read_token_carries():
    """`write:repos` is new on purpose.

    Adding these verbs under an existing scope would hand every token already
    issued for reading the ability to register repositories and start audits —
    silently, to tokens nobody re-consented.
    """
    from pathlib import Path

    server = (Path(__file__).resolve().parents[2]
              / "src" / "mcp_server" / "server.py").read_text()
    for tool in ("_add_repo", "_start_dep_audit"):
        idx = server.find(f"def {tool}(")
        assert idx > 0, tool
        # The decorator sits directly above the definition.
        preceding = server[max(0, idx - 400):idx]
        assert 'require_scopes("write:repos")' in preceding, tool


def test_reading_an_audit_does_not_need_the_write_scope():
    """Polling a run and reading findings are reads. Requiring the write scope
    for them would push callers to ask for more than they need."""
    from pathlib import Path

    server = (Path(__file__).resolve().parents[2]
              / "src" / "mcp_server" / "server.py").read_text()
    for tool in ("_get_dep_audit", "_list_dep_findings"):
        idx = server.find(f"def {tool}(")
        preceding = server[max(0, idx - 400):idx]
        assert 'require_scopes("write:repos")' not in preceding, tool
        assert "require_scopes(" in preceding, tool


def test_an_account_with_a_narrowed_scope_list_is_left_alone():
    """The upgrade adds new standard scopes to accounts carrying the standard
    set. Someone deliberately reduced to read-only must not be widened back by
    a deploy."""
    from types import SimpleNamespace

    from src.users.scopes import STANDARD_SCOPES, held_scopes

    narrowed = SimpleNamespace(scopes=["read:graph"])
    assert held_scopes(narrowed) == ["read:graph"]

    standard = SimpleNamespace(scopes=["read:graph", "read:groups", "write:groups"])
    assert set(held_scopes(standard)) >= set(STANDARD_SCOPES)


def test_both_surfaces_ask_the_same_question_about_scopes():
    """Token issuance and client registration disagreed once: one computed the
    upgrade, the other read the stored list, so a caller was refused a scope
    their own token was already carrying. One function answers now."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "api" / "routers"
    for name in ("auth.py", "oauth.py"):
        assert "held_scopes" in (root / name).read_text(), name


def test_a_client_token_without_a_resolvable_owner_cannot_write():
    """The tenancy hole this surface would otherwise have opened.

    A client_credentials token names a client, not a person, so the workspace
    resolver falls back to "default". Reading there is harmless. Writing would
    register a repository into a tenant nobody chose — so a write refuses
    unless the workspace was actually resolved, and says what to fix.
    """
    from pathlib import Path

    server = (Path(__file__).resolve().parents[2]
              / "src" / "mcp_server" / "server.py").read_text()
    idx = server.find("def _actor(")
    body = server[idx:idx + 1200]
    assert "workspace_resolved" in body, "the write path does not check resolution"
    assert "writing" in body, "reads and writes are held to the same bar"

    # Both write tools must pass writing=True; a read must not.
    for tool in ("_add_repo", "_start_dep_audit"):
        start = server.find(f"def {tool}(")
        assert 'writing=True' in server[start:start + 900], tool
    for tool in ("_get_dep_audit", "_list_dep_findings"):
        start = server.find(f"def {tool}(")
        assert 'writing=True' not in server[start:start + 900], tool


def test_the_caller_reports_whether_the_workspace_was_resolved():
    """`workspace_resolved` defaults True so stdio and tests keep working —
    the flag exists to mark the ONE case that must not write."""
    from src.mcp_server.identity import McpCaller

    default = McpCaller("u1", False, "ws-1", (), authenticated=True)
    assert default.workspace_resolved is True

    fallback = McpCaller("client:x", False, "default", (),
                         authenticated=True, workspace_resolved=False)
    assert fallback.workspace_resolved is False


def test_a_workspace_owner_can_register_a_client_but_not_widen_themselves():
    """The gate that made the whole surface unreachable.

    Registering an OAuth client needed a PLATFORM admin, while the people who
    own the repositories an automation would act on are workspace owners. A
    client is bound to whoever registered it — the MCP resolver reads
    created_by and hands the token that person's workspace — so it grants no
    authority the registrant does not already have by clicking. What it must
    not become is a way to mint a scope you do not hold.
    """
    from pathlib import Path

    oauth = (Path(__file__).resolve().parents[2]
             / "src" / "api" / "routers" / "oauth.py").read_text()
    idx = oauth.find("async def register_client(")
    head = oauth[idx:idx + 1400]
    assert "require_workspace_admin" in head, "registration still needs a platform admin"
    assert "held_scopes" in head and "is_admin" in head, (
        "a non-platform-admin must be held to the scopes they actually hold"
    )
    # Listing and deleting OTHER PEOPLE'S clients stays platform-admin work.
    #
    # This used to be spelled `require_admin`, which also locked the registrant
    # out of the client they had just made: you could mint a credential and
    # then neither see it nor revoke it. Both handlers now take a workspace
    # admin and narrow by ownership instead, which is strictly less than the
    # old 403 gave a non-platform-admin.
    #
    # Read with ast rather than grepped: `created_by` appears in the body of
    # list_clients whatever it does, because the summary reports that field —
    # a substring check would pass on a handler with no filter at all.
    import ast

    tree = ast.parse(oauth)
    for handler in ("list_clients", "delete_client"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == handler
        )
        compares = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Compare)
            and any(
                isinstance(side, ast.Attribute) and side.attr == "created_by"
                for side in [n.left, *n.comparators]
            )
            and any(
                isinstance(side, ast.Attribute) and side.attr == "email"
                for side in [n.left, *n.comparators]
            )
        ]
        assert compares, (
            f"{handler} does not compare created_by against the caller's "
            f"email, so it either hands out or acts on somebody else's clients"
        )
        checks_admin = any(
            isinstance(n, ast.Attribute) and n.attr == "is_admin"
            for n in ast.walk(fn)
        )
        assert checks_admin, (
            f"{handler} narrows by owner but never lets a platform admin past"
        )
