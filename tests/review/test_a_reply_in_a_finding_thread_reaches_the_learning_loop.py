"""A reply in the thread of one of our findings is feedback, whichever way it
reached us, and it is read BEFORE the command path would treat it as a chat
question.

  * `@celmis dismiss` and a thumbs-down in a finding's thread teach the
    reviewer and are not also answered as chat;
  * a question in that thread (with or without the handle) goes on to the chat;
  * a named command (`review`, `remember`) never goes through the learning loop;
  * a stranger's reply teaches nothing and costs no model call;
  * a reply to something that is not a finding ends after one lookup;
  * GitLab replies name the discussion, not the note: the discussion id is kept;
  * GitHub tells us a thread was resolved, and that is a weak signal.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from src.review import markers
from src.review.commands.parser import CHAT, ParsedCommand
from src.review.learning import receiver, resolve
from src.review.learning import signals as sig
from src.review.providers.base import PostedComment
from src.review.settings import ReviewSettings
from src.review.webhook import _dispatch_command, _dispatch_thread_event, build_webhook_app
from tests.review.comment_support import FakeProvider, command, event
from tests.review.rules_db import rules_db
from tests.review.test_the_comment_payload_fixtures_parse import _load

WS = "ws-1"
PR = sig.PRRef("github", "acme/shop", 7)
SNAP = sig.FindingSnapshot(
    title="Possible null dereference in user lookup", file_path="src/users/lookup.py",
    body="user may be None here", rule_id="defect.null", agent="defect", severity="warning")


def _reply(body="👎", **over):
    base = dict(parent_id="901", thread_id="901", kind="inline", actor_assoc="MEMBER")
    return event(body=body, comment_id="1001", **{**base, **over})


def _post(**extra):
    fp = sig.snapshot_of(SNAP).fingerprint
    assert sig.record_posted(
        WS, PR, [SNAP], [{"comment_id": 901, "fingerprint": fp[:16], **extra}]) == 1


def _signals(tmp_path):
    with sa.create_engine(f"sqlite:///{tmp_path}/rules.db").connect() as conn:
        return [tuple(r) for r in conn.execute(sa.text(
            "SELECT signal, source, actor FROM finding_signals"))]


class _Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def config_for_repo(self, provider, repo):
        return self._cfg


@pytest.fixture
def seams(monkeypatch):
    """The tenant binding, the learning handler, the ledger claim and the queue."""
    seen = SimpleNamespace(accepted=[], learned=[], result=None)
    cfg = SimpleNamespace(workspace_id=WS, user_id="owner", enabled=True)
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: _Store(cfg))

    def learn(ev, *, workspace_id, user_id):
        seen.learned.append((ev.comment_id, workspace_id, user_id))
        return seen.result

    def accept(ev, cmd, *, workspace_id, user_id):
        seen.accepted.append(cmd)
        return {"ledger_id": "row-1"}

    monkeypatch.setattr("src.review.learning.receiver.learn_from_comment", learn)
    monkeypatch.setattr("src.review.commands.handlers.accept", accept)
    monkeypatch.setattr("src.sync.queue.enqueue", lambda **kw: "job-1")
    return seen


def _result(**kw):
    from src.review.learning.replies import ReplyResult

    return ReplyResult(**kw)


# ─── the receiver's ordering ─────────────────────────────────────


async def test_feedback_that_was_taken_is_not_also_answered_as_chat(seams):
    seams.result = _result(handled=True, action="dismiss", signal="dismissed")
    await _dispatch_command(_reply("@celmis dismiss"), ParsedCommand(CHAT, args="dismiss"),
                            expected_workspace_id=WS)
    assert seams.learned == [("1001", WS, "owner")]
    assert seams.accepted == []


async def test_a_question_in_a_finding_thread_goes_on_to_the_chat(seams):
    seams.result = _result(action="question", handoff="chat")
    await _dispatch_command(_reply("@celmis why is this a bug?"),
                            ParsedCommand(CHAT, args="why is this a bug?"),
                            expected_workspace_id=WS)
    assert [c.name for c in seams.accepted] == [CHAT]


async def test_a_question_without_the_handle_in_a_finding_thread_is_the_chats_too(seams):
    seams.result = _result(action="question", handoff="chat")
    await _dispatch_command(_reply("why is this a bug"), None, expected_workspace_id=WS)
    [asked] = seams.accepted
    assert (asked.name, asked.args) == (CHAT, "why is this a bug")


async def test_a_reply_that_teaches_nothing_and_asks_nothing_ends_there(seams):
    seams.result = _result(action="other")
    await _dispatch_command(_reply("ok, will do"), None, expected_workspace_id=WS)
    assert seams.accepted == []


async def test_a_chat_command_the_loop_did_not_take_is_still_a_chat(seams):
    seams.result = _result(reason="not a reply to a finding")
    await _dispatch_command(_reply("@celmis explain the cache"),
                            ParsedCommand(CHAT, args="explain the cache"),
                            expected_workspace_id=WS)
    assert [c.name for c in seams.accepted] == [CHAT]


@pytest.mark.parametrize("name", ["review", "start-review", "help", "remember"])
async def test_a_named_command_never_goes_through_the_learning_loop(seams, name):
    seams.result = _result(handled=True)
    await _dispatch_command(_reply("@celmis " + name), command(name), expected_workspace_id=WS)
    assert seams.learned == []
    assert [c.name for c in seams.accepted] == [name]


async def test_a_comment_that_is_not_a_reply_skips_the_loop(seams):
    await _dispatch_command(event(body="@celmis why?"), ParsedCommand(CHAT, args="why?"),
                            expected_workspace_id=WS)
    assert seams.learned == []
    assert [c.name for c in seams.accepted] == [CHAT]


# ─── the webhook's first filter ──────────────────────────────────


@pytest.fixture
def client():
    settings = ReviewSettings(webhook_secret="github-secret")
    with patch("src.review.webhook._dispatch_command", new_callable=AsyncMock) as dispatch, \
            patch("src.review.webhook._dispatch_thread_event",
                  new_callable=AsyncMock) as thread:
        yield TestClient(build_webhook_app(settings)), dispatch, thread


def _signed(payload, event_name, delivery):
    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig_ = "sha256=" + hmac.new(b"github-secret", body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig_, "X-GitHub-Delivery": delivery,
                  "X-GitHub-Event": event_name}


def _review_comment(body, reply_to):
    payload = _load("github_review_comment_reply")
    payload["comment"]["body"] = body
    payload["comment"]["in_reply_to_id"] = reply_to
    return payload


def test_a_reply_without_the_handle_is_looked_at_but_the_answer_is_unchanged(client):
    c, dispatch, _ = client
    body, headers = _signed(_review_comment("not an issue", 901), "pull_request_review_comment", "d-1")
    resp = c.post("/webhook/github", content=body, headers=headers)
    assert resp.json() == {"status": "ignored", "reason": "no command"}
    assert dispatch.await_count == 1 and dispatch.await_args.args[1] is None


def test_a_top_level_comment_without_the_handle_costs_nothing(client):
    c, dispatch, _ = client
    body, headers = _signed(_review_comment("not an issue", None), "pull_request_review_comment", "d-2")
    c.post("/webhook/github", content=body, headers=headers)
    dispatch.assert_not_awaited()


def test_a_reply_that_carries_our_marker_is_not_looked_at(client):
    from src.review import markers

    c, dispatch, _ = client
    text = markers.finding_marker("a" * 16, "b" * 12) + "\nour finding"
    body, headers = _signed(_review_comment(text, 901), "pull_request_review_comment", "d-3")
    c.post("/webhook/github", content=body, headers=headers)
    dispatch.assert_not_awaited()


def _thread_payload(action="resolved", sender=None):
    return {"action": action, "repository": {"full_name": "acme/shop"},
            "pull_request": {"number": 7},
            "thread": {"comments": [{"id": 901}, {"id": 1001}]},
            "sender": sender or {"login": "jane", "type": "User"}}


def test_a_resolved_thread_is_handed_to_the_learning_loop(client):
    c, _, thread = client
    body, headers = _signed(_thread_payload(), "pull_request_review_thread", "d-4")
    resp = c.post("/webhook/github", content=body, headers=headers)
    assert resp.status_code == 202 and resp.json()["thread"] == "resolved"
    assert thread.await_count == 1


def test_a_thread_resolved_by_a_bot_is_ignored(client):
    c, _, thread = client
    body, headers = _signed(
        _thread_payload(sender={"login": "ci[bot]", "type": "Bot"}),
        "pull_request_review_thread", "d-5")
    assert c.post("/webhook/github", content=body, headers=headers).json()["status"] == "ignored"
    thread.assert_not_awaited()


def test_the_thread_events_carry_the_delivery_signature_check(client):
    c, _, thread = client
    body, headers = _signed(_thread_payload(), "pull_request_review_thread", "d-6")
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert c.post("/webhook/github", content=body, headers=headers).status_code == 401
    thread.assert_not_awaited()


async def test_a_thread_event_of_an_unbound_repo_is_dropped(monkeypatch):
    called = []
    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: _Store(None))
    monkeypatch.setattr("src.review.learning.receiver.learn_from_thread",
                        lambda *a, **k: called.append(1))
    ev = resolve.ThreadEvent(provider="github", repo="acme/shop", pr_number=7, resolved=True,
                             comment_ids=["901"])
    await _dispatch_thread_event(ev, expected_workspace_id=WS)
    assert called == []


# ─── the blocking half, against a real database ──────────────────


def _provider(monkeypatch, **kw):
    provider = FakeProvider(**kw)
    monkeypatch.setattr("src.review.providers.get_provider_for", lambda *a, **k: provider)
    return provider


async def test_a_person_with_access_teaches_by_a_reply(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        provider = _provider(monkeypatch)
        out = receiver.learn_from_comment(_reply("@celmis dismiss", repo_private=True),
                                          workspace_id=WS, user_id="owner")
        assert (out.handled, out.signal) == (True, "dismissed")
        assert _signals(tmp_path) == [("dismissed", "command", "101")]
        assert provider.closed


async def test_a_stranger_teaches_nothing_and_costs_no_model_call(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, permission="read")
        asked = []
        monkeypatch.setattr("src.review.learning.replies.ask_model",
                            lambda *a, **k: asked.append(1))
        out = receiver.learn_from_comment(
            _reply("this is wrong because we always pass a user", actor_assoc="NONE",
                   repo_private=None),
            workspace_id=WS, user_id="owner")
        assert not out.handled and out.reason == "not allowed to teach here"
        assert _signals(tmp_path) == [] and asked == []


async def test_a_reply_that_carries_our_marker_teaches_nothing(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, viewer={"101"})
        out = receiver.learn_from_comment(
            _reply(markers.with_chat_marker("👎"), repo_private=True),
            workspace_id=WS, user_id="owner")
        assert not out.handled and _signals(tmp_path) == []


async def test_the_token_owner_replying_without_a_marker_is_a_person_who_teaches(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, viewer={"101"})
        out = receiver.learn_from_comment(_reply("👎", repo_private=True),
                                          workspace_id=WS, user_id="owner")
        assert out.handled and len(_signals(tmp_path)) == 1


async def test_a_reply_to_something_that_is_not_a_finding_builds_no_provider(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the provider must not be built for ordinary conversation")

        monkeypatch.setattr("src.review.providers.get_provider_for", boom)
        out = receiver.learn_from_comment(_reply("thanks", parent_id="555", thread_id="555"),
                                          workspace_id=WS, user_id="owner")
        assert out.reason == "not a reply to a finding"


async def test_a_gitlab_reply_names_the_discussion_not_the_note(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        fp = sig.snapshot_of(SNAP).fingerprint
        pr = sig.PRRef("gitlab", "acme/shop", 7)
        posted = PostedComment(comment_id=4711, path="src/users/lookup.py", line=3,
                               fingerprint=fp[:16], finding_key=fp, thread_id="d3adb33f")
        assert sig.record_posted(WS, pr, [SNAP], [posted]) == 1
        _provider(monkeypatch)
        ev = _reply("👎", provider="gitlab", parent_id=None, thread_id="d3adb33f",
                    repo_private=True)
        assert receiver.learn_from_comment(ev, workspace_id=WS, user_id="owner").signal == (
            "dismissed")
        assert sig.find_posted(WS, pr, "4711")["fingerprint"] == fp
        assert sig.find_posted(WS, pr, "d3adb33f")["fingerprint"] == fp


async def test_a_resolved_thread_becomes_a_weak_signal(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, permission="write")
        ev = resolve.extract_github_thread_event(_thread_payload())
        receiver.learn_from_thread(ev, workspace_id=WS, user_id="owner")
        assert [s[:2] for s in _signals(tmp_path)] == [("resolved", "resolve")]


async def test_a_stranger_resolving_a_thread_teaches_nothing(tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, permission="read")
        ev = resolve.extract_github_thread_event(_thread_payload())
        out = receiver.learn_from_thread(ev, workspace_id=WS, user_id="owner")
        assert not out["handled"] and _signals(tmp_path) == []


async def test_anyone_who_can_see_a_private_repo_may_resolve_a_thread_that_teaches(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, permission="read")
        payload = _thread_payload()
        payload["repository"]["private"] = True
        ev = resolve.extract_github_thread_event(payload)
        assert ev.repo_private is True
        receiver.learn_from_thread(ev, workspace_id=WS, user_id="owner")
        assert len(_signals(tmp_path)) == 1


async def test_a_thread_that_holds_no_finding_does_not_ask_the_provider(
        tmp_path, monkeypatch):
    async with rules_db(tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the provider must not be built for an ordinary thread")

        monkeypatch.setattr("src.review.providers.get_provider_for", boom)
        ev = resolve.extract_github_thread_event(_thread_payload())
        assert receiver.learn_from_thread(ev, workspace_id=WS, user_id="owner")["action"] \
            == "not a finding"


@pytest.fixture
def no_closed_threads():
    resolve._closed_by_us.clear()
    yield
    resolve._closed_by_us.clear()


async def test_a_thread_the_reviewer_closed_itself_is_not_a_persons_signal(
        tmp_path, monkeypatch, no_closed_threads):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        _provider(monkeypatch, permission="write")
        ev = resolve.extract_github_thread_event(_thread_payload())
        resolve.note_closed_by_us(ev.provider, ev.repo, ev.pr_number, ev.comment_ids)
        out = receiver.learn_from_thread(ev, workspace_id=WS)
        assert not out["handled"] and _signals(tmp_path) == []


async def test_a_person_reopening_a_thread_the_reviewer_closed_is_still_heard(
        tmp_path, monkeypatch, no_closed_threads):
    async with rules_db(tmp_path, monkeypatch):
        _post()
        ev = resolve.extract_github_thread_event(_thread_payload())
        resolve.note_closed_by_us(ev.provider, ev.repo, ev.pr_number, ev.comment_ids)
        ev.resolved = False
        assert receiver.learn_from_thread(ev, workspace_id=WS)["action"] == "reopened"
