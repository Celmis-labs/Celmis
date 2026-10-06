"""The first run reads `backfill_days` back and no further; widening the window reads the extra."""

from __future__ import annotations

from datetime import timedelta

from src.productivity import settings as settings_mod
from tests.productivity.support import (
    NOW,
    PROVIDER,
    REPO,
    WS,
    FakeProvider,
    at,
    aware,
    enable,
    make_engine,
    pr,
    prs_in,
    state_of,
    sync,
)


def _history():
    return [pr(1, merged=at(-200), created=at(-205)), pr(2, merged=at(-100), created=at(-105)),
            pr(3, merged=at(-20), created=at(-25)), pr(4, merged=at(-3), created=at(-6))]


def test_the_default_window_is_180_days() -> None:
    engine = make_engine()
    enable(engine)
    provider = FakeProvider(_history())
    sync(engine, provider)
    assert provider.list_since == [NOW - timedelta(days=180)]
    assert sorted(prs_in(engine)) == [2, 3, 4]


def test_a_repository_can_ask_for_less() -> None:
    engine = make_engine()
    enable(engine, backfill_days=30)
    provider = FakeProvider(_history())
    sync(engine, provider)
    assert provider.list_since == [NOW - timedelta(days=30)]
    assert sorted(prs_in(engine)) == [3, 4]
    assert aware(state_of(engine).backfill_from) == NOW - timedelta(days=30)


def test_widening_the_window_later_reads_the_older_prs_and_keeps_the_rest() -> None:
    engine = make_engine()
    enable(engine, backfill_days=30)
    sync(engine, FakeProvider(_history()))
    settings_mod.save(WS, PROVIDER, REPO, {"backfill_days": 365}, engine=engine)
    wider = FakeProvider(_history())
    sync(engine, wider)
    assert wider.list_since == [NOW - timedelta(days=365)]
    assert sorted(prs_in(engine)) == [1, 2, 3, 4]
    assert wider.detail_calls == [2, 1]          # 3 and 4 were done in the first run


def test_the_window_is_never_wider_than_the_limit_the_settings_allow() -> None:
    import pytest

    with pytest.raises(settings_mod.SettingsError):
        settings_mod.validate("backfill_days", 5000)
