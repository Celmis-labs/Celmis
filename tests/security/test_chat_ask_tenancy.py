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

The last two sections are about a repository SHARED with the workspace. A share
lets an MCP token (and only that) ask about the owner's code, and only when the
owner allowed questions. The web chat is a person's own session: it never carries
a token's repo list or its shares into the Q&A pipeline, and a chat or a project
cannot be pointed at a shared repository, because it is not one of the workspace's
registered repositories.
"""

from __future__ import annotations

import ast
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


# ─── a repository shared with the workspace is not part of its web chat ───


def _calls_in(source: str, function: str | None, callee: str):
    """The keyword names of every call to ``callee`` (a function or a method) made
    inside ``function``, or anywhere when ``function`` is None."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function is not None and node.name != function:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.attr if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", None))
            if name == callee:
                yield node.name, {k.arg for k in call.keywords}


def test_the_web_chat_asks_the_pipeline_as_a_person_and_never_as_a_token():
    """Both web routes (the chat ask and its stream) reach the pipeline without a
    token's repo list or its shares: that is how a shared repository stays out of
    them. A person's access is their own workspace's (`resolve_access`)."""
    seen = []
    for function, callee in (("ask", "_generate_full"), ("_ask_stream", "retrieve")):
        seen += [(function, kw) for _name, kw in _calls_in(QA, function, callee)]
    assert len(seen) == 2, "the two web ask paths moved: re-check what they pass"
    for function, keywords in seen:
        assert not keywords & {"token_filter", "token_shares", "name_free_notice"}, (
            f"{function} hands the pipeline a token's rights")


def test_only_the_token_verbs_hand_shares_to_the_pipeline():
    """The set of places that put a share into a question, pinned: the MCP `ask`
    action, the actor it is built from, and the pass-through in between. A new one
    has to be added here on purpose, with its own test on ``allow_ask``."""
    allowed = {
        "api/routers/qa.py",                    # _generate_full: a parameter, passed on
        "automation/actions.py",                # Actor.token_shares
        "automation/actions_reviews.py",        # ask_code: only the shares that allow it
        "mcp_server/identity.py",               # actor_for: from the verified token
        "mcp_server/dev_profile/tools_ask.py",  # the dev `ask` verb
        "qa/multi_repo_retriever.py",           # retrieve: Principal(share_ids=...)
    }
    src_dir = ROOT / "src"
    found = {
        str(path.relative_to(src_dir))
        for path in src_dir.rglob("*.py")
        if re.search(r"\btoken_shares\b", path.read_text())
    }
    assert found == allowed


def test_the_stream_route_has_no_way_to_take_shares_at_all():
    from src.api.routers import qa

    params = inspect.signature(qa._ask_stream).parameters
    assert not {"token_shares", "token_filter"} & set(params)


async def test_a_chat_cannot_be_started_on_a_repository_shared_with_the_workspace(
        tmp_path, monkeypatch):
    from src.api.routers import chats
    from tests.api.sharing_world import A_REPO, B_REPO, add_share, shared_world

    async with shared_world(tmp_path, monkeypatch, extra_routers=(chats.router,)) as w:
        add_share(w, ask=True)       # the owner allows questions: this is the open case
        mine = await w.client.post("/api/chats", json={"repo_slug": A_REPO},
                                   headers=w.h("admin_a", "ws-a"))
        assert mine.status_code == 201, mine.text
        theirs = await w.client.post("/api/chats", json={"repo_slug": B_REPO},
                                     headers=w.h("admin_a", "ws-a"))
        assert theirs.status_code == 404 and theirs.json()["detail"] == "repo not registered"


async def test_a_project_cannot_take_a_repository_shared_with_the_workspace(
        tmp_path, monkeypatch):
    from src.api.routers import projects
    from tests.api.sharing_world import A_REPO, B_REPO, add_share, shared_world

    async with shared_world(tmp_path, monkeypatch, extra_routers=(projects.router,)) as w:
        add_share(w, ask=True)
        mine = await w.client.post(
            "/api/projects", json={"name": "mine", "repos": [{"repo_slug": A_REPO}]},
            headers=w.h("admin_a", "ws-a"))
        assert mine.status_code == 201, mine.text
        theirs = await w.client.post(
            "/api/projects", json={"name": "theirs", "repos": [{"repo_slug": B_REPO}]},
            headers=w.h("admin_a", "ws-a"))
        assert theirs.status_code == 404 and B_REPO in theirs.json()["detail"]

