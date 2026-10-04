"""A review run says how it got where it ended, stage by stage.

A run row used to say only how a review ENDED. A skip for a branch mismatch
read exactly like a skip for a draft; a failure did not say whether it fell
over fetching the pull request or posting the comments; and a skip decided
before the pipeline started (auto-review off, a draft at the webhook) left no
row at all, so the pull-requests page could not say anything. Pinned here:

  * the orchestrator records every stage in order — fetch, settings, each
    gate, context, each agent with its model/tokens/findings, verifier,
    summary, publish — and the writer adds "record" and "finished";
  * a closed gate is a SKIPPED stage whose reason names the exact values
    ("target branch 'master' does not match configured patterns [...]"),
    and the finished stage repeats it;
  * a crash fails the stage that was running, with a curated sentence —
    never the exception's text, which can carry a token;
  * the stages are bounded and scrubbed, persisted on the run row by an
    additive migration, and read back defensively;
  * the queue path, the webhook's own skips and a duplicate request each
    leave a run with its reason.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.api import review_runs as runs_mod
from src.api.review_runs import (
    ReviewRun,
    ReviewRunStore,
    finish_stages,
    record_completed_review,
    run_stage_sink,
)
from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Finding, FindingSeverity
from src.review.orchestrator import ReviewOrchestrator
from src.review.providers.base import PullRequestProviderError
from src.review.stages import (
    MAX_REASON,
    MAX_STAGES,
    StageRecorder,
    normalize_stage,
    parse_stages,
    scrub,
    status_reason,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    _PassThroughVerifier,
    _pr,
    env,  # noqa: F401 — the fixture, imported for its name
)

# ─── doubles ──────────────────────────────────────────────────────────


class _Provider:
    name = "github"

    def __init__(self, pr, *, fetch_raises=None, post_raises=None) -> None:
        self.pr = pr
        self.fetch_raises = fetch_raises
        self.post_raises = post_raises
        self.closed = False

    def fetch_pull_request(self, repo, number):
        if self.fetch_raises is not None:
            raise self.fetch_raises
        return self.pr

    def post_review(self, batch, dry_run=False):
        if self.post_raises is not None:
            raise self.post_raises
        return {"dry_run": True} if dry_run else {"cleanup": {"complete": True}}

    def close(self) -> None:
        self.closed = True


class _Agent(ReviewAgent):
    def __init__(self, name: str, findings=(), *, error=None, model="gemini-x",
                 tokens=(1200, 300)) -> None:
        self.name = name
        self._findings = list(findings)
        self._error = error
        self._model = model
        self._tokens = tokens

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(
            agent=self.name, findings=list(self._findings), error=self._error,
            tokens_in=self._tokens[0], tokens_out=self._tokens[1],
            model_used=self._model, elapsed_seconds=1.25,
        )


def _finding() -> Finding:
    return Finding(file_path="src/mod0.py", line=2, severity=FindingSeverity.ERROR,
                   title="Cache never expires", body="because", agent="defect",
                   rule_id="defect.rule", reasoning="seen in the diff")


POLICY = {
    "enabled": True, "target_branches": [], "disabled_agents": [],
    "folder_rules": [], "ignore_globs": [],
}


def _orch(monkeypatch, agents, policy=None) -> ReviewOrchestrator:
    orch = ReviewOrchestrator(agents=agents, verifier=_PassThroughVerifier())
    monkeypatch.setattr(orch, "_resolved_policy", lambda slug, ws: policy)
    monkeypatch.setattr(orch, "_build_context",
                        lambda pr, **kw: AgentContext(pull_request=pr, llm_client=None))
    return orch


def _keys(rec: StageRecorder) -> list[str]:
    return [s["key"] for s in rec.snapshot()]


def _stage(rec: StageRecorder, key: str) -> dict:
    found = rec.get(key)
    assert found is not None, f"no {key!r} stage in {_keys(rec)}"
    return found


@pytest.fixture
def store(tmp_path, monkeypatch) -> ReviewRunStore:
    s = ReviewRunStore(tmp_path / "runs.db")
    monkeypatch.setattr(runs_mod, "_default_store", s)
    return s


# ─── the orchestrator's stages ───────────────────────────────────────


def test_a_branch_mismatch_is_a_skipped_stage_naming_the_patterns(env, monkeypatch):  # noqa: F811
    pr = _pr()
    pr.base_ref = "master"
    orch = _orch(monkeypatch, [_Agent("defect")],
                 policy={**POLICY, "target_branches": ["main", "release/*"]})
    rec = StageRecorder()

    result = orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec)

    assert result.batch.run_status.value == "skipped"
    assert _keys(rec) == ["fetch_pr", "settings", "ignore_globs", "gate_enabled",
                          "gate_target_branch"]
    gate = _stage(rec, "gate_target_branch")
    assert gate["status"] == "skipped"
    assert ("target branch 'master' does not match configured patterns "
            "['main', 'release/*']") in gate["reason"]
    assert gate["reason"].startswith("Branch mismatch")
    # Nothing after the gate ran, and the finished stage repeats the reason.
    finish_stages(rec, "skipped")
    fin = rec.snapshot()[-1]
    assert fin["key"] == "finished" and fin["status"] == "skipped"
    assert fin["reason"].startswith("Skipped — Branch mismatch")


def test_a_matching_branch_says_which_pattern_it_matched(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect")],
                 policy={**POLICY, "target_branches": ["main"]})
    rec = StageRecorder()
    orch.review("github", "o/r", 1, provider=_Provider(_pr()), stages=rec,
                post_comments=False)
    gate = _stage(rec, "gate_target_branch")
    assert gate["status"] == "success"
    assert "'main' matches configured patterns ['main']" in gate["reason"]


def test_a_full_review_names_every_stage_in_order(env, monkeypatch, store):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])], policy=POLICY)
    store.insert(ReviewRun(id="r1", user_id="u", pr_ref="github:o/r#1",
                           workspace_id="ws"))
    rec = StageRecorder(sink=run_stage_sink("r1", store))
    monkeypatch.setattr("src.review.issues.record_review_run", lambda *a, **kw: True)

    result = orch.review("github", "o/r", 1, provider=_Provider(_pr()), stages=rec)
    record_completed_review(result, run_id="r1", store=store, workspace_id="ws",
                            stages=rec)

    assert _keys(rec) == [
        "fetch_pr", "settings", "ignore_globs", "gate_enabled",
        "gate_target_branch", "context", "gate_draft", "gate_size", "gate_hunks",
        "summary", "agent:defect", "verifier", "breaking_change", "compliance",
        "publish", "record", "finished",
    ]
    agent = _stage(rec, "agent:defect")
    assert agent["status"] == "success"
    assert agent["meta"] == {"model": "gemini-x", "tokens_in": 1200,
                             "tokens_out": 300, "findings": 1}
    assert agent["duration_ms"] == 1250
    assert _stage(rec, "publish")["status"] == "success"
    assert _stage(rec, "record")["status"] == "success"
    fin = _stage(rec, "finished")
    assert fin["status"] == "success" and fin["reason"].startswith("Complete — 1 finding")
    assert all(s["status"] != "running" for s in rec.snapshot())
    assert all(s["started_at"] for s in rec.snapshot())

    # Persisted on the row by the sink, and served back the same.
    row = store.get("r1")
    assert [s["key"] for s in row.stages] == _keys(rec)
    assert row.status == "complete"
    assert row.status_reason.startswith("Complete")


def test_a_switched_off_agent_is_skipped_not_failed(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect"), _Agent("security")],
                 policy={**POLICY, "disabled_agents": ["security"]})
    rec = StageRecorder()
    orch.review("github", "o/r", 1, provider=_Provider(_pr()), stages=rec,
                post_comments=False)
    sec = _stage(rec, "agent:security")
    assert sec["status"] == "skipped"
    assert "policy" in sec["reason"]
    assert _stage(rec, "agent:defect")["status"] == "success"
    assert _stage(rec, "publish")["status"] == "skipped"


def test_a_failed_agent_says_so_without_its_error_text(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [
        _Agent("defect", [_finding()]),
        _Agent("security", error="401 for key sk-live-abcdefghijklmnopqrstuvwx"),
    ], policy=POLICY)
    rec = StageRecorder()
    orch.review("github", "o/r", 1, provider=_Provider(_pr()), stages=rec,
                post_comments=False)
    sec = _stage(rec, "agent:security")
    assert sec["status"] == "failed"
    assert "sk-live" not in json.dumps(rec.snapshot())


def test_a_crash_fails_the_running_stage_with_a_curated_sentence(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect")], policy=POLICY)
    rec = StageRecorder()
    boom = RuntimeError("GET https://x:ghp_abcdefghijklmnopqrstuv@api.github.com 500")
    with pytest.raises(RuntimeError):
        orch.review("github", "o/r", 1,
                    provider=_Provider(_pr(), fetch_raises=boom), stages=rec)
    fetch = _stage(rec, "fetch_pr")
    assert fetch["status"] == "failed"
    assert "RuntimeError" in fetch["reason"]
    assert "ghp_" not in json.dumps(rec.snapshot())


def test_a_refused_post_is_a_failed_publish_stage(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])], policy=POLICY)
    rec = StageRecorder()
    refused = PullRequestProviderError("422 {'message': 'token ghp_zzzzzzzzzzzzzzzz'}")
    orch.review("github", "o/r", 1,
                provider=_Provider(_pr(), post_raises=refused), stages=rec)
    pub = _stage(rec, "publish")
    assert pub["status"] == "failed"
    assert "refused" in pub["reason"]
    assert "ghp_" not in pub["reason"]


@pytest.mark.parametrize("pr_kw,gate,word", [
    ({"draft": True}, "gate_draft", "Draft"),
    ({"hunks": []}, "gate_hunks", "No reviewable changes"),
])
def test_every_early_skip_names_its_gate(env, monkeypatch, pr_kw, gate, word):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect")], policy=POLICY)
    rec = StageRecorder()
    orch.review("github", "o/r", 1, provider=_Provider(_pr(**pr_kw)), stages=rec)
    last = rec.snapshot()[-1]
    assert last["key"] == gate and last["status"] == "skipped"
    assert last["reason"].startswith(word)
    assert rec.outcome_reason == last["reason"]


def test_a_draft_reviewed_on_purpose_says_so(env, monkeypatch):  # noqa: F811
    orch = _orch(monkeypatch, [_Agent("defect")],
                 policy={**POLICY, "run_on_drafts": True})
    rec = StageRecorder()
    result = orch.review("github", "o/r", 1, provider=_Provider(_pr(draft=True)),
                         stages=rec, post_comments=False)
    gate = _stage(rec, "gate_draft")
    assert gate["status"] == "success" and "run_on_drafts" in gate["reason"]
    assert result.batch.run_status.value != "skipped"
    assert _stage(rec, "agent:defect")["status"] == "success"


# ─── the recorder ────────────────────────────────────────────────────


def test_reasons_are_scrubbed_and_bounded():
    dirty = ("failed for https://bob:hunter2@gitlab.com/x with glpat-abcdefghijklmnop "
             "and Bearer abcdefghijklmnop123 token=s3cr3t-value") + "x" * 1000
    out = scrub(dirty)
    for leaked in ("hunter2", "glpat-", "abcdefghijklmnop123", "s3cr3t"):
        assert leaked not in out
    assert len(out) <= MAX_REASON


def test_the_stage_list_is_bounded_and_keeps_its_last_word():
    rec = StageRecorder()
    for i in range(MAX_STAGES + 20):
        rec.add(f"s{i}", "Stage", "success", "ok")
    rec.finish("complete", "done")
    snap = rec.snapshot()
    assert len(snap) == MAX_STAGES
    assert snap[-1]["key"] == "finished"


def test_an_unknown_status_reads_as_failed_never_success():
    assert normalize_stage({"key": "x", "status": "great"})["status"] == "failed"
    assert normalize_stage({"status": "success"}) is None
    assert parse_stages("not json") is None
    assert parse_stages(None) is None
    assert parse_stages(json.dumps({"key": "x"})) is None
    assert parse_stages(json.dumps([{"key": "a", "status": "success"}, 7]))[0]["key"] == "a"


def test_finish_closes_what_was_left_running():
    rec = StageRecorder()
    rec.begin("publish", "Publish to provider")
    rec.finish("failed", "Failed — boom")
    pub = rec.get("publish")
    assert pub["status"] == "failed" and pub["duration_ms"] is not None
    assert rec.get("finished")["status"] == "failed"


def test_a_sink_that_breaks_never_breaks_the_review():
    def _sink(_):
        raise sqlite3.OperationalError("database is locked")

    rec = StageRecorder(sink=_sink)
    rec.add("received", "Review started", "success", "ok")
    assert _keys(rec) == ["received"]


def test_the_reason_of_a_legacy_row_comes_from_its_summary():
    assert status_reason(None, status="skipped",
                         summary="Review skipped — base branch 'x' is not in …\nmore") \
        == "Review skipped — base branch 'x' is not in …"
    assert status_reason(None, status="complete", summary="all good") is None


# ─── the store ───────────────────────────────────────────────────────


def test_the_column_is_added_to_an_old_database(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE review_runs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, pr_ref TEXT NOT NULL,
                status TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT 'pending',
                findings_count INTEGER NOT NULL DEFAULT 0,
                critical INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning INTEGER NOT NULL DEFAULT 0, info INTEGER NOT NULL DEFAULT 0,
                cross_repo_callers INTEGER NOT NULL DEFAULT 0,
                posted INTEGER NOT NULL DEFAULT 0, elapsed_seconds REAL,
                summary TEXT NOT NULL DEFAULT '', error_message TEXT,
                started_at TEXT NOT NULL, finished_at TEXT);
            INSERT INTO review_runs (id, user_id, pr_ref, status, started_at)
            VALUES ('old', 'u', 'github:o/r#1', 'skipped', '2026-01-01T00:00:00+00:00');
        """)
    store = ReviewRunStore(db)
    ReviewRunStore(db)  # idempotent
    row = store.get("old")
    assert row.stages is None, "a row written before the column is 'not recorded'"
    store.set_stages("old", [{"key": "finished", "name": "Finished",
                              "status": "skipped", "reason": "Skipped — x"}])
    assert store.get("old").status_reason == "Skipped — x"


