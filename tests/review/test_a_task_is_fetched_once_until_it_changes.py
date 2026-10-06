"""A task is read from Jira once, then from the shared cache until it changes.

Ten pull requests naming the same task, or three workers reviewing the same
one, must not make thirty requests to a site that rate-limits. Within the
window the cached read is used; after it, one cheap request asks only for the
`updated` stamp and a task that did not change is served again; one that did
is read in full. A missing or forbidden task is remembered briefly so a typo in
a branch name is not retried on every push.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from src.db.models import TaskContextCache
from src.review.task_context import cache, service
from src.review.task_context.jira_client import JiraError
from tests.review import jira_fakes
from tests.review.jira_fakes import FakeJira, issue


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    jira_fakes.install(monkeypatch, tmp_path, fake)
    return fake


def _read(fake: FakeJira, key="PROJ-6066", workspace="ws-a"):
    with fake.client() as client:
        return service.read_issue(client, workspace, key, acceptance_field=None, comments_n=0)


def _age(minutes: float) -> None:
    """Make every cached read `minutes` old."""
    with Session(cache._engine()) as s:
        for row in s.query(TaskContextCache):
            row.fetched_at = datetime.now(UTC) - timedelta(minutes=minutes)
        s.commit()


def test_a_second_read_inside_the_window_makes_no_request(site):
    _read(site)
    _read(site)
    assert site.count("/issue/PROJ-6066") == 1


def test_a_stale_read_asks_only_for_the_updated_stamp_and_keeps_an_unchanged_task(site):
    first = _read(site)
    _age(60)
    again = _read(site)
    assert again.summary == first.summary
    # One full read, then one stamp check: two requests, no comments fetched.
    assert site.calls.count("/issue/PROJ-6066") == 2
    assert site.count("/comment") == 0


def test_a_task_that_changed_is_read_again_in_full(site):
    _read(site)
    site.issues["PROJ-6066"] = issue(summary="New title", updated="2026-10-02T09:00:00.000+0000")
    _age(60)
    assert _read(site).summary == "New title"


def test_a_task_is_cached_per_workspace_so_one_cannot_read_anothers(site):
    _read(site, workspace="ws-a")
    _read(site, workspace="ws-b")
    assert site.count("/issue/PROJ-6066") == 2


def test_a_missing_task_is_remembered_for_a_short_while(site):
    for _ in range(2):
        with pytest.raises(JiraError) as err:
            _read(site, key="PROJ-404")
        assert err.value.kind == "not_found"
    assert site.count("/issue/PROJ-404") == 1


def test_a_remembered_miss_is_retried_once_it_is_old_enough(site):
    with pytest.raises(JiraError):
        _read(site, key="PROJ-404")
    _age(10)
    site.issues["PROJ-404"] = issue("PROJ-404", summary="Now it exists")
    assert _read(site, key="PROJ-404").summary == "Now it exists"


def test_asking_for_comments_after_a_read_without_them_is_not_served_from_the_old_read(site):
    _read(site)
    with site.client() as client:
        service.read_issue(client, "ws-a", "PROJ-6066", acceptance_field=None, comments_n=3)
    assert site.count("/comment") == 1


def test_a_database_that_cannot_be_read_still_gives_the_task(site, monkeypatch):
    def broken():
        raise RuntimeError("no database")

    monkeypatch.setattr(cache, "_engine", broken)
    assert _read(site).key == "PROJ-6066"


def test_a_blip_on_the_updated_check_serves_the_stored_task(site):
    first = _read(site)
    _age(60)
    site.raises = httpx.ConnectError("refused")
    again = _read(site)
    assert again.summary == first.summary


def test_a_rejected_token_on_the_updated_check_is_not_hidden_by_the_stored_task(site):
    _read(site)
    _age(60)
    site.status_for["PROJ-6066"] = 401
    with pytest.raises(JiraError) as err:
        _read(site)
    assert err.value.kind == "auth"
