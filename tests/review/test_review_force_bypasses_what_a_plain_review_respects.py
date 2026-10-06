"""`@celmis review --force` asks for a FULL review that skips every soft gate;
a plain `review` / `start-review` is the explicit, incremental request.

The queue is faked: what matters is the `ReviewRequest` the command builds.
"""

from __future__ import annotations

import pytest

from src.review import pr_state
from src.review.commands import handlers
from src.review.commands.handlers import CommandContext
from src.review.dispatch import QueuedReview
from tests.review.comment_support import FakeProvider, command, event


@pytest.fixture
def queued(monkeypatch):
    calls: list[dict] = []

    def fake_enqueue(provider, repo, number, **kw):
        calls.append({"provider": provider, "repo": repo, "number": number, **kw})
        return QueuedReview("run-1", calls[-1].get("status", "queued"), "", {"run_id": "run-1"})

    monkeypatch.setattr("src.review.dispatch.enqueue_review_run", fake_enqueue)
    monkeypatch.setattr(pr_state, "resume", lambda *a, **k: False)
    return calls


def _ctx(provider=None, **cmd):
    return CommandContext(
        ev=event(), provider=provider or FakeProvider(), command=command(**cmd),
        settings={}, workspace_id="ws", user_id="u", language="en")


def test_review_force_requests_a_full_forced_review(queued):
    handlers._start_review(_ctx(force=True))
    [call] = queued
    request = call["request"]
    assert (request.trigger, request.force, request.scope) == ("command", True, "full")
    assert request.bypasses("gate_draft") and request.bypasses("gate_target_branch")
    assert call["source"] == "command"


def test_a_plain_review_is_explicit_but_not_forced(queued):
    handlers._start_review(_ctx())
    request = queued[0]["request"]
    assert (request.trigger, request.force, request.scope) == ("command", False, None)
    assert request.bypasses("gate_draft") and request.bypasses("gate_cadence")
    assert not request.bypasses("gate_target_branch")


def test_the_enabled_gate_is_never_bypassed_by_a_command(queued):
    request = _run_and_get_request(queued)
    assert not request.bypasses("gate_enabled")
    assert not request.bypasses("gate_size")


def _run_and_get_request(queued):
    handlers._start_review(_ctx(force=True))
    return queued[0]["request"]


def test_the_review_is_queued_with_the_note_to_answer_when_it_ends(queued):
    handlers._start_review(_ctx())
    ack = queued[0]["extra"]["command_ack"]
    assert ack["event"]["comment_id"] == "c1" and ack["language"] == "en"


def test_a_pull_request_that_is_not_open_is_not_reviewed(queued):
    provider = FakeProvider()
    ctx = _ctx(provider)
    ctx.ev = event(pr_state="merged")
    handlers._start_review(ctx)
    assert queued == []
    assert "merged" in provider.replies[0]


def test_an_already_queued_review_is_said_so_not_queued_twice(queued, monkeypatch):
    monkeypatch.setattr(
        "src.review.dispatch.enqueue_review_run",
        lambda *a, **k: QueuedReview("run-2", "duplicate", "folded", {}))
    provider = FakeProvider(react=False)  # a provider without reactions posts a note
    ctx = _ctx(provider)
    handlers._start_review(ctx)
    assert provider.updates and "already queued" in provider.updates[0][1]


def test_without_a_queue_the_review_runs_in_the_worker(queued, monkeypatch):
    ran = []
    monkeypatch.setattr(
        "src.review.dispatch.enqueue_review_run",
        lambda *a, **k: QueuedReview("run-3", "inline", "", {"run_id": "run-3"}))
    monkeypatch.setattr("src.review.dispatch.execute_review", ran.append)
    handlers._start_review(_ctx())
    assert ran == [{"run_id": "run-3"}]


# ─── hand-off to the incremental scope ───────────────────────────────

_REVIEWED, _MIDDLE, _HEAD = "a1" * 20, "b2" * 20, "c3" * 20


def _scope_the_review_reads(request, *, last=_REVIEWED):
    from src.review.scope import CommitInfo, decide_scope

    def commits():
        return [CommitInfo(sha=_HEAD, parents=(_MIDDLE,)),
                CommitInfo(sha=_MIDDLE, parents=(_REVIEWED,)),
                CommitInfo(sha=_REVIEWED, parents=())]

    return decide_scope("incremental", request, last_reviewed_sha=last,
                        head_sha=_HEAD, list_commits=commits)


def test_review_force_reads_the_whole_pull_request_though_a_baseline_exists(queued):
    handlers._start_review(_ctx(force=True))

    decision = _scope_the_review_reads(queued[0]["request"])

    assert (decision.mode, decision.code) == ("full", "forced")


def test_a_plain_review_command_reads_only_what_is_new_since_the_last_review(queued):
    handlers._start_review(_ctx())

    decision = _scope_the_review_reads(queued[0]["request"])

    assert (decision.mode, decision.base_sha, decision.new_commits) == ("incremental", _REVIEWED, 2)


def test_a_plain_review_command_on_an_unchanged_head_still_reviews(queued):
    handlers._start_review(_ctx())

    decision = _scope_the_review_reads(queued[0]["request"], last=_HEAD)

    assert (decision.mode, decision.code) == ("full", "asked_again")