def test_a_prs_runs_are_found_by_coordinates_and_by_ref(store):
    store.insert(ReviewRun(id="a", user_id="u", pr_ref="github:o/r#1",
                           workspace_id="ws", started_at="2026-01-01T00:00:00+00:00"))
    store.insert(ReviewRun(id="b", user_id="u", pr_ref="https://github.com/o/r/pull/1",
                           workspace_id="ws", pr_provider="github", pr_repo="o/r",
                           pr_number=1, started_at="2026-01-02T00:00:00+00:00"))
    store.insert(ReviewRun(id="c", user_id="u", pr_ref="github:o/r#1",
                           workspace_id="other", pr_provider="github", pr_repo="o/r",
                           pr_number=1))
    assert [r.id for r in store.list_for_pr("ws", "github", "o/r", 1)] == ["b", "a"]
    latest = store.latest_for_prs("ws", "github", "o/r", [1, 2])
    assert set(latest) == {1} and latest[1].id == "b"


# ─── the queue path ──────────────────────────────────────────────────


@pytest.fixture
def queue(monkeypatch):
    import src.sync.queue as q

    jobs: list[dict] = []

    def _enqueue(**kw):
        if any(j["dedup_key"] == kw.get("dedup_key") for j in jobs):
            return None
        jobs.append(kw)
        return f"job-{len(jobs)}"

    monkeypatch.setattr(q, "enqueue", _enqueue)
    return jobs


