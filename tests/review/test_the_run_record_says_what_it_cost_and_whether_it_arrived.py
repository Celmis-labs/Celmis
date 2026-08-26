"""The run row has to say what the review cost and whether it ever arrived.

Two lies, measured on the same 14 rows of the runG2 Martian-bench run
(`runs/runG2/per_pr.json`):

  * MONEY. All 14 rows carry `cost_usd: null` and `tokens_in`/`tokens_out` of
    0, on runs that produced up to 13 findings from a roster of six agents.
    The orchestrator's aggregation loop had already summed both onto the
    batch; `record_completed_review` — the writer the queue path (webhook,
    poller, the benchmark) goes through — simply never passed them to
    `store.update`. The UI trigger's writer in src/api/routers/reviews.py has
    passed all four since Stage 11, which makes this the same drift between
    the same two writers that the docstring of `record_completed_review`
    already records ("the copy in the UI path grew `evidence_kind` and
    `cross_repo_callers` while the other had nothing at all").

  * DELIVERY. `discourse-graphite#18` has `posted: 0` with
    `status: "complete"`. GitHub answered the review POST with 422, the
    orchestrator turned the `PullRequestProviderError` into
    `provider_response={"error": ...}`, and the store read that dict for its
    "cleanup" key and nothing else. Four findings, none on the pull request,
    and a row an operator cannot tell from the thirteen that delivered.

What is pinned here, driving the real `ReviewOrchestrator` (so the cost the
row shows is the one the real aggregation loop computed) into the real
`record_completed_review` against a real temporary sqlite:

  * a normal run records its cost, its source and both token counts;
  * an unknown cost writes no number and is still distinguishable from a run
    nobody recorded, and a genuinely free run is distinguishable from both;
  * a run that was asked to post and posted nothing does not say `complete`,
    says why, and is not reported as a missing stage — `partial` is reused
    rather than joined by a seventh word, and `post_error` is what keeps the
    two cases apart;
  * a dry run, a run nobody asked to post, and a run that posted are all
    untouched;
  * every status written stays inside `ReviewRunStatus`, because the reviews
    page badges a fixed map of exactly those six words;
  * the two completion writers record the same row — the guard the file's own
    docstring argues for, and the one that would have caught the cost defect.

The database is a real temporary sqlite rather than a mock: `store.update`'s
"None means leave the column alone" rule is the trap this whole exercise turns
on, and a recording double would test the double's version of it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.review_runs import (
    ReviewRun,
    ReviewRunStore,
    completion_status,
    post_failure,
    record_completed_review,
)
from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewRunStatus,
)
from src.review.orchestrator import ReviewOrchestrator
from src.review.providers.base import PullRequestProviderError

# What GitHub actually says when it rejects the whole review, as
# GitHubPRProvider.post_review formats it before raising.
GH_422 = (
    "GitHub review POST failed 422: "
    '{"message":"Validation Failed","errors":[{"resource":"PullRequestReview",'
    '"code":"custom","message":"pull_request_review_thread.line must be part '
    'of the diff"}]}'
)

# The success shape github/gitlab/bitbucket return from a real post.
POSTED_OK = {
    "review_id": 4242,
    "summary_comment_id": 99,
    "html_url": "https://github.com/acme/api/pull/7#pullrequestreview-4242",
    "comments_posted": 1,
    "cleanup": {"deleted": 2, "failed": 0, "kept_threaded": 1, "complete": True},
}

# Every provider's dry-run branch returns this shape, from before any
# network call — which is why it can never carry an error.
DRY_RUN_RESPONSE = {"dry_run": True, "would_post": {"comments": []}}


# ─── doubles ─────────────────────────────────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        raw_diff="@@ -1 +1,2 @@\n line\n+added\n",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _finding(title: str = "reads x before it is assigned") -> Finding:
    return Finding(
        file_path="src/foo.py", line=1, severity=FindingSeverity.WARNING,
        title=title, body="b", agent="architect", rule_id="quality.dead-code",
    )


class _Canned(ReviewAgent):
    """An agent that hands back a prepared AgentRunResult — what the real
    aggregation loop reads, cost and tokens included."""

    def __init__(self, name: str, **result) -> None:
        self.name = name
        self._result = result

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, **self._result)


class _PassThroughVerifier:
    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider:
    """A provider whose post step is the only thing the test varies."""

    def __init__(self, *, response=None, raises: Exception | None = None) -> None:
        self._response = response if response is not None else {}
        self._raises = raises
        self.post_calls: list[bool] = []

    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):
        self.post_calls.append(dry_run)
        if dry_run:
            return DRY_RUN_RESPONSE
        if self._raises is not None:
            raise self._raises
        return self._response

    def close(self):
        pass


@pytest.fixture
def store(tmp_path: Path) -> ReviewRunStore:
    return ReviewRunStore(tmp_path / "review_runs.db")


@pytest.fixture
def review(monkeypatch):
    """Drive the REAL orchestrator — cost aggregation, verdict, post step —
    with no network and no database, and hand back the real ReviewRunResult.

    The cost a row shows has to be the cost the production loop computed; a
    hand-built batch would keep agreeing with itself after that loop changed.
    """
    import src.notifications as notif_mod
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    import src.review.reviewer_assignment as assign_mod

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))
    # Not under test, and both are already swallowed by the orchestrator —
    # stubbed so the run needs no channels config and no ownership snapshot.
    monkeypatch.setattr(notif_mod, "notify", lambda **kw: None)
    monkeypatch.setattr(assign_mod, "assign_reviewers_by_ownership",
                        lambda **kw: None)

    def _run(*agents, provider=None, post_comments=True, dry_run=False):
        orch = ReviewOrchestrator(agents=list(agents),
                                  verifier=_PassThroughVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        return orch.review(
            "github", "acme/api", 7,
            dry_run=dry_run, post_comments=post_comments,
            provider=provider if provider is not None else _Provider(),
        )

    return _run


def _record(store: ReviewRunStore, result, run_id: str = "r") -> ReviewRun:
    store.insert(ReviewRun(id=run_id, user_id="u1", pr_ref="github:acme/api#7"))
    record_completed_review(result, run_id=run_id, store=store)
    return store.get(run_id)


# ─── the money ───────────────────────────────────────────────────────


def test_a_run_records_what_it_cost_and_the_tokens_it_spent(review, store):
    """Two agents, $0.81 each — the sum the real aggregation loop produces."""
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=0.81,
                cost_source="litellm_estimate", tokens_in=1000, tokens_out=400),
        _Canned("security", cost_usd=0.81, cost_source="litellm_estimate",
                tokens_in=1000, tokens_out=400),
        provider=_Provider(response=POSTED_OK),
    )

    row = _record(store, result)

    assert row.cost_usd == pytest.approx(1.62)
    assert row.cost_source == "litellm_estimate"
    assert row.tokens_input == 2000
    assert row.tokens_output == 800


def test_an_unknown_cost_writes_no_number_but_still_says_it_was_recorded(review, store):
    """An agent on a model with no price makes the total a guess, and a guess
    is not a number. The column stays NULL — 0.0 would be a claim that the
    run was free, which is the lie in the other direction."""
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=0.81,
                cost_source="litellm_estimate", tokens_in=1000, tokens_out=400),
        # Spent tokens, reported no price: this is what makes the total unknown.
        _Canned("security", cost_usd=None, tokens_in=500, tokens_out=100),
        provider=_Provider(response=POSTED_OK),
    )

    row = _record(store, result)

    assert row.cost_usd is None
    # The tokens are still known, and are still worth writing down.
    assert (row.tokens_input, row.tokens_output) == (1500, 500)
    # And this is what keeps it apart from a row nobody wrote.
    assert row.cost_source == "litellm_estimate"


def test_an_unknown_cost_and_an_unrecorded_run_are_not_the_same_row(review, store):
    """Both leave `cost_usd` NULL. `cost_source` is what tells them apart:
    a recorded run has a source whatever its total; a row no completion
    writer ever reached has neither."""
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=None,
                tokens_in=500, tokens_out=100),
        provider=_Provider(response=POSTED_OK),
    )
    recorded = _record(store, result, run_id="r-unknown")

    store.insert(ReviewRun(id="r-never", user_id="u1", pr_ref="github:acme/api#8"))
    never = store.get("r-never")

    assert (recorded.cost_usd, never.cost_usd) == (None, None)
    assert recorded.cost_source is not None
    assert never.cost_source is None


def test_a_run_that_really_was_free_is_not_read_back_as_unrecorded(review, store):
    """0.0 is an answer — "recorded, and it cost nothing" — and `store.update`
    persists it because 0.0 is not None."""
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=0.0,
                cost_source="openrouter_actual", tokens_in=10, tokens_out=2),
        provider=_Provider(response=POSTED_OK),
    )

    row = _record(store, result)

    assert row.cost_usd == 0.0
    assert row.cost_usd is not None
    assert row.cost_source == "openrouter_actual"


def test_a_batch_that_never_had_the_cost_fields_stays_unrecorded(store):
    """An engine or a double built before Stage 11 must read back as "nobody
    recorded it", not as a free review."""
    batch = SimpleNamespace(
        run_status=ReviewRunStatus.COMPLETE, agents_run=["architect"],
        agents_failed=[], verdict=SimpleNamespace(value="approve"), findings=[],
        critical_count=0, error_count=0, warning_count=0, info_count=0,
        cross_repo_callers=0, elapsed_seconds=1.0, summary="ok",
    )

    row = _record(store, SimpleNamespace(batch=batch, posted=True))

    assert row.cost_usd is None
    assert row.cost_source is None
    assert (row.tokens_input, row.tokens_output) == (0, 0)


