"""Persistent review-run store — surfaces queued/failed runs in the UI.

The audit log (JSONL) only captures completed-successfully runs. When the
review pipeline errors before writing audit events, the run disappears from
the UI. This store gives us first-class visibility regardless of outcome.

Schema:
    review_runs (
        id              TEXT PRIMARY KEY,    -- uuid
        user_id         TEXT NOT NULL,
        pr_ref          TEXT NOT NULL,
        status          TEXT NOT NULL,       -- see ReviewRunStatus (src/review/models.py):
                                             -- queued | running | complete | partial
                                             -- | skipped | failed
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
    )
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_runs (
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
    finished_at     TEXT,
    -- Stage 11 (BYOK) — cost accounting
    cost_usd        REAL,               -- sum across all agents; NULL if any unknown model
    cost_source     TEXT,               -- 'openrouter_actual' | 'litellm_estimate' | 'unknown' | 'mixed'
    tokens_input    INTEGER NOT NULL DEFAULT 0,
    tokens_output   INTEGER NOT NULL DEFAULT 0,
    -- Stage 17 — findings snapshot + PR coordinates for apply-fix UI
    findings_json   TEXT,
    pr_head_sha     TEXT,
    pr_head_ref     TEXT,
    pr_provider     TEXT,
    pr_repo         TEXT,
    pr_number       INTEGER,
    -- The cross-repo drift report as DATA, not prose.
    --
    -- It used to exist only as markdown inside the architect's prompt, so the
    -- one deterministic differentiator in the product reached the user through
    -- a probabilistic layer: they saw what the model chose to mention, not the
    -- fact. Stored here it can be rendered as what it is — value, where it was
    -- removed, where it is still hardcoded, with file and line.
    drift_json      TEXT,
    -- Which agents answered and which did not, as JSON arrays of names.
    --
    -- NULL and '[]' are different answers on purpose. '[]' is "we looked and
    -- nothing failed"; NULL is "this row predates the columns, so nobody
    -- knows" — and a review product that reports an unknown as "nothing
    -- failed" is the exact false negative these columns were added to close.
    agents_run      TEXT,
    agents_skipped  TEXT,
    agents_failed   TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_runs_user_started
    ON review_runs(user_id, started_at DESC);
"""

