"""`ignored_title_keywords`: a PR whose title says "WIP" is not reviewed by a push.

The matcher is one pure function the webhook, the poller and the orchestrator
share; a person's request (a command, the Review button, the CLI) still reviews
the PR, and a repository can empty an inherited list.
"""

from __future__ import annotations

import asyncio

import pytest

from src.review.scope import ReviewRequest, title_keyword_match
from src.review.stages import StageRecorder
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _Agent,
    _finding,
    _keys,
    _orch,
    _Provider,
    _stage,
    bound,
    no_ledger,
    queue,
    store,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import (
    _pr,
    env,  # noqa: F401
)

# ─── the matcher ─────────────────────────────────────────────────────


@pytest.mark.parametrize("title, keywords, expected", [
    ("WIP: add caching", ["wip"], "wip"),
    ("Add caching [Skip Review]", ["[skip review]"], "[skip review]"),
    ("Revert \"Add caching\"", ["WIP", "revert"], "revert"),
    ("Додати кеш ЧЕРНЕТКА", ["чернетка"], "чернетка"),
    ("Straße", ["STRASSE"], "STRASSE"),
    ("Add caching", ["wip"], None),
    ("Add caching", [], None),
    ("Add caching", None, None),
    ("", ["wip"], None),
    (None, ["wip"], None),
])
def test_the_title_matches_a_keyword_in_any_case(title, keywords, expected):
    assert title_keyword_match(title, keywords) == expected


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_keyword_never_silences_every_pr(blank):
    assert title_keyword_match("Add caching", [blank]) is None


# ─── the orchestrator's gate ─────────────────────────────────────────


def _titled(title: str):
    pr = _pr()
    pr.title = title
    return pr


def _run(monkeypatch, pr, *, keywords, request=None, stages=None):
    orch = _orch(monkeypatch, [_Agent("defect", [_finding()])],
                 policy={**POLICY, "ignored_title_keywords": keywords})
    rec = stages or StageRecorder()
    result = orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec,
                         workspace_id="ws-1", request=request)
    return result, rec


def test_a_wip_title_is_skipped_and_the_stage_names_the_keyword(env, monkeypatch):  # noqa: F811
    result, rec = _run(monkeypatch, _titled("WIP: add caching"), keywords=["wip"])

    assert result.batch.run_status.value == "skipped"
    assert _keys(rec)[-1] == "gate_title"
    gate = _stage(rec, "gate_title")
    assert gate["status"] == "skipped"
    assert "wip" in gate["reason"]
    assert gate["meta"] == {"keyword": "wip"}
    assert result.batch.findings == []


def test_a_title_without_a_keyword_goes_on_to_the_context(env, monkeypatch):  # noqa: F811
    result, rec = _run(monkeypatch, _titled("Add caching"), keywords=["wip"])

    assert result.batch.run_status.value != "skipped"
    assert _stage(rec, "gate_title")["status"] == "success"
    assert _keys(rec).index("gate_title") < _keys(rec).index("context")


@pytest.mark.parametrize("trigger", ["command", "manual", "bulk", "cli", "mcp"])
def test_a_person_asking_reviews_a_title_the_push_would_skip(env, monkeypatch, trigger):  # noqa: F811
    result, rec = _run(monkeypatch, _titled("WIP: add caching"), keywords=["wip"],
                       request=ReviewRequest(trigger=trigger))

    assert result.batch.run_status.value != "skipped"
    gate = _stage(rec, "gate_title")
    assert gate["status"] == "success"
    assert "asked for explicitly" in gate["reason"]


@pytest.mark.parametrize("trigger", ["webhook", "poller"])
def test_an_automatic_trigger_meets_the_title_gate(env, monkeypatch, trigger):  # noqa: F811
    result, rec = _run(monkeypatch, _titled("WIP: add caching"), keywords=["wip"],
                       request=ReviewRequest(trigger=trigger))
    assert result.batch.run_status.value == "skipped"
    assert _keys(rec)[-1] == "gate_title"


def test_no_keywords_configured_is_one_success_stage(env, monkeypatch):  # noqa: F811
    result, rec = _run(monkeypatch, _titled("WIP"), keywords=[])
    assert result.batch.run_status.value != "skipped"
    assert _stage(rec, "gate_title")["reason"] == "No ignored title keywords configured."


