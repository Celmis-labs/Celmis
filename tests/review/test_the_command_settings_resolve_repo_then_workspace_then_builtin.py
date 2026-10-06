"""`command_settings_for_repo`: the three command settings as in force.

`settings_for_repo` already answers repo > workspace (None when neither layer
says anything); what is pinned here is the last step, the built-in, and that
an unreadable database is the permissive built-in rather than an error — a
command is never lost to a settings failure.
"""

from __future__ import annotations

import pytest

from src.review import review_defaults
from src.review.review_defaults import COMMAND_SETTINGS, command_settings_for_repo


def _stub(monkeypatch, answer: dict) -> None:
    monkeypatch.setattr(
        review_defaults, "settings_for_repo",
        lambda provider, full_name, names: {n: answer.get(n) for n in names},
    )


def test_a_repository_that_says_nothing_gets_the_builtins(monkeypatch):
    _stub(monkeypatch, {})
    assert command_settings_for_repo("github", "acme/shop") == {
        "commands_enabled": True, "chat_enabled": True, "command_permission": "repo_access",
    }


def test_a_stored_false_is_kept_and_not_replaced_by_the_builtin(monkeypatch):
    _stub(monkeypatch, {"commands_enabled": False, "command_permission": "anyone"})
    got = command_settings_for_repo("github", "acme/shop")
    assert got["commands_enabled"] is False
    assert got["command_permission"] == "anyone"
    assert got["chat_enabled"] is True


def test_the_three_command_settings_are_the_ones_the_handlers_read():
    assert set(COMMAND_SETTINGS) == {"commands_enabled", "chat_enabled", "command_permission"}


@pytest.mark.parametrize("value", ["repo_access", "participants", "anyone"])
def test_every_permission_mode_is_an_allowed_choice(value):
    assert value in review_defaults.SETTING_CHOICES["command_permission"]


def test_an_unreadable_database_is_the_permissive_builtin(monkeypatch):
    import src.api.auto_review as auto_review

    def boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(auto_review, "get_auto_review_store", boom)
    assert command_settings_for_repo("github", "acme/shop")["commands_enabled"] is True
