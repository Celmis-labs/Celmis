"""Manual / bulk review honours the repo's effective target branches.

The open-PR listing marks each PR `targeted`, lists targeted ones first and
exposes the patterns; "Review all" leaves untargeted PRs out (reported as
skipped, not counted toward BULK_LIMIT, no run row); the orchestrator's gate
remains the authority for a single-PR review.
"""

from __future__ import annotations

import pytest

from src.review.dispatch import BULK_LIMIT
from tests.api.test_every_open_pull_request_can_be_reviewed import (  # noqa: F401
    _app,
    jobs,
    server,
    store,
)


@pytest.fixture
def patterns(monkeypatch):
    import src.review.review_defaults as rd

    holder = {"v": ["main"]}
    monkeypatch.setattr(rd, "target_branches_for_repo", lambda p, r: holder["v"])
    return holder


def test_the_listing_marks_orders_and_filters_by_target(
    server, store, patterns, monkeypatch, tmp_path,  # noqa: F811
):
    server("github", total=10)  # odd -> main (targeted), even -> develop
    c = _app(monkeypatch, tmp_path, provider="github")
    body = c.get("/api/repos/acme-api/pulls").json()
    assert body["effective_target_branches"] == ["main"]
    assert body["targeted_total"] == 5 and body["total"] == 10
    flags = [i["targeted"] for i in body["items"]]
    assert flags == sorted(flags, reverse=True)  # targeted first
    assert all(i["targeted"] == (i["target_branch"] == "main") for i in body["items"])

    only = c.get("/api/repos/acme-api/pulls?targeted_only=true").json()
    assert only["total"] == 5 and all(i["targeted"] for i in only["items"])

    patterns["v"] = []
    open_ = c.get("/api/repos/acme-api/pulls").json()
    assert all(i["targeted"] for i in open_["items"])
    assert open_["effective_target_branches"] == []


def test_bulk_review_leaves_untargeted_prs_out_and_reports_them(
    server, store, jobs, patterns, monkeypatch, tmp_path,  # noqa: F811
):
    server("github", total=5)
    c = _app(monkeypatch, tmp_path, provider="github")
    r = c.post("/api/repos/acme-api/pulls/review-all",
               json={"confirm": True, "numbers": [1, 2, 3, 4]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(i["number"] for i in body["items"]) == [1, 3]
    assert body["requested"] == body["queued"] == 2 == len(jobs)
    assert [(s["number"], s["status"], s["reason"]) for s in body["skipped"]] == [
        (2, "skipped", "base branch not targeted"),
        (4, "skipped", "base branch not targeted"),
    ]
    # no run row was written for the skipped ones
    assert store.latest_for_prs("ws-1", "github", "acme/api", [2, 4]) == {}


def test_untargeted_prs_do_not_count_toward_the_bulk_limit(
    server, store, jobs, patterns, monkeypatch, tmp_path,  # noqa: F811
):
    server("github", total=BULK_LIMIT * 2 + 2)  # half target develop
    patterns["v"] = ["main"]
    c = _app(monkeypatch, tmp_path, provider="github")
    # every PR of the develop filter is untargeted: nothing queued, no 422
    r = c.post("/api/repos/acme-api/pulls/review-all",
               json={"confirm": True, "branch": "develop"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == body["queued"] == 0 == len(jobs)
    assert len(body["skipped"]) == BULK_LIMIT + 1

    # unfiltered: the targeted ones still exceed the limit — and say so
    r = c.post("/api/repos/acme-api/pulls/review-all", json={"confirm": True})
    assert r.status_code == 422


def test_without_patterns_everything_is_queued_as_before(
    server, store, jobs, patterns, monkeypatch, tmp_path,  # noqa: F811
):
    server("github", total=4)
    patterns["v"] = []
    c = _app(monkeypatch, tmp_path, provider="github")
    body = c.post("/api/repos/acme-api/pulls/review-all",
                  json={"confirm": True}).json()
    assert body["requested"] == body["queued"] == 4 and body["skipped"] == []
