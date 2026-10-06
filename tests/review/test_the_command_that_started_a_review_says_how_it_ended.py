"""`finish_ack`: what the comment that asked for a review hears afterwards,
and `@celmis help` listing only what this installation can run."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from src.review import messages
from src.review.commands import handlers
from src.review.commands.handlers import CommandContext, available_commands, finish_ack
from src.review.models import ReviewVerdict
from tests.review.comment_support import FakeProvider, command, event


def _payload(ack_comment_id=None, language="en"):
    ev = event()
    return {"command_ack": {
        "event": asdict(ev) | {"actor_ids": list(ev.actor_ids)},
        "ack_comment_id": ack_comment_id, "language": language,
    }}


def _result(verdict, summary=""):
    return SimpleNamespace(batch=SimpleNamespace(verdict=verdict, summary=summary))


def test_a_review_that_ran_on_a_provider_with_reactions_adds_nothing():
    provider = FakeProvider()
    finish_ack(_payload(), provider, result=_result(ReviewVerdict.APPROVE))
    assert provider.replies == [] and provider.updates == []


def test_a_review_that_ran_edits_the_working_note_to_point_at_the_summary():
    provider = FakeProvider()
    finish_ack(_payload(ack_comment_id="77"), provider, result=_result(ReviewVerdict.APPROVE))
    assert provider.updates == [("77", messages.t("command.done", "en"))]
    assert provider.replies == []


def test_a_review_that_a_gate_skipped_says_why_in_the_thread():
    provider = FakeProvider()
    finish_ack(
        _payload(), provider,
        result=_result(ReviewVerdict.SKIPPED, "Skipped — Branch mismatch: target 'x'"),
    )
    assert len(provider.replies) == 1
    assert "Branch mismatch: target 'x'" in provider.replies[0]


def test_a_failed_review_is_explained_in_the_thread():
    provider = FakeProvider()
    finish_ack(_payload(), provider, failed=True)
    assert provider.replies == [messages.t("command.failed", "en")]


def test_a_failed_edit_of_the_note_falls_back_to_a_new_reply():
    class Refuses(FakeProvider):
        def update_comment(self, *a, **k):
            return False

    provider = Refuses()
    finish_ack(_payload(ack_comment_id="77"), provider, result=_result(ReviewVerdict.APPROVE))
    assert provider.replies == [messages.t("command.done", "en")]


def test_a_review_that_was_not_started_by_a_comment_is_left_alone():
    provider = FakeProvider()
    finish_ack({}, provider, result=_result(ReviewVerdict.APPROVE))
    finish_ack({"command_ack": "junk"}, provider, failed=True)
    assert provider.replies == [] and provider.updates == []


def test_the_answer_is_written_in_the_reviews_language():
    provider = FakeProvider()
    finish_ack(_payload(language="uk"), provider, failed=True)
    assert provider.replies == [messages.t("command.failed", "uk")]
    assert provider.replies[0] != messages.t("command.failed", "en")


def test_help_lists_the_registered_commands_and_not_the_missing_ones():
    assert set(available_commands()) >= {"start-review", "review", "help"}
    provider = FakeProvider()
    ctx = CommandContext(
        ev=event(body="@celmis help"), provider=provider, command=command("help"),
        settings={}, workspace_id="ws", user_id="u",
    )
    handlers._help(ctx)
    text = provider.replies[0]
    assert "start-review" in text
    if "remember" not in available_commands():
        assert "remember" not in text
    if "business-logic" not in available_commands():
        assert "business-logic" not in text


def test_help_lists_a_command_the_moment_it_is_registered(monkeypatch):
    monkeypatch.setitem(handlers._REGISTRY, "remember", lambda ctx: None)
    provider = FakeProvider()
    ctx = CommandContext(
        ev=event(body="@celmis help"), provider=provider, command=command("help"),
        settings={}, workspace_id="ws", user_id="u",
    )
    handlers._help(ctx)
    assert "remember" in provider.replies[0]