# Additive migration for pre-Stage-11 DBs (idempotent).
_MIGRATIONS = [
    "ALTER TABLE review_runs ADD COLUMN cost_usd REAL",
    "ALTER TABLE review_runs ADD COLUMN cost_source TEXT",
    "ALTER TABLE review_runs ADD COLUMN tokens_input INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE review_runs ADD COLUMN tokens_output INTEGER NOT NULL DEFAULT 0",
    # Stage 17 — inline findings snapshot so the UI can render + apply-fix
    # per finding without re-parsing audit logs or re-fetching from the
    # provider. Stored as JSON text; typical size <100 KB.
    "ALTER TABLE review_runs ADD COLUMN findings_json TEXT",
    # The drift report, as data. Deterministic findings must not reach the
    # user only through a model's summary of them.
    "ALTER TABLE review_runs ADD COLUMN drift_json TEXT",
    # PR metadata needed by apply-fix to branch off head — captured once
    # at review time so the UI doesn't have to prompt the user.
    "ALTER TABLE review_runs ADD COLUMN pr_head_sha TEXT",
    "ALTER TABLE review_runs ADD COLUMN pr_head_ref TEXT",
    "ALTER TABLE review_runs ADD COLUMN pr_provider TEXT",
    "ALTER TABLE review_runs ADD COLUMN pr_repo TEXT",
    "ALTER TABLE review_runs ADD COLUMN pr_number INTEGER",
    # Stage 21 — full unified diff snapshot so the web UI can render a
    # side-by-side view with inline finding markers without re-fetching
    # from the provider. Capped at persist time (~800 KB).
    "ALTER TABLE review_runs ADD COLUMN raw_diff TEXT",
    # Workspace tenancy — reviews belong to the workspace, not just the
    # triggering user, so every member sees the shared history.
    "ALTER TABLE review_runs ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'",
    "CREATE INDEX IF NOT EXISTS idx_review_runs_ws_started"
    " ON review_runs(workspace_id, started_at DESC)",
    # Which agents ran and which failed. It existed only in memory during a
    # run and reached the user as a prose prefix inside `summary`, so once the
    # review was over nothing — the reviews page, an API consumer, a future
    # single-agent re-run — could say that it was *security* that failed.
    #
    # Nullable with no backfill: every existing row was written before the
    # pipeline recorded this, and inventing '[]' for them would turn "we never
    # knew" into "nothing failed".
    "ALTER TABLE review_runs ADD COLUMN agents_run TEXT",
    "ALTER TABLE review_runs ADD COLUMN agents_failed TEXT",
    # The third state, and it was the missing one. `agents_skipped` — switched
    # off by policy, or the verifier when its LLM veto is disabled — lived on
    # the batch object and died with the process, so a history row showing
    # agents_run without `verifier` was unreadable: switched off, or fell
    # over? The night-bench harness hit exactly this — it could not tell "the
    # operator disabled it" from "dispatch broke" and had to assume the worse.
    # Same nullability contract as its siblings: NULL is "written before the
    # column", [] is "tracked, none skipped".
    "ALTER TABLE review_runs ADD COLUMN agents_skipped TEXT",
    # The comment-cleanup outcome ({deleted, failed, kept_threaded,
    # complete}) as the provider reported it. The providers went to some
    # length to count what their cleanup actually did — and the number died
    # in `provider_response`, which no store column ever received, so the UI
    # could not tell a finished cleanup from one that left last run's
    # comments on the PR. NULL means "never posted / predates the column",
    # which is a different answer from "cleaned and nothing to delete".
    "ALTER TABLE review_runs ADD COLUMN cleanup_json TEXT",
    # Every parameter Celmis changed behind the operator's back during the
    # run, as a JSON array of {agent, parameter, requested, sent, action,
    # reason, model} — see ParameterAdjustment in src/llm/capabilities.py.
    # The pipeline already self-healed four ways (a ceiling clamped, a
    # reasoning word dropped, a temperature dropped, a fallback model called)
    # and recorded each in a different place, none of which this table or
    # the API read; a review that quietly ran without the reasoning level
    # somebody configured looked exactly like one that ran with it.
    #
    # A new column rather than a key inside findings_json or cleanup_json:
    # those are a findings snapshot and a provider's cleanup report, and an
    # adjustment is neither. Nullable with no backfill, same rule as the
    # rosters: NULL is "this row predates the column", '[]' is "recorded, and
    # nothing was adjusted", and reading the first as the second would make
    # every old run claim it sent exactly what was asked.
    "ALTER TABLE review_runs ADD COLUMN adjustments_json TEXT",
    # What the run hid and why — the deny-list's count per rule, the
    # duplicates folded, the claims refused for want of evidence, the
    # veto's drops. Until this column the answer to "where did the other
    # findings go" lived in two log lines; a filter that can only say
    # "dropped 7" is the shape that let the LLM veto delete true
    # positives for five runs while reading as a success. Same NULL rule
    # as adjustments_json: NULL is "this row predates the column".
    "ALTER TABLE review_runs ADD COLUMN hidden_json TEXT",
    # Why the pull request never received the review, when it was supposed to.
    #
    # `posted` is a bool with three meanings — a dry run, a run nobody asked
    # to post, and a run the provider refused all read back as 0 — so the
    # third was invisible. In the runG2 bench, discourse-graphite#18 has
    # posted=0 with status='complete': GitHub answered the review POST 422,
    # the orchestrator turned the PullRequestProviderError into
    # `provider_response={"error": ...}`, and that dict reached the store only
    # for its "cleanup" key. Four findings, none delivered, a green row.
    #
    # NULL is "no delivery failure recorded": posted fine, or was never asked
    # to post, or predates this column. A string is "we were asked to post,
    # the provider refused, and this is what it said" — kept as the provider's
    # own words because "post failed" is not something an operator can act on.
    "ALTER TABLE review_runs ADD COLUMN post_error TEXT",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run additive ALTERs; ignore 'duplicate column name' when already applied."""
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise


def _drift_hits(row: sqlite3.Row) -> int:
    """Number of drift hits in a row, without trusting its shape.

    The column is TEXT holding a JSON report, added by a later migration, so
    it may be absent, NULL, empty or — on a row written by a version that
    shaped it differently — something other than a dict with "hits". None of
    those is worth failing a list request over: the count is a hint for one
    badge and one boolean.
    """
    import json as _json

    try:
        # noqa on purpose: `row` is a sqlite3.Row, not a dict. `x in row`
        # tests the VALUES; only `.keys()` tests the column names, so the
        # "simplification" would silently read the wrong thing.
        if "drift_json" not in row.keys() or not row["drift_json"]:  # noqa: SIM118
            return 0
        report = _json.loads(row["drift_json"])
        hits = report.get("hits") if isinstance(report, dict) else None
        return len(hits) if isinstance(hits, list) else 0
    except Exception:  # noqa: BLE001
        return 0


def _agent_roster(row: sqlite3.Row, column: str) -> list[str] | None:
    """The agent names stored in `column`, or None when they were never recorded.

    None is not []. [] is the pipeline saying "I tracked this and nothing
    failed"; None is "the column did not exist when this row was written".
    Collapsing them would let every review that ran before this shipped read
    back as a clean, complete review — which is the same false negative, moved
    from the verdict into the history.

    Everything else degrades to None rather than raising: the column is TEXT
    added by a later migration, so it can be absent, NULL, empty, or shaped by
    a version that wrote something other than a list of strings, and none of
    that is worth failing a history request over.
    """
    try:
        # noqa on purpose: `row` is a sqlite3.Row, not a dict. `x in row`
        # tests the VALUES; only `.keys()` tests the column names, so the
        # "simplification" would silently read the wrong thing.
        if column not in row.keys() or row[column] is None:  # noqa: SIM118
            return None
        parsed = json.loads(row[column])
    # BLE001 is deliberate: any shape we cannot read is "unknown", and
    # unknown is exactly what None already means here.
    except Exception:  # noqa: BLE001
        return None
    return [str(a) for a in parsed] if isinstance(parsed, list) else None


def _cleanup_report(row: sqlite3.Row) -> dict | None:
    """The stored comment-cleanup outcome, or None when it was never recorded.

    Degrades to None on anything unreadable, for the same reason
    `_agent_roster` does: the column is TEXT added by a later migration, so
    absent / NULL / empty / oddly-shaped rows are all just "unknown", and
    unknown must not fail a history request or dress up as a clean cleanup.
    """
    try:
        # sqlite3.Row again — `x in row` tests the VALUES; see _drift_hits.
        if "cleanup_json" not in row.keys() or not row["cleanup_json"]:  # noqa: SIM118
            return None
        parsed = json.loads(row["cleanup_json"])
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def _adjustments(row: sqlite3.Row) -> list[dict] | None:
    """The stored parameter adjustments, or None when never recorded.

    None is not []. [] is the pipeline saying "I tracked this and changed
    nothing"; None is "the column did not exist when this row was written",
    and collapsing them would let every review that ran before this shipped
    read back as having sent exactly what was configured — the silent
    self-heal, moved from the run into the history.

    Degrades to None on anything unreadable, like `_agent_roster`: the column
    is TEXT added by a later migration, and a history request must not fail
    over a row shaped by another version. Items that are not objects are
    dropped rather than failing the list, for the same reason.
    """
    try:
        # sqlite3.Row again — `x in row` tests the VALUES; see _drift_hits.
        if "adjustments_json" not in row.keys() or row["adjustments_json"] is None:  # noqa: SIM118
            return None
        parsed = json.loads(row["adjustments_json"])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, list):
        return None
    return [a for a in parsed if isinstance(a, dict)]


def adjustments_payload(batch) -> list[dict] | None:
    """What the run row stores for a finished batch's adjustments.

    None — leave the column NULL, "not recorded" — when the batch has no such
    attribute at all: an engine or a test double built before the field
    existed must not be read as "nothing was adjusted". [] when it has the
    attribute and it is empty. Shared by both completion writers (the UI
    trigger's background task and `record_completed_review`), because the two
    have drifted before and a list serialised two ways is how they would
    drift again.
    """
    from src.llm.capabilities import adjustment_as_dict

    raw = getattr(batch, "parameter_adjustments", None)
    if raw is None:
        return None
    return [adjustment_as_dict(a) for a in raw]


def _hidden_report(row: sqlite3.Row) -> dict | None:
    """What the run hid, or None for a row written before it was recorded.

    Degrades to None on anything unreadable, like `_adjustments`, and for
    the same reason: a history request must not fail over a row shaped by
    another version.
    """
    try:
        if "hidden_json" not in row.keys() or row["hidden_json"] is None:  # noqa: SIM118
            return None
        parsed = json.loads(row["hidden_json"])
    except Exception:  # noqa: BLE001
        return None
    return parsed if isinstance(parsed, dict) else None


def hidden_payload(batch) -> dict | None:
    """What the run row stores about the findings this batch hid.

    None — leave the column NULL, "not recorded" — when the batch has no
    deny-list count at all (an engine or a double built before the field
    existed). Otherwise every cause, zeros included: a zero written down
    is "nothing was hidden for this reason", which is the answer the
    operator is looking for. Shared by both completion writers, like
    `adjustments_payload` and for the same reason.
    """
    by_rule = getattr(batch, "dropped_by_rule", None)
    if by_rule is None:
        return None

    def _n(name: str) -> int:
        try:
            return int(getattr(batch, name, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "by_rule": {str(k): int(v) for k, v in dict(by_rule).items()},
        "duplicates": _n("dropped_duplicates"),
        "near_duplicates": _n("dropped_near_duplicates"),
        "low_confidence": _n("dropped_low_confidence"),
        "no_evidence": _n("dropped_no_evidence"),
        "coverage_claim": _n("dropped_coverage_claim"),
        "veto": _n("dropped_by_veto"),
    }


def post_failure(result) -> str | None:
    """The provider's refusal, when the run was asked to post and posted nothing.

    The orchestrator's post step is the only writer of this shape: it catches
    `PullRequestProviderError` and returns `provider_response={"error": ...}`,
    which no store column ever received. So in the runG2 bench,
    discourse-graphite#18 recorded posted=0 / status='complete' after GitHub
    answered the review POST with 422 — four findings produced, none on the
    pull request, and a row that reads exactly like the thirteen that posted.

    None for everything else, deliberately:
      * a dry run — every provider returns `{"dry_run": True, ...}` from a
        branch that runs before any network call, so it cannot carry an
        error; the explicit `dry_run` test below is belt-and-braces against a
        provider that grows a simulation which reports a problem. A dry run
        never posts by design and is not a delivery failure;
      * `post_comments=False`, and the early returns (draft PR, no hunks,
        policy off) — all of which return `provider_response={}`;
      * a result object with no `provider_response` at all, which test
        doubles have shipped without.

    A shared helper, next to `adjustments_payload` and for the reason that one
    gives — two hand-written copies of the same rule are how `cost_usd` came
    to be written by one writer and not the other. Only
    `record_completed_review` calls it today: the UI trigger's writer in
    src/api/routers/reviews.py still passes `batch.run_status.value` straight
    through, so a review triggered from the UI that GitHub refuses still says
    `complete`. That outstanding half is pinned by
    tests/review/test_the_run_record_says_what_it_cost_and_whether_it_arrived.py.
    """
    response = getattr(result, "provider_response", None)
    if not isinstance(response, dict) or response.get("dry_run"):
        return None
    error = response.get("error")
    return str(error) if error else None


def completion_status(batch, post_error: str | None) -> str:
    """The status of a finished run, delivery included.

    `batch.run_status` answers "did every stage answer?" and nothing else, so
    a review that produced findings and could not deliver them came out
    COMPLETE — the word the product uses when it has actually looked
    everywhere and said so.

    A failed delivery makes a COMPLETE run PARTIAL, and PARTIAL is reused
    rather than joined by a new word for a checked reason: `STATUS_BADGES` in
    web/app/(app)/reviews/page.tsx is a fixed map of exactly the six members
    of `ReviewRunStatus`, and its `else` arm drops the lifecycle badge and
    shows the VERDICT instead — so a seventh word would render an undelivered
    review as a bare "COMMENT", the same badge a reviewed-and-posted PR gets.
    A new word would also need a label in sixteen i18n catalogues, which is a
    design decision and not this change's to make.

    What `partial` alone cannot say, `post_error` says, and the two cases an
    operator acts on differently stay apart: PARTIAL with a name in
    `agents_failed` and no `post_error` is a review with a stage missing that
    reached the pull request; PARTIAL with an empty `agents_failed` and a
    `post_error` is a whole review nobody received. Note this widens what
    `ReviewRunStatus.PARTIAL` means — its own docstring still says "every
    comment it did produce was posted", which is now one of two cases.

    FAILED and SKIPPED are left alone. A run in which every agent errored had
    nothing to deliver, and a run in which no stage was dispatched never
    reached the post step; calling either of them "partial" because the post
    failed would upgrade the news.
    """
    from src.review.models import ReviewRunStatus

    status = batch.run_status.value
    if post_error and status == ReviewRunStatus.COMPLETE.value:
        return ReviewRunStatus.PARTIAL.value
    return status


@dataclass
class ReviewRun:
    id: str
    user_id: str
    pr_ref: str
    #: See ReviewRunStatus in src/review/models.py — the same vocabulary,
    #: extended with 'partial' for a review that posted its comments with a
    #: stage missing, and 'skipped' for one in which no stage was ever
    #: dispatched (draft, no hunks, or a policy that disables every agent).
    status: str = "queued"
    verdict: str = "pending"
    findings_count: int = 0
    critical: int = 0
    error_count: int = 0
    warning: int = 0
    info: int = 0
    cross_repo_callers: int = 0
    posted: bool = False
    elapsed_seconds: float | None = None
    summary: str = ""
    error_message: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    # Stage 11 — cost accounting
    cost_usd: float | None = None
    cost_source: str | None = None
    tokens_input: int = 0
    tokens_output: int = 0
    workspace_id: str = "default"
    #: How many cross-repo drift hits this run found.
    #:
    #: Counted here rather than left inside drift_json, because the run LIST
    #: has to know whether there is anything to show without loading every
    #: report. `findings_count` is the model's findings only — a run with
    #: zero of those and a full drift report is exactly the case the panel
    #: below was written for, and the list could not tell it apart from a
    #: run that found nothing at all.
    drift_hits: int = 0
    #: Which agents produced an answer, and which failed to.
    #:
    #: None means "not recorded" — a row written before these columns existed.
    #: [] means "recorded, and none". A caller that needs to know whether the
    #: review had a hole in it must not read None as an empty list; `status`
    #: is the field that answers it for rows that were written since.
    agents_run: list[str] | None = None
    agents_failed: list[str] | None = None
    #: Switched off — by policy, or the verifier with its veto disabled. The
    #: third state that keeps "absent from agents_run" readable: skipped is a
    #: decision, failed is an accident, and a row that cannot tell them apart
    #: reports an operator's choice as an outage.
    agents_skipped: list[str] | None = None
    #: Comment-cleanup outcome from the provider's post step —
    #: {deleted, failed, kept_threaded, complete} — or None when the run
    #: never posted (dry-run, skip, failure) or predates the column. Kept a
    #: plain dict: three providers each build it, and a history request must
    #: not fail because one of them grew a key.
    cleanup: dict | None = None
    #: What Celmis changed between what was asked and what was sent, per
    #: agent — {agent, parameter, requested, sent, action, reason, model}.
    #: None means "not recorded" (a row written before the column, or an
    #: engine that does not report them); [] means "recorded, and nothing
    #: was adjusted". Plain dicts, like `cleanup`, so a history request
    #: cannot 500 over a row another version shaped.
    parameter_adjustments: list[dict] | None = None
    #: What the run hid and why — {by_rule: {rule_id: n}, duplicates,
    #: near_duplicates, low_confidence, no_evidence, veto}. None means
    #: "not recorded" (a row written before the column); a dict of zeros
    #: means "recorded, and nothing was hidden". A plain dict, like the
    #: two above, so history cannot 500 over a row another version shaped.
    hidden: dict | None = None
    #: Why the pull request got no review, when one was supposed to be posted.
    #:
    #: None is "no delivery failure recorded" — it posted, or nobody asked it
    #: to (a dry run, `post_comments=False`), or the row predates the column.
    #: A string is the provider's own refusal. It is NOT `error_message`:
    #: that one means the pipeline raised and produced nothing, while this
    #: means the review exists, is stored in `findings_json`, and never
    #: reached the pull request — two different things to do about it.
    post_error: str | None = None

    @property
    def adjustments_count(self) -> int:
        """How many adjustments the run carries — 0 when none or unknown.

        For the history list, which has to badge a run without shipping the
        list; the detail view reads `parameter_adjustments` itself.
        """
        return len(self.parameter_adjustments or ())

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = datetime.now(UTC).isoformat()


class ReviewRunStore:
    """Single-process SQLite store for review runs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _apply_migrations(conn)
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def insert(self, run: ReviewRun) -> ReviewRun:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO review_runs
                   (id, user_id, pr_ref, status, verdict, findings_count,
                    critical, error_count, warning, info, cross_repo_callers,
                    posted, elapsed_seconds, summary, error_message,
                    started_at, finished_at, workspace_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id, run.user_id, run.pr_ref, run.status, run.verdict,
                    run.findings_count, run.critical, run.error_count,
                    run.warning, run.info, run.cross_repo_callers,
                    int(run.posted), run.elapsed_seconds, run.summary,
                    run.error_message, run.started_at, run.finished_at,
                    run.workspace_id,
                ),
            )
        return run

    def update(
        self, run_id: str, *,
        status: str | None = None,
        verdict: str | None = None,
        findings_count: int | None = None,
        critical: int | None = None,
        error_count: int | None = None,
        warning: int | None = None,
        info: int | None = None,
        cross_repo_callers: int | None = None,
        posted: bool | None = None,
        elapsed_seconds: float | None = None,
        summary: str | None = None,
        error_message: str | None = None,
        finished: bool = False,
        # Stage 11
        cost_usd: float | None = None,
        cost_source: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        # Stage 17
        findings_json: str | None = None,
        drift_json: str | None = None,
        pr_head_sha: str | None = None,
        pr_head_ref: str | None = None,
        pr_provider: str | None = None,
        pr_repo: str | None = None,
        pr_number: int | None = None,
        raw_diff: str | None = None,
        agents_run: list[str] | None = None,
        agents_failed: list[str] | None = None,
        agents_skipped: list[str] | None = None,
        cleanup_json: str | None = None,
        parameter_adjustments: list[dict] | None = None,
        hidden: dict | None = None,
        post_error: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list = []
        for col, val in [
            ("status", status), ("verdict", verdict),
            ("findings_count", findings_count), ("critical", critical),
            ("error_count", error_count), ("warning", warning), ("info", info),
            ("cross_repo_callers", cross_repo_callers),
            ("elapsed_seconds", elapsed_seconds),
            ("summary", summary), ("error_message", error_message),
            ("cost_usd", cost_usd), ("cost_source", cost_source),
            ("tokens_input", tokens_input), ("tokens_output", tokens_output),
            ("findings_json", findings_json),
            ("drift_json", drift_json),
            ("pr_head_sha", pr_head_sha), ("pr_head_ref", pr_head_ref),
            ("pr_provider", pr_provider), ("pr_repo", pr_repo),
            ("pr_number", pr_number), ("raw_diff", raw_diff),
            ("cleanup_json", cleanup_json), ("post_error", post_error),
        ]:
            if val is not None:
                fields.append(f"{col} = ?")
                values.append(val)
        # Serialised separately from the loop above: these are lists, and
        # `[]` is a value we very much want written (it is the difference
        # between "nothing failed" and "nobody looked"), so the None-means-
        # leave-alone test still applies but the binding cannot.
        for col, roster in (("agents_run", agents_run),
                            ("agents_failed", agents_failed),
                            ("agents_skipped", agents_skipped)):
            if roster is not None:
                fields.append(f"{col} = ?")
                values.append(json.dumps([str(a) for a in roster]))
        # Same None-means-leave-alone, []-means-write rule as the rosters, and
        # for the same reason: "[]" is "nothing was adjusted", which is worth
        # writing; NULL stays "nobody recorded it".
        if parameter_adjustments is not None:
            fields.append("adjustments_json = ?")
            values.append(json.dumps(list(parameter_adjustments), ensure_ascii=False))
        if hidden is not None:
            fields.append("hidden_json = ?")
            values.append(json.dumps(dict(hidden), ensure_ascii=False))
        if posted is not None:
            fields.append("posted = ?")
            values.append(int(posted))
        if finished:
            fields.append("finished_at = ?")
            values.append(datetime.now(UTC).isoformat())
        if not fields:
            return
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE review_runs SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def get(self, run_id: str) -> ReviewRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_runs WHERE id = ?", (run_id,),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list_for_user(self, user_id: str, *, limit: int = 50) -> list[ReviewRun]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM review_runs
                   WHERE user_id = ?
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def list_for_workspace(
        self, workspace_id: str, *, user_id: str = "", limit: int = 50,
    ) -> list[ReviewRun]:
        """All runs of a workspace, plus the caller's own legacy rows that
        predate the workspace_id column (stored as 'default')."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM review_runs
                   WHERE workspace_id = ?
                      OR (workspace_id = 'default' AND user_id = ?)
                   ORDER BY started_at DESC
                   LIMIT ?""",
                (workspace_id, user_id, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ReviewRun:
        return ReviewRun(
            id=row["id"],
            user_id=row["user_id"],
            pr_ref=row["pr_ref"],
            status=row["status"],
            verdict=row["verdict"],
            findings_count=row["findings_count"],
            critical=row["critical"],
            error_count=row["error_count"],
            warning=row["warning"],
            info=row["info"],
            cross_repo_callers=row["cross_repo_callers"],
            posted=bool(row["posted"]),
            elapsed_seconds=row["elapsed_seconds"],
            summary=row["summary"] or "",
            error_message=row["error_message"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            # sqlite3.Row again — see the note in _drift_hits.
            cost_usd=row["cost_usd"] if "cost_usd" in row.keys() else None,  # noqa: SIM118
            cost_source=row["cost_source"] if "cost_source" in row.keys() else None,  # noqa: SIM118
            tokens_input=row["tokens_input"] if "tokens_input" in row.keys() else 0,  # noqa: SIM118
            tokens_output=row["tokens_output"] if "tokens_output" in row.keys() else 0,  # noqa: SIM118
            workspace_id=(row["workspace_id"]
                          if "workspace_id" in row.keys() else "default"),  # noqa: SIM118
            drift_hits=_drift_hits(row),
            agents_run=_agent_roster(row, "agents_run"),
            agents_skipped=_agent_roster(row, "agents_skipped"),
            agents_failed=_agent_roster(row, "agents_failed"),
            cleanup=_cleanup_report(row),
            parameter_adjustments=_adjustments(row),
            hidden=_hidden_report(row),
            # sqlite3.Row again — see the note in _drift_hits.
            post_error=(row["post_error"]
                        if "post_error" in row.keys() else None),  # noqa: SIM118
        )


_default_store: ReviewRunStore | None = None


def get_review_run_store() -> ReviewRunStore:
    global _default_store
    if _default_store is None:
        from src.config import get_settings
        s = get_settings()
        db_path = s.workspace_dir / "secrets" / "review_runs.db"
        _default_store = ReviewRunStore(db_path)
    return _default_store


def record_completed_review(result, *, run_id: str, store=None) -> None:
    """Write a finished review into the run store.

    There were two paths into a review and only one of them recorded anything.
    A review triggered from the UI created a row and updated it; a review
    triggered by a webhook or the poller went straight to the orchestrator,
    posted its comments to the pull request, and left no trace — so
    /api/reviews/history was empty, the findings were unreachable after the
    fact, and the cost appeared in no report. That is why the history showed
    zero runs on an installation where auto review had been configured.

    Shared rather than copied, because the shape of a run row is exactly the
    kind of thing two call sites drift apart on: the copy in the UI path grew
    `evidence_kind` and `cross_repo_callers` while the other had nothing at all.
    """
    import json as _json
    import logging

    log = logging.getLogger(__name__)
    store = store or get_review_run_store()
    batch = getattr(result, "batch", None)
    if batch is None:
        log.warning("review_run_not_recorded run=%s reason=no_batch", run_id)
        return

    findings_payload = [
        {
            "id": f.rule_id or f"{f.agent}.{f.file_path}:{f.line}",
            "agent": f.agent,
            "file_path": f.file_path,
            "line": f.line,
            "severity": (f.severity.value if hasattr(f.severity, "value")
                         else str(f.severity)),
            "title": f.title,
            "body": f.body,
            "suggestion": f.suggestion,
            "rule_id": f.rule_id,
            "confidence": f.confidence,
            # The sentence the evidence gate exists to force, and the one
            # the bench judge most often matches on: 43 of 51 extracted
            # candidates were lexically closer to the comment's first
            # `*Why:*` line than to the whole rest of the body. It was
            # dropped here while `evidence_kind` — HOW the finding was
            # arrived at — was kept, so the history page, every report and
            # any reconstruction of a review that failed to post showed a
            # finding with no reason attached. Measured on a full run of
            # the benchmark: 0 of 63 stored findings carried it.
            "reasoning": getattr(f, "reasoning", "") or "",
            # How it was arrived at, which is a different question from how
            # sure we are. A float cannot carry both.
            "evidence_kind": getattr(f, "evidence_kind", "inferred"),
        }
        for f in batch.findings
    ]
    # The cleanup outcome rides in `provider_response`, not on the batch —
    # only a run that posted has one, and only a dict counts as one: an
    # {"error": ...} response or a dry-run's {} must stay NULL ("never
    # cleaned"), not become an empty report that reads as a clean cleanup.
    cleanup = getattr(result, "provider_response", None)
    cleanup = cleanup.get("cleanup") if isinstance(cleanup, dict) else None
    # Same dict, the other key. The post error sat next to the cleanup report
    # the whole time and only the cleanup report was ever read out of it.
    post_error = post_failure(result)
    store.update(
        run_id,
        # Not the literal "complete". A run whose security agent never
        # answered posted its comments and is still missing a stage, and this
        # row is the only place that can say so once the PR comment has been
        # read. `run_status` is the same property the summary banner is built
        # from, so the two cannot disagree — and `completion_status` adds the
        # half `run_status` cannot see, which is whether anything arrived.
        status=completion_status(batch, post_error),
        post_error=post_error,
        agents_run=list(batch.agents_run),
        agents_failed=list(batch.agents_failed),
        agents_skipped=list(getattr(batch, "agents_skipped", []) or []),
        verdict=batch.verdict.value,
        findings_count=len(batch.findings),
        critical=batch.critical_count,
        error_count=batch.error_count,
        warning=batch.warning_count,
        info=batch.info_count,
        cross_repo_callers=batch.cross_repo_callers,
        posted=bool(getattr(result, "posted", False)),
        elapsed_seconds=batch.elapsed_seconds,
        summary=(batch.summary or "")[:500],
        finished=True,
        findings_json=_json.dumps(findings_payload, ensure_ascii=False),
        cleanup_json=(_json.dumps(cleanup) if isinstance(cleanup, dict) else None),
        # What the run changed behind the operator's back — the same list
        # the banner in `summary` was built from, stored as data so the page
        # can say which knob to turn instead of paraphrasing a sentence.
        parameter_adjustments=adjustments_payload(batch),
        hidden=hidden_payload(batch),
        # The money, which this writer never passed on. Every one of the 14
        # rows the runG2 bench left behind reads cost_usd NULL and
        # tokens_in/tokens_out 0 — for runs that carry up to 13 findings from
        # a roster of six agents, so the tokens were spent and the aggregation
        # loop in the orchestrator had already counted them onto the batch.
        # Only the queue path is affected: the UI trigger's writer in
        # src/api/routers/reviews.py has passed these four all along, which is
        # what makes this the same drift the docstring above already records.
        #
        # `batch.cost_usd` stays None when any agent ran on a model with no
        # price, and None means "leave the column alone" in `store.update`, so
        # an unknown total writes NULL — NOT 0.0. Zero is a claim that the run
        # was free, and this run was not. What keeps NULL-because-unknown
        # apart from NULL-because-nobody-recorded-it is `cost_source`, which
        # the batch always sets to a non-empty word ('unknown' | 'mixed' |
        # the one source it saw): a recorded run has a source whatever its
        # total, a row nobody wrote has neither. A genuinely free run writes
        # 0.0, which `store.update` persists because 0.0 is not None.
        #
        # getattr, not attribute access: `record_completed_review` is driven
        # by test doubles and by an engine that may predate a field, and a
        # missing cost attribute must degrade to "not recorded" rather than
        # fail the whole run out of the history.
        cost_usd=getattr(batch, "cost_usd", None),
        cost_source=getattr(batch, "cost_source", None) or None,
        tokens_input=int(getattr(batch, "tokens_in", 0) or 0),
        tokens_output=int(getattr(batch, "tokens_out", 0) or 0),
    )
