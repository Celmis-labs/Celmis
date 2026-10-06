"""A Jira site that cannot be used, or is slow, ends the task read in a sentence.

The saved site is checked again when a client is built: a host that no longer
resolves, or resolves to a private address, must not reach the catch-all that
answers "unexpected error" with a traceback on every review. And however many
calls one task read makes, a review waits on the tracker for a bounded time in
all, and does not ask a site that just failed for its project list again at once.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.review.models import PullRequest
from src.review.task_context import service
from src.sync import jira_instance
from tests.review import jira_fakes
from tests.review.jira_fakes import FakeJira, issue


def _pr(title="PROJ-6066 cut profiles") -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=1, title=title, description="",
        author="a", base_ref="main", base_sha="x", head_ref="feat", head_sha="y",
        state="open", hunks=[],
    )


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    fake.issues["PROJ-6067"] = issue()
    real_open_client = service.open_client
    jira_fakes.install(monkeypatch, tmp_path, fake)
    fake.real_open_client = real_open_client
    return fake


def _resolve(**kw):
    return service.resolve_task_context(
        kw.pop("pr", _pr()), workspace_id="ws-a", policy=None, **kw)


def test_a_host_that_now_resolves_to_a_private_address_is_a_sentence_without_a_traceback(
    site, monkeypatch, caplog,
):
    monkeypatch.setattr(service, "open_client", site.real_open_client)
    monkeypatch.setattr(jira_instance, "_resolve", lambda host, port: ["127.0.0.1"])
    with caplog.at_level(logging.WARNING):
        ctx = _resolve()
    assert ctx.status == "error" and "unexpected error" not in ctx.note
    assert ctx.note
    assert not [r for r in caplog.records if r.exc_info]
    assert site.calls == []                       # nothing was sent anywhere


def test_a_host_that_does_not_resolve_is_a_sentence_too(site, monkeypatch, caplog):
    def nowhere(host, port):
        raise OSError("name or service not known")

    monkeypatch.setattr(service, "open_client", site.real_open_client)
    monkeypatch.setattr(jira_instance, "_resolve", nowhere)
    with caplog.at_level(logging.WARNING):
        ctx = _resolve()
    assert ctx.status == "error" and "unexpected error" not in ctx.note
    assert not [r for r in caplog.records if r.exc_info]


def test_a_review_stops_asking_jira_once_its_overall_time_is_spent(site, monkeypatch):
    monkeypatch.setattr(service, "OVERALL_DEADLINE_SECONDS", -1.0)
    ctx = _resolve(pr=_pr(title="PROJ-6066 and PROJ-6067"))
    assert ctx.status == "error" and "too slow" in ctx.note
    assert site.count("/issue/") == 0


def test_a_site_that_failed_the_project_list_is_not_asked_again_at_once(site):
    site.raises = httpx.ConnectError("refused")
    _resolve()
    _resolve()
    assert site.count("/project/search") == 1


def test_the_project_list_is_not_asked_when_the_text_names_nothing_key_shaped(site):
    ctx = _resolve(pr=_pr(title="tidy up the build"))
    assert ctx.status == "no_key"
    assert site.count("/project/search") == 0


def test_the_project_list_is_inside_the_overall_time_a_review_may_spend(site, monkeypatch):
    monkeypatch.setattr(service, "OVERALL_DEADLINE_SECONDS", -1.0)
    ctx = _resolve()
    assert ctx.status == "error"
    assert site.calls == []                       # not even the project list was asked