# ─── the delivery ────────────────────────────────────────────────────


def test_a_run_that_was_asked_to_post_and_posted_nothing_is_not_complete(review, store):
    """discourse-graphite#18, reproduced: findings produced, GitHub refused
    the whole review, and the row used to read `complete`."""
    provider = _Provider(raises=PullRequestProviderError(GH_422))
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=0.1,
                cost_source="litellm_estimate", tokens_in=10, tokens_out=5),
        provider=provider,
    )

    row = _record(store, result)

    assert provider.post_calls == [False], "the run really did try to post"
    assert row.status != ReviewRunStatus.COMPLETE.value
    assert row.status == ReviewRunStatus.PARTIAL.value
    assert row.posted is False
    assert row.post_error is not None
    assert "422" in row.post_error
    # The findings survive — they exist, they are stored, they were never
    # delivered. That is the whole distinction the row has to carry.
    assert row.findings_count == 1


def test_a_failed_delivery_is_not_reported_as_a_missing_stage(review, store):
    """`partial` is reused because a seventh status word would render as no
    badge at all — so the row has to carry what tells the two cases apart.
    An empty `agents_failed` beside a `post_error` is a complete review
    nobody received; a named agent with no `post_error` is a review with a
    hole in it that was delivered."""
    undelivered = _record(store, review(
        _Canned("architect", findings=[_finding()]),
        provider=_Provider(raises=PullRequestProviderError(GH_422)),
    ), run_id="r-undelivered")

    holed = _record(store, review(
        _Canned("architect", findings=[_finding()]),
        _Canned("security", error="provider quota exhausted"),
        provider=_Provider(response=POSTED_OK),
    ), run_id="r-holed")

    assert undelivered.status == holed.status == ReviewRunStatus.PARTIAL.value
    assert undelivered.agents_failed == []
    assert undelivered.post_error is not None
    assert holed.agents_failed == ["security"]
    assert holed.post_error is None
    assert holed.posted is True


