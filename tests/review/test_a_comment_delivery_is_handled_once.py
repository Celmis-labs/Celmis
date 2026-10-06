"""A comment is claimed in the ledger once: a redelivery, a retry or an edit
of a comment that was already handled runs nothing a second time."""

from __future__ import annotations

import pytest

from src.review.commands import ledger
from src.review.commands.handlers import accept
from tests.review.comment_support import command, event, ledger_engine  # noqa: F401

SETTINGS = {"commands_enabled": True, "chat_enabled": True,
            "command_permission": "repo_access"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr("src.review.review_defaults.command_settings_for_repo",
                        lambda provider, repo: dict(SETTINGS))


def _accept(ledger_engine, **over):  # noqa: F811
    return accept(event(**over), command(), workspace_id="ws", user_id="u",
                  engine=ledger_engine)


def test_the_second_delivery_of_a_comment_is_dropped(ledger_engine):  # noqa: F811
    first = _accept(ledger_engine)
    assert first is not None and first["ledger_id"]
    assert _accept(ledger_engine) is None


def test_an_edit_of_a_handled_comment_is_dropped(ledger_engine):  # noqa: F811
    assert _accept(ledger_engine) is not None
    assert _accept(ledger_engine, edited=True) is None


def test_a_comment_edited_into_a_command_counts_the_first_time(ledger_engine):  # noqa: F811
    assert _accept(ledger_engine, comment_id="c9", edited=True) is not None


def test_the_same_comment_id_in_another_workspace_is_another_comment(ledger_engine):  # noqa: F811
    assert _accept(ledger_engine) is not None
    other = accept(event(), command(), workspace_id="ws-2", user_id="u",
                   engine=ledger_engine)
    assert other is not None


def test_the_claim_survives_in_the_table_not_in_memory(ledger_engine):  # noqa: F811
    ledger.claim("ws", "github", "acme/shop", 7, "c1", command="review",
                 engine=ledger_engine)
    assert ledger.claim("ws", "github", "acme/shop", 7, "c1", command="review",
                        engine=ledger_engine) is None


def test_the_payload_carries_everything_the_worker_needs(ledger_engine):  # noqa: F811
    payload = _accept(ledger_engine)
    assert payload["event"]["comment_id"] == "c1"
    assert payload["event"]["actor_ids"] == ["101", "alice"]
    assert payload["command"]["name"] == "start-review"
    assert (payload["workspace_id"], payload["user_id"]) == ("ws", "u")
    assert payload["settings"]["command_permission"] == "repo_access"


def test_a_switched_off_repository_records_nothing(ledger_engine, monkeypatch):  # noqa: F811
    monkeypatch.setitem(SETTINGS, "commands_enabled", False)
    assert _accept(ledger_engine) is None
    assert ledger.for_pr("ws", "github", "acme/shop", 7, engine=ledger_engine) == []


def test_a_question_is_ignored_when_chat_is_switched_off(ledger_engine, monkeypatch):  # noqa: F811
    monkeypatch.setitem(SETTINGS, "chat_enabled", False)
    assert accept(event(), command("chat", args="why?"), workspace_id="ws",
                  user_id="u", engine=ledger_engine) is None
    assert _accept(ledger_engine) is not None  # a command still works


def test_the_ledger_finishes_a_row_and_lists_it_for_the_timeline(ledger_engine):  # noqa: F811
    payload = _accept(ledger_engine)
    ledger.finish(payload["ledger_id"], ledger.DONE, run_id="run-1",
                  engine=ledger_engine)
    [row] = ledger.for_pr("ws", "github", "acme/shop", 7, engine=ledger_engine)
    assert (row["command"], row["status"], row["run_id"], row["actor_name"]) == (
        "start-review", "done", "run-1", "alice")
    assert row["finished_at"] is not None


def test_a_chat_mention_is_not_answered_while_nothing_can_chat(ledger_engine, monkeypatch):  # noqa: F811
    from src.review.commands import handlers
    from src.review.commands.parser import ParsedCommand

    monkeypatch.delitem(handlers._REGISTRY, "chat")
    thanks = ParsedCommand("chat", args="thanks")
    assert accept(event(body="thanks @celmis"), thanks, workspace_id="ws", user_id="u",
                  engine=ledger_engine) is None
    assert ledger.for_pr("ws", "github", "acme/shop", 7, engine=ledger_engine) == []


def test_a_chat_mention_is_taken_once_chat_is_registered(ledger_engine, monkeypatch):  # noqa: F811
    from src.review.commands import handlers
    from src.review.commands.parser import ParsedCommand

    monkeypatch.setitem(handlers._REGISTRY, "chat", lambda ctx: None)
    assert accept(event(), ParsedCommand("chat", args="hi"), workspace_id="ws",
                  user_id="u", engine=ledger_engine) is not None
