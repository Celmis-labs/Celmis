"""`ask` is rationed and gated; the graph is opened per call and closed off the hot path."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from src.mcp_server.dev_profile import access, common, emit, tools_ask
from tests.mcp_server.dev.conftest import BILLING, SHOP

# ─── the scope check inside every tool ───────────────────────────────

def _token(*scopes: str):
    return SimpleNamespace(scopes=list(scopes), client_id="c")


@pytest.fixture
def with_token(monkeypatch):
    import mcp.server.auth.middleware.auth_context as ctx

    def apply(token):
        monkeypatch.setattr(ctx, "get_access_token", lambda: token)

    return apply


def test_a_token_without_the_read_code_scope_is_refused_inside_the_tool(with_token):
    from mcp.server.fastmcp.exceptions import ToolError

    with_token(_token("read:graph"))
    with pytest.raises(ToolError):
        access.require_dev_scope()


def test_a_token_with_the_read_code_scope_passes(with_token):
    with_token(_token("read:code"))
    access.require_dev_scope()


def test_an_admin_token_passes(with_token):
    with_token(_token("admin"))
    access.require_dev_scope()


def test_a_call_with_no_token_at_all_is_the_trusted_local_case(with_token):
    with_token(None)
    access.require_dev_scope()


# ─── ask ─────────────────────────────────────────────────────────────

@pytest.fixture
def ask_env(monkeypatch, as_caller, world):
    tools_ask._calls.clear()
    answered: list[list[str]] = []

    async def fake_ask(actor, _unused, *, question, repo_slugs):
        answered.append(list(repo_slugs))
        return {"answer": "It is done in create_order.", "files": ["app/services/orders.py"]}

    import src.automation.actions_reviews as ar

    monkeypatch.setattr(ar, "ask_code", fake_ask)
    monkeypatch.setattr(tools_ask, "_actor", lambda: object())
    return answered


def test_ask_prints_the_answer_under_the_idx_line_with_its_sources(ask_env):
    out = asyncio.run(tools_ask.run("where are orders created?", [SHOP]))
    assert out.startswith("idx: ") and "It is done in create_order." in out
    assert "sources: app/services/orders.py" in out
    assert ask_env == [[SHOP]]


def test_ask_over_a_repo_with_metadata_only_access_is_refused_like_a_missing_repo(ask_env, as_caller):
    as_caller({SHOP: "metadata", BILLING: "code"})
    out = asyncio.run(tools_ask.run("q?", [SHOP]))
    assert "not found or not accessible" in out and ask_env == []


def test_ask_without_repos_covers_every_repo_the_caller_can_read_in_full(ask_env, as_caller):
    as_caller({SHOP: "metadata", BILLING: "code"})
    asyncio.run(tools_ask.run("q?"))
    assert ask_env == [[BILLING]]


def test_ask_is_limited_to_five_calls_a_minute_per_user(ask_env):
    for _ in range(tools_ask.RATE_LIMIT):
        assert "rate limited" not in asyncio.run(tools_ask.run("q?", [SHOP]))
    assert "rate limited" in asyncio.run(tools_ask.run("q?", [SHOP]))
    assert len(ask_env) == tools_ask.RATE_LIMIT


def test_an_empty_question_is_an_error_line_and_costs_nothing(ask_env):
    assert "question is empty" in asyncio.run(tools_ask.run("  ", [SHOP]))
    assert ask_env == []


def test_the_answer_is_cut_to_the_requested_token_budget(monkeypatch, ask_env):
    import src.automation.actions_reviews as ar

    async def long_answer(*a, **kw):
        return {"answer": "\n".join(f"line {i} of a long answer" for i in range(500))}

    monkeypatch.setattr(ar, "ask_code", long_answer)
    out = asyncio.run(tools_ask.run("q?", [SHOP], budget_tokens=200))
    assert len(out) <= int(200 * emit.CHARS_PER_TOKEN) + 150
    assert "truncated" in out.split("\n")[-1]


# ─── the graph lifecycle ─────────────────────────────────────────────

class _SlowClose:
    def __init__(self, log: list[str], delay: float):
        self.log, self.delay = log, delay

    def close(self) -> None:
        self.log.append("closing")
        time.sleep(self.delay)
        self.log.append("closed")


@pytest.fixture
def fake_graphs(monkeypatch, world):
    import src.indexing.graph.graph_store as gs

    log: list[str] = []
    monkeypatch.setattr(gs, "make_graph_store", lambda path: (log.append("open"), _SlowClose(log, 0.4))[1])
    return log


def test_the_caller_does_not_wait_for_the_graph_to_shut_down(fake_graphs):
    started = time.monotonic()
    with common.open_store(SHOP):
        pass
    assert time.monotonic() - started < 0.3
    deadline = time.monotonic() + 3
    while "closed" not in fake_graphs and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fake_graphs == ["open", "closing", "closed"]


def test_the_next_open_of_the_same_repo_waits_for_the_previous_close(fake_graphs):
    with common.open_store(SHOP):
        pass
    with common.open_store(SHOP):
        pass
    # Never two servers on one file: the second open comes after the first close.
    assert fake_graphs.index("open", 1) > fake_graphs.index("closed")


def test_two_different_repos_do_not_wait_for_each_other(fake_graphs):
    with common.open_store(SHOP):
        pass
    started = time.monotonic()
    with common.open_store(BILLING):
        pass
    assert time.monotonic() - started < 0.3


def test_an_open_that_fails_releases_the_lock(monkeypatch, world):
    import src.indexing.graph.graph_store as gs

    def boom(path):
        raise OSError("no such graph")

    monkeypatch.setattr(gs, "make_graph_store", boom)
    with pytest.raises(OSError), common.open_store(SHOP):
        pass
    monkeypatch.setattr(gs, "make_graph_store", lambda path: _SlowClose([], 0))
    done = threading.Event()

    def reopen():
        with common.open_store(SHOP):
            done.set()

    t = threading.Thread(target=reopen)
    t.start()
    t.join(3)
    assert done.is_set()


def test_a_graph_that_stays_busy_gives_up_with_a_timeout(monkeypatch, world):
    monkeypatch.setattr(common, "LOCK_TIMEOUT_S", 0.1)
    lock = common._slug_lock("github_acme-stuck")
    lock.acquire()
    try:
        with pytest.raises(TimeoutError), common.open_store("github_acme-stuck"):
            pass
    finally:
        lock.release()


def test_ask_never_lists_a_secret_file_among_its_sources(monkeypatch, ask_env):
    import src.automation.actions_reviews as ar

    async def answer(*a, **kw):
        return {"answer": "ok", "files": [".env", "app/services/orders.py", "deploy/server.key",
                                          f"{SHOP}:config/.env.local"]}

    monkeypatch.setattr(ar, "ask_code", answer)
    out = asyncio.run(tools_ask.run("q?", [SHOP]))
    assert "sources: app/services/orders.py" in out
    assert ".env" not in out and "server.key" not in out


def test_the_ask_rate_table_forgets_callers_whose_window_has_passed(ask_env):
    tools_ask._calls.update({f"u{i}": [0.0] for i in range(tools_ask._MAX_TRACKED + 5)})
    asyncio.run(tools_ask.run("q?", [SHOP]))
    assert len(tools_ask._calls) < 10
