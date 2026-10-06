"""The bot's own words, and another bot's, never start a command.

Decided by the bot marker in the text (and the author's type), so that a reply carrying
a command-looking sentence (the help text itself) cannot loop.
"""

from __future__ import annotations

from src.review import markers
from src.review.commands import gate
from src.review.commands.handlers import CommandContext, _run
from src.review.commands.parser import help_markdown
from src.review.settings import ReviewSettings
from src.review.webhook import _command_candidate
from tests.review.comment_support import FakeProvider, command, event


def test_the_help_text_does_not_trigger_the_help():
    text = markers.with_chat_marker(help_markdown("@celmis", "en"))
    assert gate.own_text_reason(event(body=text)) == "carries a bot marker"
    assert _command_candidate(event(body=text), ReviewSettings()) is None


def test_a_hidden_marker_is_recognised_too():
    hidden = markers.hide(markers.with_chat_marker("@celmis start-review"), "refdef")
    assert "<!--" not in hidden
    assert gate.own_text_reason(event(body=hidden)) == "carries a bot marker"


def test_a_person_quoting_us_is_still_a_person():
    quoted = "> <!-- celmis:chat:v1 -->\n> sure\n\n@celmis start-review"
    assert gate.own_text_reason(event(body=quoted)) == ""


def test_a_bot_author_is_ignored_whatever_it_says():
    assert gate.own_text_reason(event(actor_is_bot=True)) == "author is a bot"
    assert _command_candidate(event(actor_is_bot=True), ReviewSettings()) is None


def test_the_token_owner_writing_without_a_marker_is_a_person():
    ev = event(actor_id="celmis-bot", actor_ids=("celmis-bot",))
    assert gate.own_text_reason(ev) == ""


def test_the_token_owner_writing_with_a_marker_is_us():
    ev = event(actor_id="celmis-bot", actor_ids=("celmis-bot",),
               body=markers.with_chat_marker("@celmis start-review"))
    assert gate.own_text_reason(ev) == "carries a bot marker"


def test_the_worker_obeys_the_token_owner_who_commands_without_a_marker():
    provider = FakeProvider(viewer={"101"})
    ctx = CommandContext(ev=event(), provider=provider, command=command(), settings={},
                         workspace_id="ws", user_id="u")
    assert _run(ctx, {"rate_limited": True}) == "rate_limited"


def test_the_worker_stops_on_a_marker_even_when_the_receiver_did_not():
    provider = FakeProvider(viewer={"101"})
    ev = event(body=markers.with_chat_marker("@celmis start-review"))
    ctx = CommandContext(ev=ev, provider=provider, command=command(), settings={},
                         workspace_id="ws", user_id="u")
    assert _run(ctx, {}) == "ignored"
    assert provider.replies == []


def test_a_bot_is_not_told_it_is_rate_limited():
    provider = FakeProvider()
    ctx = CommandContext(
        ev=event(actor_is_bot=True), provider=provider, command=command(),
        settings={"command_permission": "repo_access"}, workspace_id="ws", user_id="u")
    assert _run(ctx, {"rate_limited": True}) == "ignored"
    assert provider.replies == []