@pytest.fixture
def no_ledger(monkeypatch):
    import src.review.issues as issues_mod

    calls: list[dict] = []
    monkeypatch.setattr(issues_mod, "record_review_run", lambda *a, **kw: True)
    monkeypatch.setattr(issues_mod, "record_failed_review",
                        lambda **kw: calls.append({"failed": kw}))
    monkeypatch.setattr(issues_mod, "record_unreviewed_run",
                        lambda **kw: calls.append({"unreviewed": kw}) or True)
    return calls


def _wire(monkeypatch, orch, provider) -> None:
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", lambda: orch)
    monkeypatch.setattr(providers_mod, "get_provider_for", lambda *a, **kw: provider)


def test_a_queued_review_records_its_wait_and_every_stage(
    env, monkeypatch, store, queue, no_ledger,  # noqa: F811
):
    from src.review.dispatch import enqueue_review_run, execute_review

    pr = _pr()
    pr.base_ref = "master"
    _wire(monkeypatch, _orch(monkeypatch, [_Agent("defect")],
                             policy={**POLICY, "target_branches": ["main"]}),
          _Provider(pr))

    res = enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws",
                             source="manual")
    assert res.status == "queued"
    row = store.get(res.run_id)
    assert row.status == "queued" and row.pr_number == 1
    assert [(s["key"], s["status"]) for s in row.stages] == [
        ("received", "success"), ("queued", "running")]
    assert queue[0]["payload"]["run_id"] == res.run_id

    execute_review(queue[0]["payload"])

    row = store.get(res.run_id)
    assert row.status == "skipped"
    keys = [s["key"] for s in row.stages]
    assert keys[:3] == ["received", "queued", "fetch_pr"]
    assert keys[-3:] == ["gate_target_branch", "record", "finished"]
    assert row.stages[1]["status"] == "success"
    assert row.status_reason.startswith("Skipped — Branch mismatch: target branch "
                                        "'master' does not match configured patterns "
                                        "['main']")


