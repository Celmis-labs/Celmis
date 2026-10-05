"""A PR into a branch the target patterns leave out is recorded as skipped at
the door (webhook, poller) and left out of manual bulk review — not queued as
a job that would only end at the orchestrator's gate.

The orchestrator gate stays the authority: with no base branch in the
delivery, or no patterns, nothing is pre-skipped.
"""

from __future__ import annotations

import asyncio

from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    bound,
    no_ledger,
    queue,
    store,
)


def _patterns(monkeypatch, patterns):
    import src.review.review_defaults as rd

    monkeypatch.setattr(rd, "target_branches_for_repo", lambda provider, repo: patterns)


def test_a_ready_pr_into_an_untargeted_branch_is_recorded_not_queued(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    _patterns(monkeypatch, ["staging"])
    asyncio.run(_dispatch_review("github", "acme/payments", 11,
                                 expected_workspace_id="ws-1",
                                 pr_meta={"base_ref": "main"}))
    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 11)
    assert row.status == "skipped"
    assert row.status_reason.startswith("Skipped — Branch mismatch")
    assert [st["key"] for st in row.stages][-2] == "gate_target_branch"


def test_a_targeted_branch_a_missing_base_or_no_patterns_still_queues(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    from src.review.webhook import _dispatch_review

    s, cfg = bound
    s.upsert(cfg())
    _patterns(monkeypatch, ["staging"])
    asyncio.run(_dispatch_review("github", "acme/payments", 12,
                                 expected_workspace_id="ws-1",
                                 pr_meta={"base_ref": "staging"}))
    asyncio.run(_dispatch_review("github", "acme/payments", 13,
                                 expected_workspace_id="ws-1"))  # base unknown
    _patterns(monkeypatch, [])
    asyncio.run(_dispatch_review("github", "acme/payments", 14,
                                 expected_workspace_id="ws-1",
                                 pr_meta={"base_ref": "main"}))
    assert [q["payload"]["pr_number"] for q in queue] == [12, 13, 14]


def test_the_gitlab_poller_pre_skips_an_untargeted_mr(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    from src.review import poller

    s, cfg = bound
    c = cfg()
    s.upsert(c)
    _patterns(monkeypatch, ["staging"])
    mr = {"iid": 5, "title": "T", "target_branch": "main", "source_branch": "f",
          "author": {"username": "dana"}, "web_url": "https://x/5"}
    assert poller._skip_untargeted("github", c, 5, mr) is True
    (row,) = store.list_for_pr(c.workspace_id, "github", c.full_name, 5)
    assert row.status == "skipped"
    assert row.status_reason.startswith("Skipped — Branch mismatch")

    assert poller._skip_untargeted("github", c, 6, {**mr, "target_branch": "staging"}) is False
    assert poller._skip_untargeted("github", c, 7, {**mr, "target_branch": ""}) is False
    _patterns(monkeypatch, [])
    assert poller._skip_untargeted("github", c, 8, mr) is False
