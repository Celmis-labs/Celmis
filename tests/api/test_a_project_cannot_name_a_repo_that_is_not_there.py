"""Creating a project validates its members, like creating a chat does.

THE DEFECT. Two endpoints set the same kind of target and disagreed about
whether the target has to exist:

    POST /api/chats    {"repo_slug": "github_does-not-exist-anywhere"}
      → 404 "repo not registered"

    POST /api/projects {"repos":[{"repo_slug":"github_does-not-exist-anywhere"}]}
      → 201 CREATED

A project is what a question is asked against. A silently bogus member means
retrieval quietly searches one repository fewer than the user believes, and
nothing anywhere says so — the project page lists it, the chat runs, the
answer is simply thinner.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.auto_review import AutoReviewStore, RepoConfig


@pytest.fixture()
def store(tmp_path, monkeypatch) -> AutoReviewStore:
    s = AutoReviewStore(tmp_path / "ar.db")
    s.upsert(RepoConfig(
        user_id="alice@example.com", repo_slug="github_acme-api",
        provider="github", full_name="acme/api",
        url="https://github.com/acme/api", workspace_id="ws-1",
    ))
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: s)
    return s


def test_a_registered_repo_is_accepted(store):
    from src.api.routers.projects import _require_registered

    _require_registered(["github_acme-api"], "ws-1")


def test_an_unregistered_repo_is_refused(store):
    from src.api.routers.projects import _require_registered

    with pytest.raises(HTTPException) as exc:
        _require_registered(["github_does-not-exist-anywhere"], "ws-1")

    assert exc.value.status_code == 404
    assert "github_does-not-exist-anywhere" in str(exc.value.detail)


def test_a_repo_in_another_workspace_is_refused(store):
    """Registered somewhere is not registered here — and a project that could
    name another tenant's repo is worse than one that names nothing."""
    from src.api.routers.projects import _require_registered

    with pytest.raises(HTTPException):
        _require_registered(["github_acme-api"], "ws-2")


def test_every_missing_member_is_named_at_once(store):
    """Reporting them one at a time makes a user fix a five-repo project in
    five round trips."""
    from src.api.routers.projects import _require_registered

    with pytest.raises(HTTPException) as exc:
        _require_registered(["nope-one", "github_acme-api", "nope-two"], "ws-1")

    detail = str(exc.value.detail)
    assert "nope-one" in detail
    assert "nope-two" in detail


def test_an_empty_project_is_allowed(store):
    """Repos can be added later; refusing an empty one would be a new rule,
    not a fix."""
    from src.api.routers.projects import _require_registered

    _require_registered([], "ws-1")


def test_the_two_endpoints_now_agree():
    """The defect was the disagreement, so this pins both sides."""
    import inspect

    from src.api.routers import chats, projects

    assert "get_in_workspace" in inspect.getsource(chats)
    assert "get_in_workspace" in inspect.getsource(projects._require_registered)


def test_adding_a_member_one_at_a_time_is_checked_too():
    """The other way into the same silently-bogus project."""
    import ast
    import inspect

    from src.api.routers import projects

    tree = ast.parse(inspect.getsource(projects))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "add_repo":
            assert "_require_registered" in ast.unparse(node)
            return
    raise AssertionError("add_repo not found — did the router move?")