def test_a_second_request_for_a_queued_pr_is_answered_not_dropped(
    store, queue, no_ledger,
):
    from src.review.dispatch import enqueue_review_run

    first = enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws")
    second = enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws")
    assert first.status == "queued" and second.status == "duplicate"
    row = store.get(second.run_id)
    assert row.status == "skipped" and "already queued" in row.status_reason


def test_no_queue_means_the_caller_runs_it_inline(store, monkeypatch):
    import src.sync.queue as q
    from src.review.dispatch import enqueue_review_run

    def _down(**kw):
        raise OSError("connection refused")

    monkeypatch.setattr(q, "enqueue", _down)
    res = enqueue_review_run("bitbucket", "acme/api", 3, user_id="u", workspace_id="ws")
    assert res.status == "inline" and res.payload["run_id"] == res.run_id


def test_a_missing_connection_fails_the_run_instead_of_leaving_it_running(
    store, queue, no_ledger, monkeypatch,
):
    import src.review.providers as providers_mod
    from src.review.dispatch import enqueue_review_run, execute_review

    def _no_creds(*a, **kw):
        raise PullRequestProviderError("No Bitbucket credentials saved for user 'u'")

    monkeypatch.setattr(providers_mod, "get_provider_for", _no_creds)
    res = enqueue_review_run("bitbucket", "acme/api", 3, user_id="u", workspace_id="ws")
    with pytest.raises(PullRequestProviderError):
        execute_review(queue[0]["payload"])
    row = store.get(res.run_id)
    assert row.status == "failed"
    fetch = next(s for s in row.stages if s["key"] == "fetch_pr")
    assert fetch["status"] == "failed" and "Bitbucket" in fetch["reason"]
    assert row.stages[-1]["key"] == "finished"
    assert no_ledger and "failed" in no_ledger[-1]


