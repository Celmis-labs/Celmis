"""The set operations, reachable by an agent as well as by a person.

`start_dep_audit` was already an MCP tool — which is exactly why the fifty-
repository cap applied to agents and not to the web, and why converging the two
implementations mattered. `generate_docs` is new and goes out the same door
from the start, so there is no second implementation to diverge from.

The thing worth pinning is not that the tools exist but that they cannot become
a way around the guards: the tool calls the action, the action holds the caps,
and the scope required to reach it is the same one that registering a
repository needs.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
APP = (SRC / "mcp_server" / "http_app.py").read_text(encoding="utf-8")


def test_both_verbs_are_exposed():
    for tool in ("start_dep_audit", "generate_docs"):
        assert f'name="{tool}"' in APP, f"{tool} is not reachable over MCP"


def test_they_go_through_the_action_not_around_it():
    """A tool that queued its own job would be the third implementation, and
    the caps live in the action."""
    for verb in ("start_dep_audit", "generate_docs"):
        assert f"from src.automation.actions import ActionError, {verb}" in APP


def test_they_need_a_write_scope():
    """Both spend money — an audit clones and calls advisory databases, a vault
    build calls a model once per module. A read-only token must not reach
    either, and the browser-issued token is read-only by construction."""
    from src.api.routers.mcp_access import _TOKEN_SCOPES

    for verb in ("start_dep_audit", "generate_docs"):
        assert f'"{verb}": "write:repos"' in APP, f"{verb} has no scope"
    assert "write:repos" not in _TOKEN_SCOPES


def test_a_refusal_comes_back_as_an_answer_not_an_exception():
    """An agent that gets a traceback retries it. One that is told "at most 50
    repositories" narrows the request."""
    start = APP.index('name="generate_docs"')
    body = APP[start:start + 2200]
    assert "except ActionError as exc:" in body
    assert '"ok": False' in body


def test_the_description_says_what_missing_only_is_for():
    """The single most consequential argument: without it the same phrase means
    "regenerate everything", which is hours of model time and almost never what
    was meant. A model choosing the call has to know that from the description
    alone."""
    start = APP.index('name="generate_docs"')
    description = APP[start:APP.index("async def _generate_docs", start)]
    assert "missing_only" in description
    assert "regenerates everything" in description
    assert "not indexed" in description, (
        "nothing warns that an unindexed repository is refused rather than "
        "documented from its filenames"
    )