def test_a_dry_run_is_unaffected(review, store):
    """It never posts by design, so there is nothing it failed to deliver."""
    provider = _Provider(response=POSTED_OK)
    result = review(
        _Canned("architect", findings=[_finding()]),
        provider=provider, dry_run=True, post_comments=True,
    )

    row = _record(store, result)

    assert provider.post_calls == [True]
    assert row.status == ReviewRunStatus.COMPLETE.value
    assert row.post_error is None
    assert row.posted is False


def test_a_dry_run_that_somehow_reports_an_error_is_still_not_a_delivery_failure():
    """Belt and braces for a provider that grows a simulation able to report
    a problem: the `dry_run` marker wins over the `error` key, because a
    simulation that did not post is not a review that failed to arrive."""
    result = SimpleNamespace(
        batch=None,
        provider_response={"dry_run": True, "error": "would have failed"},
    )

    assert post_failure(result) is None


def test_a_run_nobody_asked_to_post_is_unaffected(review, store):
    provider = _Provider(response=POSTED_OK)
    result = review(
        _Canned("architect", findings=[_finding()]),
        provider=provider, post_comments=False,
    )

    row = _record(store, result)

    assert provider.post_calls == []
    assert row.status == ReviewRunStatus.COMPLETE.value
    assert row.post_error is None


def test_a_run_that_posted_is_unchanged(review, store):
    result = review(
        _Canned("architect", findings=[_finding()], cost_usd=0.5,
                cost_source="litellm_estimate", tokens_in=100, tokens_out=20),
        provider=_Provider(response=POSTED_OK),
    )

    row = _record(store, result)

    assert row.status == ReviewRunStatus.COMPLETE.value
    assert row.posted is True
    assert row.post_error is None
    # The cleanup report still comes out of the same dict it always did.
    assert row.cleanup == POSTED_OK["cleanup"]