def test_a_job_queued_by_an_older_version_still_gets_a_row(
    env, monkeypatch, store, no_ledger,  # noqa: F811
):
    from src.review.dispatch import execute_review

    _wire(monkeypatch, _orch(monkeypatch, [_Agent("defect")], policy=POLICY),
          _Provider(_pr()))
    execute_review({"provider": "github", "repo": "o/r", "pr_number": 1,
                    "post_comments": False, "user_id": "u", "workspace_id": "ws",
                    "source": "webhook", "enqueued_at": "2026-10-04T10:00:00+00:00"})
    (row,) = store.list_for_pr("ws", "github", "o/r", 1)
    assert row.status == "complete"
    assert [s["key"] for s in row.stages][:2] == ["received", "queued"]
    assert "webhook" in row.stages[0]["reason"]


# ─── the webhook's own skips ─────────────────────────────────────────


@pytest.fixture
def bound(tmp_path, monkeypatch):
    import src.api.auto_review as ar
    from src.api.auto_review import AutoReviewStore
    from tests.review.test_a_webhook_runs_as_the_repos_owner import cfg

    s = AutoReviewStore(tmp_path / "auto_review.db")
    monkeypatch.setattr(ar, "get_auto_review_store", lambda: s)
    return s, cfg


def test_auto_review_off_is_recorded_once_with_its_reason(bound, store, queue, no_ledger):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg(enabled=False))
    for _ in range(3):  # one row per answer, not one per delivery
        asyncio.run(_dispatch_review("github", "acme/payments", 7,
                                     expected_workspace_id="ws-1"))
    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 7)
    assert row.status == "skipped"
    assert row.status_reason == ("Skipped — Auto-review disabled: automatic review "
                                 "is switched off for this repository.")
    assert [s["key"] for s in row.stages] == ["received", "gate_enabled", "finished"]
    assert sum("unreviewed" in c for c in no_ledger) == 1


