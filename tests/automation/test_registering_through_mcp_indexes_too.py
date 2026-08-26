"""The MCP door onto "add a repository" queues the index as well.

`register_repo` is the same verb as `POST /api/repos` for a non-human caller —
an external Claude Code, a ticket connector. It had the identical defect: it
wrote the row and stopped, so a repository registered over MCP was reviewed
with "(no graph context)" for ever.

Fixing only the HTTP route would have left the exact same silence one door
along, and the connector is the caller most likely to register in bulk and
never look at the Repositories page.

`already_registered` gets the same treatment on purpose: a connector replays a
ticket precisely when the first attempt did not finish, and answering
"already registered" while the repository still has no graph is the silence
this change exists to remove. It cannot re-clone anything — a live job or an
existing graph short-circuits inside the shared helper, which these tests
drive rather than mock.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.automation.actions import Actor, register_repo
from src.config import get_settings
from src.sync.git_providers import parse_repo_url

URL = "https://github.com/acme/billing"
SLUG = parse_repo_url(URL).slug
ACTOR = Actor(user_id="u-1", email="bot@acme.test",
              workspace_id="ws-tenant-a", label="mcp")


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}

    def upsert(self, cfg):
        self.rows[(cfg.workspace_id, cfg.repo_slug)] = cfg
        return cfg

    def existing_workspace_binding(self, provider: str, full_name: str) -> str | None:
        for cfg in self.rows.values():
            if cfg.provider == provider and cfg.full_name == full_name:
                return cfg.workspace_id
        return None

    def get_in_workspace(self, workspace_id: str, repo_slug: str):
        return self.rows.get((workspace_id, repo_slug))


class FakeQueue:
    """Same dedup rule the real `sync_jobs` partial index enforces."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.live: set[str] = set()

    def enqueue(self, *, kind, payload, dedup_key=None, enqueued_by=None, **kw):
        self.calls.append({"kind": kind, "payload": payload,
                           "dedup_key": dedup_key, "enqueued_by": enqueued_by})
        if dedup_key in self.live:
            return None
        self.live.add(dedup_key)
        return f"job-{len(self.calls)}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    store, queue = FakeStore(), FakeQueue()
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: store)
    monkeypatch.setattr("src.sync.queue.enqueue", queue.enqueue)
    yield SimpleNamespace(store=store, queue=queue, tmp=tmp_path)
    get_settings.cache_clear()


def test_registering_over_mcp_queues_the_index(env):
    out = register_repo(ACTOR, URL)

    assert out["already_registered"] is False
    assert out["index_queued"] is True
    assert out["index_status"] == "queued"
    assert len(env.queue.calls) == 1
    assert env.queue.calls[0]["kind"] == "index_repo_full"
    assert env.queue.calls[0]["payload"]["workspace_id"] == ACTOR.workspace_id


def test_it_uses_the_same_dedup_key_as_every_other_path(env):
    register_repo(ACTOR, URL)
    assert env.queue.calls[0]["dedup_key"] == f"index_full:{ACTOR.workspace_id}:{SLUG}"


def test_the_opt_out_registers_without_cloning(env):
    out = register_repo(ACTOR, URL, index=False)

    assert out["index_status"] == "not_requested"
    assert out["index_queued"] is False
    assert env.queue.calls == []
    assert env.store.get_in_workspace(ACTOR.workspace_id, SLUG) is not None


def test_a_replay_does_not_queue_a_second_clone(env):
    register_repo(ACTOR, URL)
    again = register_repo(ACTOR, URL)

    assert again["already_registered"] is True
    assert again["index_queued"] is False
    assert again["index_status"] == "already_queued"
    assert len(env.queue.live) == 1


def test_a_replay_of_an_unindexed_repo_gets_its_index_started(env):
    """The case the connector actually hits: registered once with no index
    (or the job died), replayed later. "already_registered" alone would leave
    it graphless for ever."""
    register_repo(ACTOR, URL, index=False)
    assert env.queue.calls == []

    again = register_repo(ACTOR, URL)

    assert again["already_registered"] is True
    assert again["index_status"] == "queued"
    assert len(env.queue.calls) == 1


def test_an_already_indexed_repo_is_left_alone(env):
    graph = get_settings().repo_graph_path(SLUG)
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text("pretend this is a graph", encoding="utf-8")

    out = register_repo(ACTOR, URL)

    assert out["index_status"] == "already_indexed"
    assert env.queue.calls == []


def test_a_queue_failure_does_not_lose_the_registration(env, monkeypatch):
    def boom(**kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("src.sync.queue.enqueue", boom)

    out = register_repo(ACTOR, URL)

    assert out["index_status"] == "queue_unavailable"
    assert out["index_queued"] is False
    assert env.store.get_in_workspace(ACTOR.workspace_id, SLUG) is not None


def test_the_mcp_tool_passes_the_flag_through(monkeypatch):
    """The knob has to be reachable from the surface it exists for. Executed
    against a stub registry, not grepped — a tool body sitting after a `return`
    would read the same in the source and register nothing."""
    import types
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "mcp_server"
              / "http_app.py").read_text(encoding="utf-8")
    start = source.find("def _register_tools(")
    end = source.find("\ndef _run_async(coro):")
    assert 0 < start < end, "could not isolate _register_tools"

    tools: dict[str, object] = {}

    class Stub:
        def tool(self, name=None, description=None, **kw):
            def keep(fn):
                tools[name] = fn
                return fn
            return keep

    seen: list[dict] = []

    def fake_register_repo(actor, url, branch=None, *, index=True):
        seen.append({"url": url, "branch": branch, "index": index})
        return {"slug": "s", "index_queued": index, "index_status": "queued"}

    monkeypatch.setattr("src.automation.actions.register_repo", fake_register_repo)
    ns = {"Any": object, "logger": types.SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None)}
    exec(compile(source[start:end], "http_app", "exec"), ns)
    ns["_register_tools"](Stub(), types.SimpleNamespace())

    # `_actor` lives outside the isolated block and is looked up at call time
    # against the exec namespace, so this is where it gets supplied.
    ns["_actor"] = lambda *a, **k: ACTOR

    add_repo = tools["add_repo"]
    assert add_repo(URL)["ok"] is True
    add_repo(URL, None, False)

    assert [c["index"] for c in seen] == [True, False], seen