def test_a_run_in_which_nothing_answered_stays_failed(review, store):
    """A failed delivery does not upgrade the news. Every agent errored, so
    there was never a review to deliver — `failed` is still the right word,
    and `post_error` records the second thing that went wrong."""
    result = review(
        _Canned("architect", error="provider quota exhausted"),
        _Canned("security", error="provider quota exhausted"),
        provider=_Provider(raises=PullRequestProviderError(GH_422)),
    )

    row = _record(store, result)

    assert row.status == ReviewRunStatus.FAILED.value
    assert row.post_error is not None


def test_every_status_the_writer_produces_is_one_the_page_can_badge(review, store):
    """`STATUS_BADGES` in web/app/(app)/reviews/page.tsx is a fixed map of
    exactly the six members of `ReviewRunStatus`; its `else` arm drops the
    lifecycle badge and shows the VERDICT instead, so a seventh word would
    render an undelivered review as a bare "COMMENT" — the same badge a
    reviewed-and-posted pull request gets."""
    vocabulary = {s.value for s in ReviewRunStatus}
    cases = {
        "delivered": (_Provider(response=POSTED_OK), []),
        "undelivered": (_Provider(raises=PullRequestProviderError(GH_422)), []),
        "holed": (_Provider(raises=PullRequestProviderError(GH_422)),
                  [_Canned("security", error="quota")]),
    }

    for name, (provider, extra) in cases.items():
        row = _record(store, review(
            _Canned("architect", findings=[_finding()]), *extra,
            provider=provider,
        ), run_id=f"r-{name}")
        assert row.status in vocabulary, name


def test_the_status_helper_leaves_a_skipped_run_alone():
    """SKIPPED never reached the post step, so it cannot have failed one —
    and calling it `partial` would claim a review happened."""
    for status in (ReviewRunStatus.SKIPPED, ReviewRunStatus.FAILED,
                   ReviewRunStatus.PARTIAL):
        batch = SimpleNamespace(run_status=status)
        assert completion_status(batch, "boom") == status.value


# ─── the two writers record the same row ─────────────────────────────


#: Columns the UI trigger's writer fills and `record_completed_review` does
#: not. This is OUTSTANDING drift, not blessed drift: the PR coordinates and
#: the diff snapshot are what the apply-fix and side-by-side views read, so a
#: webhook-triggered run cannot be apply-fixed today. They are pinned by an
#: EXACT comparison below, which means this test fails both when new drift
#: appears and when this drift is closed — at which point the entry comes out
#: of this set. `raw_diff` is a storage decision (capped at 800 KB per row)
#: that belongs with whoever owns src/api/routers/reviews.py.
KNOWN_OUTSTANDING = {
    "pr_head_sha", "pr_head_ref", "pr_provider", "pr_repo", "pr_number",
    "raw_diff",
}

#: Genuinely per-row, not drift.
_PER_ROW = {"id", "started_at", "finished_at"}

#: TEXT columns holding JSON. Compared as the values they encode: the two
#: writers call `json.dumps` with different `ensure_ascii`, which changes the
#: bytes and not one field of the record.
_JSON_COLUMNS = {"findings_json", "drift_json", "cleanup_json",
                 "adjustments_json", "hidden_json", "agents_run",
                 "agents_failed"}


def _raw_row(store: ReviewRunStore, run_id: str) -> dict:
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
    out = {}
    # noqa on purpose: `row` is a sqlite3.Row. Iterating it yields the VALUES;
    # only `.keys()` yields the column names, so the "simplification" would
    # compare the two rows by value-as-key and pass on anything.
    for key in row.keys():  # noqa: SIM118
        value = row[key]
        if key in _JSON_COLUMNS and isinstance(value, str):
            value = json.loads(value)
        out[key] = value
    return out


