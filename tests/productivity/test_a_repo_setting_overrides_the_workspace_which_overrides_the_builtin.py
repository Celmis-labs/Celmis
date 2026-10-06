"""Productivity settings: layering, validation, and a stored value the rules no longer accept."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from src.db.models import ProductivityRepoSettings
from src.productivity import settings as sm
from tests.productivity.support import PROVIDER, REPO, WS, make_engine


def test_the_builtins_are_what_the_design_says() -> None:
    b = sm.BUILTIN
    assert (b.enabled, b.backfill_days, b.deploy_source, b.failure_window_days) == (False, 180, "merge", 7)
    assert (b.deploy_group_minutes, b.rate_per_hour) == (30, 500)
    assert b.production_branches == ("main", "master") and b.integration_branches == ()
    assert "<!-- celmis:" in b.bot_markers and "<!-- code-analyzer:" in b.bot_markers


def test_a_repository_says_something_the_workspace_says_something_else_and_the_rest_is_built_in() -> None:
    engine = make_engine()
    sm.save(WS, "", "", {"production_branches": ["release"], "failure_window_days": 14}, engine=engine)
    sm.save(WS, PROVIDER, REPO, {"production_branches": ["master"]}, engine=engine)
    got = sm.load(WS, PROVIDER, REPO, engine=engine)
    assert got.production_branches == ("master",)          # the repository wins
    assert got.failure_window_days == 14                   # the workspace fills in
    assert got.deploy_group_minutes == 30                  # the built-in is the floor
    other = sm.load(WS, PROVIDER, "acme/other", engine=engine)
    assert other.production_branches == ("release",)


def test_clearing_a_field_returns_it_to_inheriting() -> None:
    engine = make_engine()
    sm.save(WS, "", "", {"backfill_days": 90}, engine=engine)
    sm.save(WS, PROVIDER, REPO, {"backfill_days": 30}, engine=engine)
    assert sm.load(WS, PROVIDER, REPO, engine=engine).backfill_days == 30
    sm.save(WS, PROVIDER, REPO, {"backfill_days": None}, engine=engine)
    assert sm.load(WS, PROVIDER, REPO, engine=engine).backfill_days == 90


def test_the_layers_are_reported_so_a_page_can_say_where_each_value_came_from() -> None:
    engine = make_engine()
    sm.save(WS, "", "", {"failure_window_days": 14}, engine=engine)
    sm.save(WS, PROVIDER, REPO, {"enabled": True}, engine=engine)
    layers = sm.load_layers(WS, PROVIDER, REPO, engine=engine)
    assert layers["workspace"] == {"failure_window_days": 14}
    assert layers["repo"] == {"enabled": True}
    assert layers["resolved"]["enabled"] is True and layers["resolved"]["failure_window_days"] == 14
    assert layers["builtin"]["failure_window_days"] == 7


def test_the_index_resolves_every_repository_from_one_read() -> None:
    engine = make_engine()
    sm.save("ws-a", "", "", {"enabled": True}, engine=engine)
    sm.save("ws-b", "github", "o/r", {"enabled": True}, engine=engine)
    index = sm.load_index(engine)
    assert index.for_repo("ws-a", "github", "any/repo").enabled
    assert index.for_repo("ws-b", "github", "o/r").enabled
    assert not index.for_repo("ws-b", "github", "o/other").enabled
    assert not index.for_repo("ws-c", "github", "o/r").enabled


@pytest.mark.parametrize("name,value", [
    ("backfill_days", 0), ("backfill_days", 5000), ("backfill_days", "30"), ("backfill_days", True),
    ("rate_per_hour", 5), ("rate_per_hour", 5000), ("failure_window_days", 0),
    ("deploy_source", "ftp"), ("enabled", "yes"), ("revert_patterns", ["(["]), ("bugfix_patterns", "x" * 300),
    ("tag_pattern", "(["), ("nonsense", 1), ("ignored_authors", 5),
])
def test_a_value_the_rules_reject_is_refused_with_the_field_named(name: str, value) -> None:
    with pytest.raises(sm.SettingsError) as caught:
        sm.validate(name, value)
    assert name in str(caught.value)


def test_a_rejected_write_changes_nothing() -> None:
    engine = make_engine()
    sm.save(WS, PROVIDER, REPO, {"backfill_days": 30}, engine=engine)
    with pytest.raises(sm.SettingsError):
        sm.save(WS, PROVIDER, REPO, {"backfill_days": 60, "deploy_source": "ftp"}, engine=engine)
    assert sm.load(WS, PROVIDER, REPO, engine=engine).backfill_days == 30


def test_a_repository_setting_needs_both_halves_of_its_name() -> None:
    with pytest.raises(sm.SettingsError):
        sm.save(WS, PROVIDER, "", {"enabled": True}, engine=make_engine())


def test_a_list_can_be_given_as_text_one_entry_per_line_or_comma() -> None:
    assert sm.validate("ignored_authors", "renovate, dependabot\nkody") == ["renovate", "dependabot", "kody"]


def test_a_stored_value_the_rules_reject_falls_back_instead_of_stopping_the_sync() -> None:
    engine = make_engine()
    with Session(engine) as s:
        s.add(ProductivityRepoSettings(workspace_id=WS, provider=PROVIDER, repo=REPO, enabled=True,
                                       backfill_days=99999, rate_per_hour=1))
        s.commit()
    got = sm.load(WS, PROVIDER, REPO, engine=engine)
    assert got.enabled is True and got.backfill_days == 180 and got.rate_per_hour == 500
