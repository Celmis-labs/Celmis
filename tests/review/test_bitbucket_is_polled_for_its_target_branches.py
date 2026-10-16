"""Bitbucket repositories with auto-review on are polled — no webhook needed.

One listing per repository per poll, asking Bitbucket only for open pull
requests updated since the last poll into the repository's target branches;
the requests of all repositories sharing a token stay within an hourly
budget. A listed pull request goes through the webhook's own dispatch, so
the gates (target branch, draft, title, same commit, cadence) and the
queue's dedup are the same whichever noticed the change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    bound,
    no_ledger,
    queue,
    store,
)

REPO = "acme/payments"


def _bb_cfg(cfg, **kw):
    base = dict(provider="bitbucket", repo_slug="bitbucket_acme-payments",
                url=f"https://bitbucket.org/{REPO}", full_name=REPO, mode="polling")
    base.update(kw)
    return cfg(**base)


def _pr(number, *, head="a" * 40, base="develop", title="Add totals", draft=False):
    return {
        "id": number, "title": title, "draft": draft,
        "updated_on": datetime.now(UTC).isoformat(),
        "source": {"commit": {"hash": head}, "branch": {"name": f"feature/{number}"}},
        "destination": {"branch": {"name": base}},
        "author": {"display_name": "Dana", "nickname": "dana"},
        "links": {"html": {"href": f"https://bitbucket.org/{REPO}/pull-requests/{number}"}},
    }


@pytest.fixture
def clean_poller(bound, monkeypatch):  # noqa: F811
    from src.review import poller

    # The poller holds its own reference to the store accessor.
    monkeypatch.setattr(poller, "get_auto_review_store", lambda: bound[0])
    poller._BB_SEEN.clear()
    poller._BB_BACKOFF_UNTIL.clear()
    yield poller
    poller._BB_SEEN.clear()
    poller._BB_BACKOFF_UNTIL.clear()


@pytest.fixture
def patterns(monkeypatch):
    import src.review.review_defaults as rd

    box = {"value": ["develop"]}
    monkeypatch.setattr(rd, "target_branches_for_repo", lambda p, r: box["value"])
    monkeypatch.setattr(rd, "run_on_drafts_for_repo", lambda p, r: False)
    return box


class _FakeProvider:
    """Stands in for BitbucketPRProvider in the poller: records each listing."""

    listings: list[dict] = []
    calls: list[dict] = []
    error: Exception | None = None

    def __init__(self, token, *, email=None, workspace_id="default"):
        self.token, self.email = token, email

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def list_open_pull_requests(self, repo, *, updated_after, target_patterns=None):
        type(self).calls.append({"repo": repo, "since": updated_after,
                                 "patterns": target_patterns})
        if type(self).error is not None:
            raise type(self).error
        return list(type(self).listings)


@pytest.fixture
def fake_bb(monkeypatch):
    import src.review.providers.bitbucket as bb

    _FakeProvider.listings, _FakeProvider.calls, _FakeProvider.error = [], [], None
    monkeypatch.setattr(bb, "BitbucketPRProvider", _FakeProvider)
    return _FakeProvider


def _age(s, cfg_row, seconds):
    """Pretend the repository was last listed `seconds` ago."""
    import sqlite3

    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(s.db_path) as conn:
        conn.execute("UPDATE auto_review_config SET last_polled_at=? "
                     "WHERE user_id=? AND repo_slug=?",
                     (stamp, cfg_row.user_id, cfg_row.repo_slug))
    return s.get(cfg_row.user_id, cfg_row.repo_slug)


# ─── the query ───────────────────────────────────────────────────────


def test_the_query_names_only_open_prs_updated_since_into_the_target_branches():
    from src.review.providers.bitbucket import open_prs_query

    since = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    q = open_prs_query(since, ["develop", "release/*", "!release/old"])
    assert q == ('state = "OPEN" AND updated_on > 2026-10-16T09:30:00+00:00 AND '
                 '(destination.branch.name = "develop" OR '
                 'destination.branch.name ~ "release/")')


def test_every_branch_means_no_branch_clause_and_names_are_escaped():
    from src.review.providers.bitbucket import open_prs_query

    since = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    for every in ([], None, ["*"], ["develop", "*"], ["!main"]):
        assert "destination" not in open_prs_query(since, every)
    assert 'destination.branch.name = "we\\"ird"' in open_prs_query(since, ['we"ird'])


# ─── the listing request ─────────────────────────────────────────────


class _Http:
    def __init__(self, responses):
        self.responses, self.urls = list(responses), []

    def get(self, url, headers=None):
        self.urls.append(url)
        return self.responses.pop(0)


def _provider(responses):
    from src.review.providers.bitbucket import BitbucketPRProvider

    p = BitbucketPRProvider("tok", email="bot@example.com")
    p._http.close()
    p._http = _Http(responses)
    return p


def _json(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers or {})


def test_one_request_asks_bitbucket_to_filter_and_trim_the_listing():
    p = _provider([_json({"values": [_pr(7)]})])
    since = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
    out = p.list_open_pull_requests(REPO, updated_after=since, target_patterns=["develop"])
    assert [v["id"] for v in out] == [7]
    (url,) = p._http.urls
    parts = urlsplit(url)
    assert parts.path == f"/2.0/repositories/{REPO}/pullrequests"
    qs = parse_qs(parts.query)
    assert 'destination.branch.name = "develop"' in qs["q"][0]
    assert "values.source.commit.hash" in qs["fields"][0]


def test_pages_are_followed_on_bitbucket_only_and_a_429_says_how_long_to_wait():
    from src.review.providers.bitbucket import BitbucketRateLimited

    nxt = "https://api.bitbucket.org/2.0/repositories/acme/payments/pullrequests?page=2"
    p = _provider([_json({"values": [_pr(1)], "next": nxt}),
                   _json({"values": [_pr(2)], "next": "https://evil.example/steal"})])
    since = datetime(2026, 10, 16, tzinfo=UTC)
    assert [v["id"] for v in p.list_open_pull_requests(REPO, updated_after=since)] == [1, 2]
    assert len(p._http.urls) == 2  # the foreign "next" is not followed

    p = _provider([_json({}, status=429, headers={"Retry-After": "42"})])
    with pytest.raises(BitbucketRateLimited) as exc:
        p.list_open_pull_requests(REPO, updated_after=since)
    assert exc.value.retry_after == 42


# ─── the poll cycle ──────────────────────────────────────────────────


def test_the_first_poll_is_a_baseline_then_changes_are_queued_as_the_poller(
    bound, store, queue, no_ledger, clean_poller, patterns, fake_bb,  # noqa: F811
):
    s, cfg = bound
    c = _bb_cfg(cfg)
    s.upsert(c)
    fake_bb.listings = [_pr(7)]

    clean_poller._poll_bitbucket("tok", None, [s.get(c.user_id, c.repo_slug)])
    assert fake_bb.calls == [] and queue == []  # no sweep of what is open
    row = s.get(c.user_id, c.repo_slug)
    assert row.last_polled_at

    row = _age(s, row, 600)
    clean_poller._poll_bitbucket("tok", None, [row])
    (call,) = fake_bb.calls
    assert call["patterns"] == ["develop"]
    # From a little before the last poll, so nothing updated meanwhile is lost.
    assert call["since"] < datetime.fromisoformat(row.last_polled_at)
    (job,) = queue
    assert job["payload"]["pr_number"] == 7
    assert job["payload"]["source"] == "poller"
    assert job["enqueued_by"] == "poller:bitbucket"
    assert job["dedup_key"] == f"review:bitbucket:{REPO}#7"  # same as the webhook's


def test_a_repeat_listing_dispatches_nothing_a_new_commit_does(
    bound, store, queue, no_ledger, clean_poller, patterns, fake_bb,  # noqa: F811
):
    s, cfg = bound
    c = _bb_cfg(cfg)
    s.upsert(c)
    row = _age(s, c, 600)
    fake_bb.listings = [_pr(7)]
    clean_poller._poll_bitbucket("tok", None, [row])
    queue.clear()

    clean_poller._poll_bitbucket("tok", None, [_age(s, row, 600)])
    assert queue == []

    fake_bb.listings = [_pr(7, head="b" * 40)]
    clean_poller._poll_bitbucket("tok", None, [_age(s, row, 600)])
    assert [j["payload"]["pr_number"] for j in queue] == [7]


def test_drafts_and_excluded_branches_are_recorded_not_queued(
    bound, store, queue, no_ledger, clean_poller, patterns, fake_bb,  # noqa: F811
):
    s, cfg = bound
    c = _bb_cfg(cfg)
    s.upsert(c)
    patterns["value"] = ["release/*", "!release/old"]
    fake_bb.listings = [_pr(8, base="release/old"), _pr(9, base="release/2", draft=True)]
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 600)])
    assert queue == []
    reasons = {n: store.list_for_pr("ws-1", "bitbucket", REPO, n)[0].status_reason
               for n in (8, 9)}
    assert reasons[8].startswith("Skipped — Branch mismatch")
    assert "draft" in reasons[9].lower()


def test_a_repository_listed_recently_waits_and_the_budget_spreads_many(
    bound, store, queue, no_ledger, clean_poller, patterns, fake_bb,  # noqa: F811
):
    s, cfg = bound
    c = _bb_cfg(cfg)
    s.upsert(c)
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 30)])
    assert fake_bb.calls == []

    assert clean_poller._bitbucket_interval(1) == clean_poller.BITBUCKET_MIN_INTERVAL_SECONDS
    # Twenty repositories on one token: each listed every 4 minutes, so the
    # token spends at most the budget (300 an hour by default) on listings.
    assert clean_poller._bitbucket_interval(20) * clean_poller.BITBUCKET_HOURLY_BUDGET \
        >= 20 * 3600


def test_a_429_pauses_the_token_and_an_error_pauses_only_that_repository(
    bound, store, queue, no_ledger, clean_poller, patterns, fake_bb,  # noqa: F811
):
    from src.review.providers.base import PullRequestProviderError
    from src.review.providers.bitbucket import BitbucketRateLimited

    s, cfg = bound
    c = _bb_cfg(cfg)
    s.upsert(c)
    fake_bb.error = BitbucketRateLimited("slow down", retry_after=60)
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 600)])
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 600)])
    assert len(fake_bb.calls) == 1  # the second cycle did not ask

    clean_poller._BB_BACKOFF_UNTIL.clear()
    fake_bb.calls.clear()
    fake_bb.error = PullRequestProviderError("403")
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 600)])
    clean_poller._poll_bitbucket("tok", None, [_age(s, c, 600)])
    assert len(fake_bb.calls) == 1


# ─── the mode ────────────────────────────────────────────────────────


def test_bitbucket_auto_review_now_defaults_to_polling_and_restarts_its_cursor(tmp_path):
    from src.api.auto_review import AutoReviewStore, RepoConfig
    from src.api.routers.repos import _default_mode, restart_polling_cursor

    assert _default_mode("bitbucket", True) == "polling"
    assert _default_mode("bitbucket", False) == "manual"
    s = AutoReviewStore(tmp_path / "ar.db")
    c = RepoConfig(user_id="u", repo_slug="r", provider="bitbucket", full_name=REPO,
                   url="u", enabled=True, mode="polling")
    s.upsert(c)
    c = _age(s, c, 3600)
    restart_polling_cursor(c, was_enabled=True, store=s)
    assert s.get("u", "r").last_polled_at  # still on: the cursor stands
    restart_polling_cursor(c, was_enabled=False, store=s)
    assert s.get("u", "r").last_polled_at is None  # just switched on: from now


def test_rows_forced_to_manual_become_polling_once(tmp_path):
    import sqlite3

    from src.api.auto_review import AutoReviewStore, RepoConfig

    db = tmp_path / "ar.db"
    s = AutoReviewStore(db)
    with sqlite3.connect(db) as conn:  # a database from before the migration
        conn.execute("PRAGMA user_version = 0")
    for slug, enabled in (("on", True), ("off", False)):
        s.upsert(RepoConfig(user_id="u", repo_slug=slug, provider="bitbucket",
                            full_name=f"acme/{slug}", url="u", enabled=enabled,
                            mode="manual"))
    s = AutoReviewStore(db)
    assert s.get("u", "on").mode == "polling"
    assert s.get("u", "off").mode == "manual"

    c = s.get("u", "on")
    c.mode = "manual"  # chosen afterwards: never rewritten again
    s.upsert(c)
    assert AutoReviewStore(db).get("u", "on").mode == "manual"
