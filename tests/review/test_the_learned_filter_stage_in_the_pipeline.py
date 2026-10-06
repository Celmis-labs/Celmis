"""The orchestrator's learned-filter stage, and what the run record says of it.

  * `off` leaves no trace: no stage, nothing read;
  * `shadow` (the built-in) keeps every finding and says how many it would
    have left out, by name, in the run's hidden report;
  * `on` leaves the dismissed ones out, says so in a summary section for the
    comment and records the count;
  * a failing filter keeps every finding and says so in its stage;
  * a provider that lists reactions is asked once per finding comment of the
    pull request before the decision.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.review_runs import hidden_payload
from src.review.learning import signals as sig
from src.review.learning import similarity, suppress
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest, ReviewBatch
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import get_review_settings
from src.review.stages import StageRecorder
from tests.review.learning_db import FakeQdrant, hash_embed
from tests.review.rules_db import rules_db

WS = "ws-a"


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/shop", number=7, title="t", description="d",
        author="alice", base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="src/foo.py", old_file_path="src/foo.py", old_start=1,
                    old_count=1, new_start=1, new_count=2, content="@@ -1 +1,2 @@\n line\n+x\n")])


def _finding(title="Possible null dereference", line=1, severity=FindingSeverity.WARNING):
    return Finding(agent="defect", file_path="src/foo.py", line=line, severity=severity,
                   title=title, body="b", confidence=0.9, reasoning="because",
                   rule_id="defect.null")


def _orch() -> ReviewOrchestrator:
    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    orch.settings = get_review_settings()
    return orch


@pytest.fixture(autouse=True)
def _vectors(monkeypatch):
    monkeypatch.setattr(similarity, "_qdrant", lambda client=None: FakeQdrant())
    monkeypatch.setattr(similarity, "_embed", lambda texts, ws: hash_embed(texts))


def _run(mode, findings, *, provider=None):
    pr = _pr()
    batch = ReviewBatch(pull_request=pr, findings=list(findings))
    stages = StageRecorder()
    kept = _orch()._learned_filter(
        batch, list(findings), policy={"learning_suppression": mode, "workspace_id": WS},
        workspace_id=WS, pr=pr, stages=stages, provider=provider,
        provider_name="github" if provider is not None else "")
    return kept, batch, stages


def _teach(finding):
    sig.record_verdict(
        WS, _pr().local_slug, sig.snapshot_of(finding), "dismissed", "reply",
        pr=sig.PRRef("github", "acme/shop", 3), actor="jane")


async def test_off_leaves_no_stage_and_reads_nothing(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not be read")

    monkeypatch.setattr(suppress, "apply", boom)
    f = _finding()
    kept, _batch, stages = _run("off", [f])
    assert kept == [f] and stages.get("learned_filter") is None


async def test_shadow_keeps_everything_and_reports_what_it_would_have_left_out(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        seen, fresh = _finding(), _finding("Another thing entirely", line=5)
        _teach(seen)
        kept, batch, stages = _run("shadow", [seen, fresh])
        assert kept == [seen, fresh]
        assert (batch.dropped_by_feedback, batch.would_drop_by_feedback) == (0, 1)
        stage = stages.get("learned_filter")
        assert stage["status"] == "success" and "Shadow mode" in stage["reason"]
        hidden = hidden_payload(_with_counts(batch))
        assert hidden["learned"] == 0 and hidden["learned_would_hide"] == 1
        assert hidden["learned_items"][0]["title"] == "Possible null dereference"
        assert not any(s.key == "learned" for s in batch.summary_sections)


async def test_on_leaves_the_dismissed_out_and_says_so(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        seen, fresh = _finding(), _finding("Another thing entirely", line=5)
        _teach(seen)
        kept, batch, stages = _run("on", [seen, fresh])
        assert kept == [fresh] and batch.dropped_by_feedback == 1
        assert stages.get("learned_filter")["meta"]["hidden"] == 1
        [section] = [s for s in batch.summary_sections if s.key == "learned"]
        assert "1" in section.markdown and section.order == 600 and "comment" in section.targets
        assert hidden_payload(_with_counts(batch))["learned"] == 1
        assert "learned_would_hide" not in hidden_payload(_with_counts(batch))


async def test_a_critical_finding_is_kept_even_when_it_was_dismissed(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        crit = _finding(severity=FindingSeverity.CRITICAL)
        _teach(crit)
        kept, batch, _stages = _run("on", [crit])
        assert kept == [crit] and batch.dropped_by_feedback == 0


async def test_nothing_dismissed_yet_is_a_skipped_stage(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        kept, _batch, stages = _run("on", [_finding()])
        assert len(kept) == 1 and stages.get("learned_filter")["status"] == "skipped"


async def test_a_failing_filter_keeps_every_finding_and_says_so(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(suppress, "apply", boom)
    f = _finding()
    kept, _batch, stages = _run("on", [f])
    assert kept == [f] and stages.get("learned_filter")["status"] == "failed"


async def test_the_thumbs_are_read_before_the_decision(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        f = _finding()
        sig.record_posted(WS, sig.PRRef("github", "acme/shop", 7), [f],
                          [{"comment_id": 901, "fingerprint": sig.snapshot_of(f).fingerprint[:16]}])
        prov = SimpleNamespace(calls=[], actor_permission=lambda *a, **k: "write",
                               pr_participants=lambda *a: frozenset())
        prov.list_comment_reactions = lambda repo, n, cid: (
            prov.calls.append(cid) or [("jane", "down")])
        kept, batch, _stages = _run("on", [f], provider=prov)
        assert prov.calls == ["901"]
        assert kept == [] and batch.dropped_by_feedback == 1, (
            "a thumbs-down given since the last review already counts")


def _with_counts(batch):
    """`hidden_payload` answers only for a batch that carries the deny-list
    counts; a fresh one has them as the empty dict."""
    if getattr(batch, "dropped_by_rule", None) is None:
        batch.dropped_by_rule = {}
    return batch
