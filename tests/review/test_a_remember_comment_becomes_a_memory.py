"""`@celmis remember ...` in a pull request comment, end to end up to the store.

What is checked, by behaviour: the command is registered (so the guide and
`help` list it), the scope flags decide the memory's place, a stranger's rule
waits for approval and says so, a trusted one is active, and the answer is
posted in the comment's thread.
"""

from __future__ import annotations

from src.db.models import RepoReviewPolicy
from src.review import memories
from src.review.commands import handlers
from src.review.commands.handlers import CommandContext
from src.review.commands.parser import help_markdown, parse_comment
from tests.review.comment_support import FakeProvider, event
from tests.review.rules_db import rules_db

WS = "ws-a"
SLUG = "github_acme-shop"


def _ctx(body: str, provider=None, **ev_over) -> CommandContext:
    ev = event(body=body, **ev_over)
    parsed = parse_comment(body, "@celmis")
    assert parsed is not None, body
    return CommandContext(
        ev=ev, provider=provider or FakeProvider(), command=parsed,
        settings={}, workspace_id=WS, user_id="u")


def test_remember_is_a_registered_command_and_the_guide_lists_it():
    assert "remember" in handlers.available_commands()
    guide = help_markdown("@celmis", "en", available=handlers.available_commands())
    assert "remember" in guide


async def test_a_stranger_rule_waits_for_approval_and_the_thread_hears_it(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        ctx = _ctx("@celmis remember: money is integer cents")
        handlers.remember_command(ctx)
        assert ctx.provider.replies and "approves it" in ctx.provider.replies[0]
        assert memories.load_active_sync(WS, SLUG) == []


async def test_a_trusted_commenter_rule_is_active_at_once(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch) as factory:
        async with factory() as s:
            s.add(RepoReviewPolicy(repo_slug=SLUG, workspace_id=WS,
                                   memory_trusted_commenters=["alice"]))
            await s.commit()
        ctx = _ctx("@celmis remember: money is integer cents")
        handlers.remember_command(ctx)
        assert "Remembered for this repository" in ctx.provider.replies[0]
        assert [m["text"] for m in memories.load_active_sync(WS, SLUG)] == [
            "money is integer cents"]


async def test_org_scope_makes_a_workspace_memory(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(workspace_id, text, **kw):
        seen.update(kw, text=text)
        return memories.MemoryWrite(action="create", memory={"id": "m1"})

    monkeypatch.setattr(memories, "remember", fake)
    ctx = _ctx("@celmis remember --org: every service logs JSON")
    handlers.remember_command(ctx)
    assert seen["repo_slug"] is None and seen["path_glob"] is None
    assert seen["origin"] == "command"
    assert "every repository of the workspace" in ctx.provider.replies[0]


def test_dir_scope_takes_the_flag_path_or_the_directory_of_the_commented_file(monkeypatch):
    seen: list[dict] = []

    def fake(workspace_id, text, **kw):
        seen.append(kw)
        return memories.MemoryWrite(action="pending", memory={"id": "m1"}, reason="waits")

    monkeypatch.setattr(memories, "remember", fake)
    handlers.remember_command(_ctx("@celmis remember --dir=src/api: keep handlers thin"))
    handlers.remember_command(_ctx("@celmis remember --dir: keep handlers thin",
                                   kind="inline", path="web/lib/api.ts", line=3))
    assert [(k["repo_slug"], k["path_glob"]) for k in seen] == [
        (SLUG, "src/api"), (SLUG, "web/lib")]


def test_dir_scope_without_any_directory_asks_for_one(monkeypatch):
    monkeypatch.setattr(memories, "remember", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("nothing may be stored without a place")))
    ctx = _ctx("@celmis remember --dir: keep handlers thin")
    handlers.remember_command(ctx)
    assert "--dir" in ctx.provider.replies[0]


def test_only_the_tokens_own_account_counts_as_the_token_owner(monkeypatch):
    seen: list[memories.ActorRef] = []

    def fake(workspace_id, text, **kw):
        seen.append(kw["actor"])
        return memories.MemoryWrite(action="skip", reason="already known")

    monkeypatch.setattr(memories, "remember", fake)
    handlers.remember_command(_ctx("@celmis remember: a rule here"))
    handlers.remember_command(
        _ctx("@celmis remember: a rule here", FakeProvider(viewer={"101"})))
    assert [a.is_token_owner for a in seen] == [False, True]
    assert seen[0].external_id == "101"


def test_a_refused_rule_says_why(monkeypatch):
    monkeypatch.setattr(memories, "remember", lambda *a, **k: memories.MemoryWrite(
        action="skip", reason="the team already knows this"))
    ctx = _ctx("@celmis remember: a rule here")
    handlers.remember_command(ctx)
    assert ctx.provider.replies == ["Nothing was saved: the team already knows this"]