# ─── the webhook's early answer ──────────────────────────────────────

GATES = {"ignored_title_keywords": [], "review_cadence": "automatic",
         "auto_pause_pushes": 3, "auto_pause_window_minutes": 15,
         "status_feedback": True}


def _gates(monkeypatch, **over):
    import src.review.review_defaults as rd

    monkeypatch.setattr(rd, "gate_settings_for_repo",
                        lambda provider, repo: {**GATES, **over})


def _deliver(title: str, head: str = "a" * 12):
    from src.review.webhook import _dispatch_review

    asyncio.run(_dispatch_review(
        "github", "acme/payments", 7, head_sha=head, event="pull_request",
        expected_workspace_id="ws-1", pr_meta={"title": title}))


def test_a_wip_title_never_costs_a_job_and_leaves_a_run_row(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch, ignored_title_keywords=["wip"])

    _deliver("WIP: add caching")

    assert queue == []
    (row,) = store.list_for_pr("ws-1", "github", "acme/payments", 7)
    assert row.status == "skipped"
    assert "wip" in row.status_reason


def test_a_clean_title_is_queued(bound, store, queue, no_ledger, monkeypatch):  # noqa: F811
    s, cfg = bound
    s.upsert(cfg())
    _gates(monkeypatch, ignored_title_keywords=["wip"])

    _deliver("Add caching")

    assert len(queue) == 1
    assert store.list_for_pr("ws-1", "github", "acme/payments", 7) == []


# ─── a fixed title brings the pull request back ──────────────────────


def _github_edit(*, title_changed: bool, state: str = "open"):
    from src.review.webhook import _extract_github_pr

    return _extract_github_pr({
        "action": "edited",
        "changes": {"title": {"from": "WIP: x"}} if title_changed else {"body": {"from": ""}},
        "pull_request": {"number": 7, "title": "x", "state": state,
                         "head": {"sha": "a" * 40}},
        "repository": {"full_name": "acme/payments"},
    })


def test_a_github_title_edit_is_a_trigger():
    info = _github_edit(title_changed=True)
    assert info and info["action"] == "edited" and info["title"] == "x"


def test_a_github_description_edit_is_not_a_trigger():
    assert _github_edit(title_changed=False) is None


def test_a_github_title_edit_on_a_closed_pull_request_is_not_a_trigger():
    assert _github_edit(title_changed=True, state="closed") is None


def test_a_title_fixed_after_the_skip_is_reviewed_by_the_poller(monkeypatch):
    from types import SimpleNamespace

    from src.review import poller

    cfg = SimpleNamespace(full_name="g/p", last_poll_etag=None, last_polled_at=None,
                          last_seen_pr_id=0, user_id="u", repo_slug="gitlab_g-p",
                          workspace_id="ws-a")
    titles = {"now": "WIP: add caching"}

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return [{"iid": 5, "title": titles["now"], "target_branch": "main"}]

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    queued, skips = [], []
    monkeypatch.setattr(poller, "build_client", lambda **k: _Client())
    monkeypatch.setattr(poller, "_skip_untargeted", lambda *a, **k: False)
    monkeypatch.setattr(poller, "_trigger_review", lambda *a, **k: queued.append(a[2]))
    monkeypatch.setattr(poller, "get_auto_review_store", lambda: SimpleNamespace(
        update_polling_state=lambda *a, **k: None))
    monkeypatch.setattr("src.review.dispatch.record_gate_skip",
                        lambda *a, **k: skips.append(a[2]))
    _gates(monkeypatch, ignored_title_keywords=["wip"])
    poller._TITLE_HELD.clear()

    poller._poll_gitlab_project("t", cfg)
    assert queued == [] and skips == [5]

    cfg.last_seen_pr_id = 5  # the poller moved past it, as it does
    poller._poll_gitlab_project("t", cfg)
    assert queued == [] and skips == [5], "still WIP: nothing new to say"

    titles["now"] = "Add caching"
    poller._poll_gitlab_project("t", cfg)
    assert queued == [5]

    poller._poll_gitlab_project("t", cfg)
    assert queued == [5], "released once, not on every poll"
