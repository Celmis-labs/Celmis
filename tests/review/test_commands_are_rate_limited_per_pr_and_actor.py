"""Commands are counted from the ledger: per pull request and per person.

Over the limit a command runs nothing; the first refusal of a window is
announced once, every later one is only written down.
"""

from __future__ import annotations

import pytest

from src.review.commands import ledger
from src.review.commands.handlers import accept
from tests.review.comment_support import command, event, ledger_engine  # noqa: F401

SETTINGS = {"commands_enabled": True, "chat_enabled": True,
            "command_permission": "repo_access"}


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr("src.review.review_defaults.command_settings_for_repo",
                        lambda provider, repo: dict(SETTINGS))
    from src.review import settings as settings_mod

    real = settings_mod.get_review_settings()
    limited = real.model_copy(update={"command_replies_per_pr_per_hour": 3,
                                      "commands_per_actor_per_hour": 2})
    monkeypatch.setattr(settings_mod, "get_review_settings", lambda: limited)


def _give(ledger_engine, n, **over):  # noqa: F811
    return accept(event(comment_id=f"c{n}", **over), command(), workspace_id="ws",
                  user_id="u", engine=ledger_engine)


def test_a_person_may_give_a_few_commands_an_hour(ledger_engine):  # noqa: F811
    assert _give(ledger_engine, 1) is not None
    assert _give(ledger_engine, 2) is not None
    over = _give(ledger_engine, 3)
    assert over is not None and over["rate_limited"] is True  # said once


def test_the_refusal_is_announced_once_per_window(ledger_engine):  # noqa: F811
    _give(ledger_engine, 1)
    _give(ledger_engine, 2)
    assert _give(ledger_engine, 3)["rate_limited"] is True
    assert _give(ledger_engine, 4) is None  # recorded, not answered
    rows = ledger.for_pr("ws", "github", "acme/shop", 7, engine=ledger_engine)
    assert sorted(r["status"] for r in rows) == [
        "claimed", "claimed", "rate_limited", "rate_limited"]


def test_another_person_is_not_held_back_by_the_first(ledger_engine):  # noqa: F811
    _give(ledger_engine, 1)
    _give(ledger_engine, 2)
    other = _give(ledger_engine, 3, actor_id="202", actor_name="bob",
                  actor_ids=("202",))
    assert other is not None and other["rate_limited"] is False


def test_one_pull_request_cannot_be_flooded_by_many_people(ledger_engine):  # noqa: F811
    for n, who in enumerate(("a", "b", "c"), start=1):
        assert _give(ledger_engine, n, actor_id=who, actor_ids=(who,))["rate_limited"] is False
    assert _give(ledger_engine, 4, actor_id="d", actor_ids=("d",))["rate_limited"] is True


def test_another_pull_request_has_its_own_budget(ledger_engine):  # noqa: F811
    for n, who in enumerate(("a", "b", "c"), start=1):
        _give(ledger_engine, n, actor_id=who, actor_ids=(who,))
    assert _give(ledger_engine, 9, pr_number=8, actor_id="d",
                 actor_ids=("d",))["rate_limited"] is False


def test_a_strangers_refused_commands_do_not_use_up_the_pull_requests_budget(
        ledger_engine):  # noqa: F811
    for n in range(1, 6):
        ledger.claim("ws", "github", "acme/shop", 7, f"s{n}", command="start-review",
                     actor_id=f"x{n}", status=ledger.DENIED, engine=ledger_engine)
    for n, who in enumerate(("a", "b", "c"), start=1):
        assert _give(ledger_engine, n, actor_id=who,
                     actor_ids=(who,))["rate_limited"] is False


def test_the_same_number_on_another_provider_is_another_person(ledger_engine):  # noqa: F811
    _give(ledger_engine, 1, provider="github", actor_id="5", actor_ids=("5",))
    _give(ledger_engine, 2, provider="github", actor_id="5", actor_ids=("5",), pr_number=8)
    other = _give(ledger_engine, 3, provider="gitlab", actor_id="5", actor_ids=("5",),
                  pr_number=9)
    assert other is not None and other["rate_limited"] is False


def _force(ledger_engine, n, who):  # noqa: F811
    return accept(event(comment_id=f"f{n}", actor_id=who, actor_ids=(who,)),
                  command(name="review", force=True), workspace_id="ws", user_id="u",
                  engine=ledger_engine)


def test_a_pull_request_gets_only_a_few_forced_reviews_an_hour(
        ledger_engine, monkeypatch):  # noqa: F811
    from src.review import settings as settings_mod

    roomy = settings_mod.get_review_settings().model_copy(
        update={"command_replies_per_pr_per_hour": 50, "commands_per_actor_per_hour": 50})
    monkeypatch.setattr(settings_mod, "get_review_settings", lambda: roomy)
    from src.review.commands.handlers import FORCED_PER_PR_PER_HOUR

    for n in range(FORCED_PER_PR_PER_HOUR):
        assert _force(ledger_engine, n, f"p{n}")["rate_limited"] is False
    assert _force(ledger_engine, 99, "late")["rate_limited"] is True
    # A plain review is not held back by the forced budget.
    assert accept(event(comment_id="plain", actor_id="q", actor_ids=("q",)), command(),
                  workspace_id="ws", user_id="u", engine=ledger_engine
                  )["rate_limited"] is False
