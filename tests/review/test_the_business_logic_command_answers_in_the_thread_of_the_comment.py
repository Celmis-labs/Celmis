"""The `business-logic` command handler: acknowledge, run, answer in the thread.

The dispatcher itself lives in another lane (and in the integrated tree); the
handler is a function of its command context, so it is tested with a fake one.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from src.review.task_context import command, on_demand
from src.review.task_context.jira_client import JiraError
from tests.review.jira_fakes import FakeJira, page


class _Provider:
    def __init__(self, *, edits: bool = True) -> None:
        self.edits, self.updated = edits, []

    def fetch_pull_request(self, repo, number):
        return "the-pr"

    def update_comment(self, repo, number, comment_id, text, kind=None):
        self.updated.append((comment_id, text))
        return self.edits


class _Ctx:
    def __init__(self, args="PROJ-1", *, note=None, provider=None) -> None:
        self.ev = SimpleNamespace(repo="acme/shop", pr_number=31, kind="issue_comment")
        self.command = SimpleNamespace(args=args)
        self.provider = provider or _Provider()
        self.workspace_id, self.user_id, self.language = "ws-a", "u-1", "en"
        self.ack_comment_id = note
        self.handle = "@celmis"
        self.replies: list[str] = []
        self.acks: list[str] = []

    def acknowledge(self, key):
        self.acks.append(key)

    def reply(self, text):
        self.replies.append(text)


@pytest.fixture
def seen(monkeypatch):
    calls: dict = {}

    def fake_check(pr, spec, **kw):
        calls.update(pr=pr, spec=spec, **kw)
        return SimpleNamespace(markdown="## Business-logic check\n\nanswer")

    monkeypatch.setattr(on_demand, "run_business_logic_check", fake_check)
    monkeypatch.setattr("src.review.orchestrator.ReviewOrchestrator.policy_for",
                        lambda self, pr, ws: {"enabled": True, "ws": ws})
    return calls


def test_the_answer_is_a_reply_in_the_thread_and_the_command_acknowledges(seen):
    ctx = _Ctx("PROJ-1")
    assert command.business_logic_command(ctx) is None
    assert ctx.acks == ["command.ack_business_logic"]
    assert ctx.replies == ["## Business-logic check\n\nanswer"]


def test_the_answer_cannot_ping_anyone_or_carry_a_marker(monkeypatch, seen):
    risky = "Ask @someone and @celmis <!-- celmis:review --> ![x](https://evil.example.com/p.png)"
    monkeypatch.setattr(on_demand, "run_business_logic_check",
                        lambda *a, **k: SimpleNamespace(markdown=risky))
    ctx = _Ctx("PROJ-1")
    command.business_logic_command(ctx)
    (text,) = ctx.replies
    assert "`@someone`" in text and "`@celmis`" in text
    assert "<!--" not in text and "evil.example.com" not in text


def test_what_was_typed_after_the_command_reaches_the_check_untouched(seen):
    command.business_logic_command(_Ctx("https://celmis.example.com/browse/PROJ-1"))
    assert seen["spec"] == "https://celmis.example.com/browse/PROJ-1"
    assert seen["pr"] == "the-pr" and seen["workspace_id"] == "ws-a" and seen["user_id"] == "u-1"
    assert seen["policy"] == {"enabled": True, "ws": "ws-a"} and seen["language"] == "en"


def test_without_arguments_the_check_gets_an_empty_spec(seen):
    command.business_logic_command(_Ctx(None))
    assert seen["spec"] == ""


def test_a_note_left_for_a_provider_without_reactions_becomes_the_answer(seen):
    ctx = _Ctx(note="note-9")
    command.business_logic_command(ctx)
    assert ctx.provider.updated == [("note-9", "## Business-logic check\n\nanswer")]
    assert ctx.replies == []


def test_a_note_that_cannot_be_edited_falls_back_to_a_reply(seen):
    ctx = _Ctx(note="note-9", provider=_Provider(edits=False))
    command.business_logic_command(ctx)
    assert ctx.replies == ["## Business-logic check\n\nanswer"]


def test_a_note_whose_edit_raises_falls_back_to_a_reply(seen):
    class Boom(_Provider):
        def update_comment(self, *a, **k):
            raise RuntimeError("403 with a token inside")

    ctx = _Ctx(note="note-9", provider=Boom())
    command.business_logic_command(ctx)
    assert ctx.replies == ["## Business-logic check\n\nanswer"]


@pytest.mark.skipif(importlib.util.find_spec("src.review.commands") is None,
                    reason="the command dispatcher is not part of this tree")
def test_registering_adds_the_command_to_the_registry():
    from src.review.commands import handlers

    command.register()
    command.register()
    assert command.NAME in getattr(handlers, "COMMANDS", getattr(handlers, "_REGISTRY", {}))


# ─── JiraClient.get_page ─────────────────────────────────────────────


@pytest.fixture
def client():
    fake = FakeJira()
    fake.pages["4242"] = page("4242")
    return fake.client(), fake


def test_a_page_is_fetched_from_the_connected_site_in_adf(client):
    jc, fake = client
    data = jc.get_page("4242")
    assert data["title"] == "Cutting spec" and fake.count("/wiki/pages/4242") == 1


@pytest.mark.parametrize("bad", ["", "abc", "42/../1", "4242?x=1", "1" * 16])
def test_anything_but_a_numeric_id_never_reaches_the_network(client, bad):
    jc, fake = client
    with pytest.raises(JiraError):
        jc.get_page(bad)
    assert fake.calls == []


def test_a_page_the_site_does_not_have_is_a_curated_not_found(client):
    jc, _ = client
    with pytest.raises(JiraError) as err:
        jc.get_page("9999")
    assert err.value.kind == "not_found" and "page" in str(err.value)
