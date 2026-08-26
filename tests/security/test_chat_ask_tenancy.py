"""The by-id route that the last tenancy sweep missed.

`/api/chats` gained `_owned_chat` on every by-id handler. `POST
/api/qa/chats/{chat_id}/ask` lives in a different router and kept loading the
chat straight from its id, which made it strictly worse than the reads that
were fixed:

  * the whole transcript is passed to the model as `history`, so "summarise the
    conversation above" exfiltrates another tenant's questions and the answers
    quoting their source;
  * the attacker's message is appended to the victim's chat;
  * the victim's chat is renamed by `auto_name_chat`;
  * `chat.project_id` was dereferenced through an equally unscoped
    `get_project`, handing back the other workspace's repo list.

Creation was the other half: `project_id` and `repo_slug` came from the request
body and were never checked, so a chat could be created inside your own
workspace pointing at somebody else's project or repository.

Source-level assertions: exercising the streaming endpoint needs a database, a
retriever and a provider, and a test that needs all three is the test that does
not run in CI.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QA = (ROOT / "src" / "api" / "routers" / "qa.py").read_text()
CHATS = (ROOT / "src" / "api" / "routers" / "chats.py").read_text()
REPOSITORIES = (ROOT / "src" / "api" / "repositories.py").read_text()


def test_ask_loads_the_chat_scoped_to_the_caller_workspace():
    idx = QA.find("chat = await repo.get_chat(")
    assert idx > 0, "the chat load moved — re-check that it is still scoped"
    call = QA[idx:idx + 220]
    assert "workspace_id=workspace_id" in call, (
        "POST /api/qa/chats/{id}/ask loads a chat by id with no workspace "
        "filter — any signed-in user who knows an id reads and writes another "
        "tenant's conversation"
    )


def test_ask_scopes_the_project_dereference_too():
    """A chat may legitimately be yours while carrying a foreign project_id."""
    idx = QA.find("project = await repo.get_project(")
    assert idx > 0
    assert "workspace_id=workspace_id" in QA[idx:idx + 200]


def test_the_repository_layer_can_enforce_the_boundary_itself():
    """So the next by-id caller cannot forget it by omission."""
    from src.api import repositories

    for name in ("get_chat", "get_project"):
        params = inspect.signature(getattr(repositories, name)).parameters
        assert "workspace_id" in params, f"{name} cannot be scoped"
        assert params["workspace_id"].kind is inspect.Parameter.KEYWORD_ONLY

    assert "Chat.workspace_id == workspace_id" in REPOSITORIES
    assert "Project.workspace_id == workspace_id" in REPOSITORIES


def test_a_chat_cannot_be_created_pointing_at_a_foreign_project():
    idx = CHATS.find("async def create_chat(")
    body = CHATS[idx:CHATS.find("\n@router", idx + 10)]
    assert "_owned_project" in body, (
        "project_id comes from the request body — without this check a chat in "
        "your workspace resolves to another tenant's repositories"
    )


def test_a_chat_cannot_be_created_pointing_at_an_unregistered_repo():
    idx = CHATS.find("async def create_chat(")
    body = CHATS[idx:CHATS.find("\n@router", idx + 10)]
    assert "get_in_workspace(ws_id, payload.repo_slug)" in body, (
        "repo_slug comes from the request body — retrieval reads "
        "repos_dir/{slug}, so an unvalidated slug points it at another "
        "tenant's checked-out source"
    )
    assert "404" in body


def test_every_by_id_chat_and_project_load_is_authorized():
    """The sweep that catches the next one. Any `get_chat(`/`get_project(` in a
    ROUTER must either pass workspace_id or sit after an _owned_* guard."""
    for path in ("qa.py", "chats.py", "projects.py"):
        source = (ROOT / "src" / "api" / "routers" / path).read_text()
        for match in re.finditer(r"await repo\.get_(chat|project)\(", source):
            window_before = source[max(0, match.start() - 400):match.start()]
            window_at = source[match.start():match.start() + 260]
            scoped = "workspace_id=workspace_id" in window_at
            guarded = ("_owned_chat(" in window_before
                       or "_owned_project(" in window_before)
            assert scoped or guarded, (
                f"{path}: a by-id load at offset {match.start()} is neither "
                f"scoped nor preceded by an ownership guard"
            )


def test_the_guards_are_reachable_from_both_routers():
    """A guard nobody can import is a guard nobody uses. Also pins that the
    import direction stays acyclic — projects.py must not import chats.py."""
    from src.api.routers.chats import _owned_chat
    from src.api.routers.projects import _owned_project

    assert callable(_owned_chat) and callable(_owned_project)
    projects = (ROOT / "src" / "api" / "routers" / "projects.py").read_text()
    assert "from src.api.routers.chats import" not in projects
