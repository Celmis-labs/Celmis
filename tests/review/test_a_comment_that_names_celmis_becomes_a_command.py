"""What a pull-request comment asks of the bot is read from its text alone.

A mention must start a token; code, quotes and look-alike names never count;
a command word with anything but flags after it is a question, not a command.
"""

from __future__ import annotations

import pytest

from src.review.commands.parser import (
    BUSINESS_LOGIC,
    CHAT,
    HELP,
    REMEMBER,
    REVIEW,
    START_REVIEW,
    ParsedCommand,
    guide_lines,
    help_markdown,
    might_address_bot,
    parse_comment,
)

HANDLE = "@celmis"


def _name(text: str, handle: str = HANDLE):
    found = parse_comment(text, handle)
    return found.name if found else None


@pytest.mark.parametrize("text, expected", [
    ("@celmis", HELP),
    ("@celmis help", HELP),
    ("@celmis help!", HELP),
    ("@celmis start-review", START_REVIEW),
    ("@celmis start", START_REVIEW),
    ("@celmis review", REVIEW),
    ("please @celmis review once more", CHAT),
    ("Hi team,\n@celmis start-review", START_REVIEW),
    ("@CELMIS Start-Review", START_REVIEW),
    ("@celmis: start-review", START_REVIEW),
    ("/celmis review", REVIEW),
    ("@celmis -v business-logic PROJ-123", BUSINESS_LOGIC),
    ("@celmis remember: use the money type", REMEMBER),
    ("@celmis why does this loop never end?", CHAT),
    ("@celmis\nwhy does this loop never end?", CHAT),
])
def test_a_mention_picks_the_command_it_names(text, expected):
    assert _name(text) == expected


@pytest.mark.parametrize("text", [
    "no mention here",
    "> @celmis start-review",
    "> quoted\n> @celmis review --force",
    "run `@celmis start-review` to start",
    "```\n@celmis start-review\n```",
    "~~~\n@celmis help\n~~~",
    "write to ops@celmis.example.com",
    "@celmis-bot start-review",
    "@celmisfan review",
    "@celmis.io review",
    "see https://example.com/celmis review",
    "src/celmis review",
    "",
    "   ",
])
def test_text_that_only_looks_like_a_mention_is_not_one(text):
    assert parse_comment(text, HANDLE) is None


def test_a_mention_after_a_closed_code_block_still_counts():
    assert _name("```\n@celmis help\n```\nnow @celmis start-review") == START_REVIEW


@pytest.mark.parametrize("text, force", [
    ("@celmis review --force", True),
    ("@celmis review -f", True),
    ("@celmis review --force.", True),
    ("@celmis start-review --force", True),
    ("@celmis review", False),
    ("@celmis start-review", False),
])
def test_force_is_a_flag_not_a_word_in_a_sentence(text, force):
    found = parse_comment(text, HANDLE)
    assert found is not None and found.name in (START_REVIEW, REVIEW)
    assert found.force is force


@pytest.mark.parametrize("text", [
    "@celmis review this function for races",
    "@celmis review --force the whole thing",
    "@celmis start-review now please",
    "@celmis help me understand this",
])
def test_a_command_word_followed_by_prose_is_a_question(text):
    found = parse_comment(text, HANDLE)
    assert found is not None and found.name == CHAT
    assert found.args == text.split(" ", 1)[1]


def test_remember_carries_its_scope_and_the_rule():
    assert parse_comment("@celmis remember: use the money type", HANDLE) == ParsedCommand(
        REMEMBER, args="use the money type", scope="repo")
    assert parse_comment("@celmis remember --org: every API is versioned", HANDLE) == (
        ParsedCommand(REMEMBER, args="every API is versioned", scope="org"))
    found = parse_comment("@celmis remember --dir=src/api: handlers are async", HANDLE)
    assert (found.scope, found.path, found.args) == ("dir", "src/api", "handlers are async")


def test_business_logic_names_its_ticket():
    found = parse_comment("@celmis -v business-logic proj-123", HANDLE)
    assert found == ParsedCommand(BUSINESS_LOGIC, args="PROJ-123")


def test_business_logic_accepts_a_link_and_keeps_its_case():
    link = "https://celmis.example.com/browse/PROJ-123"
    found = parse_comment(f"@celmis -v business-logic {link}", HANDLE)
    assert found == ParsedCommand(BUSINESS_LOGIC, args=link)
    wiki = "https://celmis.example.com/wiki/spaces/Docs/pages/42/Plan"
    assert parse_comment(f"@celmis -v business-logic <{wiki}>", HANDLE).args == wiki


def test_business_logic_with_other_words_after_the_task_is_a_question():
    assert _name("@celmis -v business-logic PROJ-123 and the other one") == CHAT
    assert _name("@celmis -v business-logic http://celmis.example.com/x") == CHAT


def test_the_business_logic_command_is_registered_with_the_dispatcher():
    from src.review.commands import handlers

    assert BUSINESS_LOGIC in handlers.available_commands()


def test_the_handle_follows_the_install_setting():
    assert _name("@reviewbot review", "@reviewbot") == REVIEW
    assert _name("/reviewbot review", "@reviewbot") == REVIEW
    assert _name("@celmis review", "@reviewbot") is None
    assert _name("/celmis review", "/celmis") == REVIEW


def test_the_prefilter_never_misses_a_command():
    assert might_address_bot("hello @Celmis", HANDLE)
    assert not might_address_bot("hello world", HANDLE)


def test_a_question_is_cut_to_a_sane_length():
    found = parse_comment("@celmis " + "why " * 5000, HANDLE)
    assert found.name == CHAT and len(found.args) <= 4000


def test_the_help_lists_only_the_commands_that_are_registered():
    text = help_markdown(HANDLE, "en", available=[START_REVIEW, REVIEW, HELP])
    assert "`@celmis start-review`" in text and "`@celmis review --force`" in text
    assert "remember" not in text and "business-logic" not in text
    assert "Mention" not in text  # no chat registered: no invitation to ask


def test_the_guide_and_the_help_speak_the_repository_language():
    assert "Команди" in "\n".join(guide_lines(HANDLE, "uk", available=[HELP]))
    assert "Як говорити" in help_markdown(HANDLE, "uk", available=[HELP])
