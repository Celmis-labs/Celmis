"""A question to the reviewer costs money, so it is bounded three ways: the
workspace's budget can refuse it (one sentence back, no model call), what it
spends is booked under its own surface (`pr_chat`, so the Usage page can tell
chat from reviews), and what the model reads and writes is capped."""

from __future__ import annotations

from types import SimpleNamespace

import src.llm.client as llm_client
from src.llm.budget import SURFACE_PR_CHAT, SURFACE_REVIEW, BudgetExceeded, BudgetUnavailable
from src.review import memories
from src.review.commands import chat, handlers
from src.review.commands.handlers import CommandContext
from src.review.commands.parser import parse_comment
from src.review.models import Hunk, PullRequest
from tests.review.comment_support import FakeProvider, event


def _ctx(body="@celmis why is the total wrong?", provider=None) -> CommandContext:
    parsed = parse_comment(body, "@celmis")
    return CommandContext(
        ev=event(body=body), provider=provider or FakeProvider(), command=parsed,
        settings={}, workspace_id="ws-a", user_id="u-1")


def _no_memories(monkeypatch):
    monkeypatch.setattr(memories, "render_for_chat", lambda *a, **k: "")


def _raises(exc):
    def enforce(_ws):
        raise exc
    return enforce


def test_a_workspace_over_its_budget_gets_one_line_and_no_model_call(monkeypatch):
    called: list = []
    monkeypatch.setattr(chat, "enforce", _raises(BudgetExceeded("ws-a", 12.0, 10.0)))
    monkeypatch.setattr(chat, "_client", lambda _ctx: called.append("built"))
    ctx = _ctx()
    assert handlers._REGISTRY["chat"](ctx) is None
    assert called == []
    assert ctx.provider.replies == [ctx.t("chat.budget")]
    assert "\n" not in ctx.provider.replies[0]


def test_a_budget_that_cannot_be_read_is_not_reported_as_used_up(monkeypatch):
    monkeypatch.setattr(chat, "enforce", _raises(BudgetUnavailable("ws-a", "db down")))
    ctx = _ctx()
    assert handlers._REGISTRY["chat"](ctx) == "failed"
    assert ctx.provider.replies == [ctx.t("chat.failed")]


def test_the_chat_surface_is_its_own_and_not_the_review_surface():
    assert SURFACE_PR_CHAT == "pr_chat"
    assert SURFACE_PR_CHAT != SURFACE_REVIEW


def test_the_client_is_built_to_book_spend_under_pr_chat(monkeypatch):
    seen: dict = {}

    def build(*args, **kwargs):
        seen.update(kwargs)
        seen["args"] = args
        return SimpleNamespace(generate=lambda **kw: SimpleNamespace(text="An answer."))

    monkeypatch.setattr(llm_client, "build_llm_client", build)
    monkeypatch.setattr(chat, "enforce", lambda _ws: None)
    _no_memories(monkeypatch)
    ctx = _ctx()
    handlers._REGISTRY["chat"](ctx)
    assert seen["spend_surface"] == SURFACE_PR_CHAT
    assert seen["surface"] == "review"          # which model route: the review one
    assert seen["args"] == ("u-1", "ws-a")      # whose key and budget
    assert ctx.provider.replies == ["An answer."]


def test_the_model_is_told_to_give_up_after_the_configured_wait_and_not_retry(monkeypatch):
    calls: list[dict] = []
    fake = SimpleNamespace(generate=lambda **kw: calls.append(kw) or SimpleNamespace(text="ok"))
    monkeypatch.setattr(chat, "_client", lambda _ctx: fake)
    monkeypatch.setattr(chat, "enforce", lambda _ws: None)
    _no_memories(monkeypatch)
    handlers._REGISTRY["chat"](_ctx())
    assert calls[0]["num_retries"] == 0
    assert calls[0]["timeout"] == 90


def test_a_huge_diff_and_thread_are_cut_to_the_context_budget(monkeypatch):
    big = "x" * 200_000
    pr = PullRequest(
        provider="github", repo="acme/shop", number=7, title="t", description=big, author="a",
        base_ref="develop", base_sha="b", head_ref="f", head_sha="h", state="open",
        hunks=[Hunk(file_path=f"src/f{n}.py", old_file_path=f"src/f{n}.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@ -1 +1 @@\n+" + big)
               for n in range(5)])
    provider = FakeProvider()
    provider.fetch_pull_request = lambda repo, n: pr
    provider.get_thread = lambda ev, limit=30: [
        chat.ThreadMessage(str(i), "alice", big) for i in range(30)]
    inp = chat._gather(_ctx(provider=provider))
    prompt, code = chat.build_prompt(inp, 20_000)
    assert len(prompt) + len(code) <= 20_000 + 2_000   # the fixed frame is not counted


def test_a_long_answer_is_cut_at_the_reply_limit():
    cut = chat.clean_answer("word " * 5000, limit=500)
    assert len(cut) <= 510 and cut.endswith("…")


def test_a_cut_never_leaves_a_code_fence_open():
    text = "Look:\n```python\n" + "x = 1\n" * 200 + "```\nDone."
    cut = chat.clean_answer(text, limit=300)
    assert cut.count("```") % 2 == 0