def test_a_draft_at_the_webhook_is_recorded(bound, store, queue, no_ledger):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    asyncio.run(_dispatch_review("github", "acme/payments", 8,
                                 expected_workspace_id="ws-1", skip_reason="draft",
                                 pr_meta={"title": "WIP", "base_ref": "main"}))
    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 8)
    assert row.status_reason.startswith("Skipped — Draft")
    assert no_ledger[-1]["unreviewed"]["title"] == "WIP"


def test_a_foreign_delivery_records_nothing(bound, store, queue, no_ledger):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg(enabled=False))
    asyncio.run(_dispatch_review("github", "acme/payments", 7,
                                 expected_workspace_id="ws-2"))
    assert store.list_for_pr("ws-1", "github", "acme/payments", 7) == []
    assert no_ledger == []


def test_a_webhook_job_carries_who_asked_and_when(bound, store, queue):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    asyncio.run(_dispatch_review("github", "acme/payments", 7,
                                 expected_workspace_id="ws-1"))
    payload = queue[0]["payload"]
    assert payload["source"] == "webhook" and payload["enqueued_at"]


def test_the_ui_trigger_records_its_stages(env, monkeypatch, store):  # noqa: F811
    import src.api.routers.reviews as reviews_mod

    monkeypatch.setattr(reviews_mod, "get_review_run_store", lambda: store)
    monkeypatch.setattr("src.review.issues.record_review_run", lambda *a, **kw: True)
    _wire(monkeypatch, _orch(monkeypatch, [_Agent("defect", [_finding()])],
                             policy=POLICY), _Provider(_pr()))
    run = reviews_mod._new_manual_run("github:o/r#1", user_id="u", workspace_id="ws")
    reviews_mod._run_review_task(pr_ref="github:o/r#1", post_comments=False,
                                 run_id=run.id, user_id="u", workspace_id="ws")
    row = store.get(run.id)
    keys = [s["key"] for s in row.stages]
    assert keys[0] == "received" and keys[-2:] == ["record", "finished"]
    assert "agent:defect" in keys
    assert row.pr_provider == "github" and row.pr_number == 1
    assert SimpleNamespace(**reviews_mod._run_to_out(row).model_dump()).stages