def test_both_completion_writers_record_the_same_row(review, store, monkeypatch):
    """The guard the file's own docstring argues for.

    Two writers, one row shape. They had already drifted twice — the UI copy
    grew `evidence_kind` and `cross_repo_callers` while the other had nothing,
    and then it kept the four cost fields to itself for every run the queue
    path ever wrote. This drives BOTH against the same finished review and
    compares the rows they leave behind, so a field added to one and not the
    other fails here instead of in a benchmark six months later.
    """
    import src.api.routers.reviews as reviews_mod
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    # A finished review with money in it, a non-ASCII title (the two writers
    # serialise findings with different `ensure_ascii`), and a cleanup report.
    result = review(
        _Canned("architect", findings=[_finding("café pointer is reused")],
                cost_usd=0.81, cost_source="litellm_estimate",
                tokens_in=1000, tokens_out=400),
        _Canned("security", error="provider quota exhausted"),
        provider=_Provider(response=POSTED_OK),
    )

    class _Prov:
        def close(self) -> None:
            pass

    class _Orch:
        def review(self, *a, **kw):
            return result

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", _Orch)
    monkeypatch.setattr(providers_mod, "get_provider_for", lambda *a, **kw: _Prov())
    monkeypatch.setattr(reviews_mod, "get_review_run_store", lambda: store)

    for run_id in ("r-queue", "r-ui"):
        store.insert(ReviewRun(id=run_id, user_id="u1",
                               pr_ref="github:acme/api#7", workspace_id="ws1"))
    record_completed_review(result, run_id="r-queue", store=store)
    reviews_mod._run_review_task(
        pr_ref="github:acme/api#7", post_comments=True,
        run_id="r-ui", user_id="u1", workspace_id="ws1",
    )

    queue, ui = _raw_row(store, "r-queue"), _raw_row(store, "r-ui")
    differ = {c for c in queue if c not in _PER_ROW and queue[c] != ui[c]}

    assert differ == KNOWN_OUTSTANDING, (
        "the two completion writers drifted; see KNOWN_OUTSTANDING above"
    )
    # Named explicitly, because these four are the defect this test exists for
    # and an exception set is only as good as what it is not allowed to hold.
    for column in ("cost_usd", "cost_source", "tokens_input", "tokens_output"):
        assert column not in differ
        assert queue[column] == ui[column]
    assert queue["cost_usd"] == pytest.approx(0.81)
    assert (queue["tokens_input"], queue["tokens_output"]) == (1000, 400)


def test_the_ui_writer_does_not_yet_record_a_failed_delivery(review, store, monkeypatch):
    """The outstanding half, pinned so it cannot be forgotten.

    `completion_status` and `post_failure` live in src/api/review_runs.py and
    only `record_completed_review` calls them; the UI trigger's writer in
    src/api/routers/reviews.py still passes `batch.run_status.value` straight
    through, so a review triggered from the UI that GitHub refuses still says
    `complete`. This test asserts the gap rather than hiding it, and will fail
    the moment somebody closes it — which is when it should be deleted.
    """
    import src.api.routers.reviews as reviews_mod
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    result = review(
        _Canned("architect", findings=[_finding()]),
        provider=_Provider(raises=PullRequestProviderError(GH_422)),
    )

    class _Orch:
        def review(self, *a, **kw):
            return result

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", _Orch)
    monkeypatch.setattr(providers_mod, "get_provider_for",
                        lambda *a, **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(reviews_mod, "get_review_run_store", lambda: store)
    store.insert(ReviewRun(id="r-ui", user_id="u1", pr_ref="github:acme/api#7"))

    reviews_mod._run_review_task(
        pr_ref="github:acme/api#7", post_comments=True,
        run_id="r-ui", user_id="u1", workspace_id="ws1",
    )

    row = store.get("r-ui")
    assert row.post_error is None
    assert row.status == ReviewRunStatus.COMPLETE.value, (
        "if this now says 'partial', the UI writer was fixed — delete this test"
    )


# ─── the migration ───────────────────────────────────────────────────


_LEGACY_SCHEMA = """
CREATE TABLE review_runs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pr_ref          TEXT NOT NULL,
    status          TEXT NOT NULL,
    verdict         TEXT NOT NULL DEFAULT 'pending',
    findings_count  INTEGER NOT NULL DEFAULT 0,
    critical        INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    warning         INTEGER NOT NULL DEFAULT 0,
    info            INTEGER NOT NULL DEFAULT 0,
    cross_repo_callers INTEGER NOT NULL DEFAULT 0,
    posted          INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL,
    summary         TEXT NOT NULL DEFAULT '',
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
"""


def test_the_column_arrives_on_a_database_that_predates_it(tmp_path: Path):
    """And a row written before it reads back as "not recorded" — never as
    "delivered fine", which is the same false negative one layer down."""
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO review_runs (id, user_id, pr_ref, status, started_at)"
            " VALUES ('old', 'u1', 'github:acme/api#1', 'complete', '2026-01-01')")

    store = ReviewRunStore(db)

    assert store.get("old").post_error is None
    # And it is writable now, on the same database.
    store.update("old", post_error="boom")
    assert store.get("old").post_error == "boom"
    # Reopening applies nothing twice.
    assert ReviewRunStore(db).get("old").post_error == "boom"
