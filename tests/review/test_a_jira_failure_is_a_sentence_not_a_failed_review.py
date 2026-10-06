"""When Jira cannot be read the review goes on, and says why in plain words.

A rejected token, a task the token cannot see, a missing task, a slow site: none
of these may fail the review or leave the pull request saying nothing. The task
context ends in a status and one sentence the author can act on, and that
sentence never carries the token, the account email or a URL with credentials.
"""

from __future__ import annotations

import httpx
import pytest

from src.review.models import PullRequest
from src.review.task_context import service
from tests.review import jira_fakes
from tests.review.jira_fakes import EMAIL, TOKEN, FakeJira, issue


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
    jira_fakes.install(monkeypatch, tmp_path, fake)
    return fake


def _resolve(**kw):
    return service.resolve_task_context(
        kw.pop("pr", _pr()), workspace_id="ws-a", policy=kw.pop("policy", None), **kw)


def test_a_readable_task_gives_an_ok_context_with_its_criteria(site):
    ctx = _resolve()
    assert ctx.status == "ok" and ctx.ok and ctx.has_statement
    assert [k for k, _ in ctx.criteria] == ["PROJ-6066", "PROJ-6066"]
    assert ctx.task_refs()[0]["key"] == "PROJ-6066"


@pytest.mark.parametrize(("http_status", "status", "words"), [
    (401, "error", "rejected the token"),
    (403, "forbidden", "cannot read"),
    (404, "not_found", "missing"),
    (429, "error", "rate limiting"),
    (500, "error", "returned 500"),
])
def test_a_refusal_from_jira_becomes_a_status_and_a_sentence(site, http_status, status, words):
    site.status_for["PROJ-6066"] = http_status
    ctx = _resolve()
    assert ctx.status == status and not ctx.ok
    assert words in ctx.note


def test_a_site_that_does_not_answer_in_time_is_a_sentence_not_an_exception(site):
    site.raises = httpx.ReadTimeout("slow")
    ctx = _resolve()
    assert ctx.status == "error"
    assert "did not answer" in ctx.note


def test_a_site_that_cannot_be_reached_is_a_sentence_not_an_exception(site):
    site.raises = httpx.ConnectError("refused")
    ctx = _resolve()
    assert ctx.status == "error" and "could not connect" in ctx.note


@pytest.mark.parametrize("http_status", [401, 403, 404, 500])
def test_no_sentence_carries_the_token_the_email_or_credentials(site, http_status):
    site.status_for["PROJ-6066"] = http_status
    note = _resolve().note
    assert TOKEN not in note and EMAIL not in note and "@acme" not in note


def test_a_pull_request_that_names_no_task_says_where_it_looked(site):
    ctx = _resolve(pr=_pr(title="tidy up"))
    assert ctx.status == "no_key"
    assert "title, branch or description" in ctx.note


def test_commit_messages_are_asked_only_when_nothing_else_names_a_task(site):
    asked = []

    def commits():
        asked.append(1)
        return ["PROJ-6066 wip"]

    assert _resolve(commit_messages=commits).ok          # the title names it
    assert asked == []
    assert _resolve(pr=_pr(title="tidy"), commit_messages=commits).ok
    assert asked == [1]


def test_a_commit_list_that_fails_to_load_is_not_a_failed_review(site):
    def broken():
        raise RuntimeError("provider down")

    ctx = _resolve(pr=_pr(title="tidy"), commit_messages=broken)
    assert ctx.status == "no_key"


def test_without_a_connection_the_context_says_so(monkeypatch, tmp_path):
    jira_fakes.install(monkeypatch, tmp_path, FakeJira(), connected=False)
    ctx = _resolve()
    assert ctx.status == "no_connection" and "Connections" in ctx.note


def test_a_repository_that_switched_it_off_reads_nothing(site):
    ctx = _resolve(policy={"task_context_enabled": False})
    assert ctx.status == "disabled"
    assert site.calls == []


def test_an_unexpected_error_inside_the_resolver_never_escapes(site, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "open_client", boom)
    ctx = _resolve()
    assert ctx.status == "error" and "see the server log" in ctx.note


def test_one_unreadable_task_among_several_still_gives_the_readable_one(site):
    site.issues["PROJ-1"] = issue("PROJ-1", summary="Second")
    ctx = _resolve(pr=_pr(title="PROJ-1 and PROJ-7 together"))
    assert ctx.ok and [t.key for t in ctx.tasks] == ["PROJ-1"]
    assert "PROJ-7 could not be read" in ctx.note
