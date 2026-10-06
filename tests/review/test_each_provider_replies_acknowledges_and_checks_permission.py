"""The conversation half of each provider, against a recording transport.

What is pinned: where a reply goes (the thread for a diff comment, a quoting
PR comment for a plain one, a child comment on Bitbucket), that every reply is
chat-marked and never review-marked, that an acknowledgement is a reaction
(none on Bitbucket), and that a permission or participant lookup that fails
degrades to "unknown" / empty rather than raising.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.review import markers
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.comment_support import event
from tests.review.test_a_rerun_does_not_double_the_comments import _patch_client


class Recorder:
    """A transport that answers by (method, path suffix) and remembers requests."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]] | None = None):
        self.routes = routes or {}
        self.seen: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, body))
        for (method, suffix), (code, payload) in self.routes.items():
            if request.method == method and request.url.path.endswith(suffix):
                return httpx.Response(code, json=payload)
        return httpx.Response(200, json={"id": 900})


def _github(rec: Recorder) -> GitHubPRProvider:
    p = GitHubPRProvider(token="fake")
    _patch_client(p, httpx.MockTransport(rec))
    return p


def _gitlab(rec: Recorder) -> GitLabPRProvider:
    p = GitLabPRProvider(token="fake")
    _patch_client(p, httpx.MockTransport(rec))
    return p


def _bitbucket(rec: Recorder) -> BitbucketPRProvider:
    p = BitbucketPRProvider(token="fake")
    _patch_client(p, httpx.MockTransport(rec))
    return p


def _body(rec: Recorder, index: int = -1) -> str:
    sent = rec.seen[index][2]
    assert isinstance(sent, dict)
    if "content" in sent:
        return sent["content"]["raw"]
    return sent["body"]


# ─── GitHub ─────────────────────────────────────────────────────────

def test_a_github_reply_to_a_diff_comment_goes_into_its_thread():
    rec = Recorder()
    ev = event(kind="inline", thread_id="55", comment_id="56")
    assert _github(rec).post_reply(ev, "Done.") == "900"
    method, path, _ = rec.seen[-1]
    assert (method, path) == ("POST", "/repos/acme/shop/pulls/7/comments/55/replies")


def test_a_github_reply_to_a_plain_comment_quotes_who_asked():
    rec = Recorder()
    _github(rec).post_reply(event(), "Done.")
    method, path, _ = rec.seen[-1]
    assert (method, path) == ("POST", "/repos/acme/shop/issues/7/comments")
    assert _body(rec).startswith("> @alice\n\n")


@pytest.mark.parametrize("make", [_github, _gitlab, _bitbucket])
def test_every_reply_carries_the_chat_marker_and_never_the_review_marker(make):
    rec = Recorder()
    make(rec).post_reply(event(provider="x", repo="acme/shop"), "Hello.")
    text = _body(rec)
    assert markers.has_marker(text, "chat:v1")
    assert not markers.has_marker(text, "review")


def test_a_github_acknowledgement_is_an_eyes_reaction_on_the_comment():
    rec = Recorder()
    assert _github(rec).acknowledge(event(comment_id="c9")) is True
    method, path, sent = rec.seen[-1]
    assert (method, path) == ("POST", "/repos/acme/shop/issues/comments/c9/reactions")
    assert sent == {"content": "eyes"}


def test_a_github_acknowledgement_of_a_diff_comment_uses_the_pulls_endpoint():
    rec = Recorder()
    _github(rec).acknowledge(event(kind="inline", comment_id="c9"))
    assert rec.seen[-1][1] == "/repos/acme/shop/pulls/comments/c9/reactions"


@pytest.mark.parametrize(("level", "expected"), [
    ("admin", "write"), ("maintain", "write"), ("write", "write"),
    ("triage", "read"), ("read", "read"), ("none", "none"), ("weird", "unknown"),
])
def test_a_github_permission_is_mapped_to_write_read_none(level, expected):
    rec = Recorder({("GET", "/permission"): (200, {"permission": level})})
    got = _github(rec).actor_permission("acme/shop", actor_id="1", actor_name="alice")
    assert got == expected


def test_a_github_permission_lookup_that_fails_is_unknown():
    rec = Recorder({("GET", "/permission"): (403, {"message": "no"})})
    assert _github(rec).actor_permission("acme/shop", actor_name="alice") == "unknown"


def test_the_github_participants_are_author_reviewers_and_assignees():
    pr = {
        "user": {"id": 1, "login": "alice"},
        "requested_reviewers": [{"id": 2, "login": "bob"}],
        "assignees": [{"id": 3, "login": "carol"}],
    }
    rec = Recorder({("GET", "/pulls/7"): (200, pr)})
    got = _github(rec).pr_participants("acme/shop", 7)
    assert got == {"1", "alice", "2", "bob", "3", "carol"}


def test_a_github_edit_of_a_reply_keeps_the_chat_marker():
    rec = Recorder()
    assert _github(rec).update_comment("acme/shop", 7, "88", "Updated.") is True
    method, path, _ = rec.seen[-1]
    assert (method, path) == ("PATCH", "/repos/acme/shop/issues/comments/88")
    assert markers.has_marker(_body(rec), "chat:v1")


# ─── GitLab ─────────────────────────────────────────────────────────

def test_a_gitlab_reply_goes_into_the_discussion():
    rec = Recorder()
    ev = event(provider="gitlab", thread_id="d1")
    _gitlab(rec).post_reply(ev, "Done.")
    assert rec.seen[-1][1].endswith("/merge_requests/7/discussions/d1/notes")


def test_a_gitlab_reply_falls_back_to_a_plain_note_when_the_thread_refuses():
    rec = Recorder({("POST", "/discussions/d1/notes"): (400, {"message": "resolved"})})
    ev = event(provider="gitlab", thread_id="d1")
    assert _gitlab(rec).post_reply(ev, "Done.") == "900"
    assert rec.seen[-1][1].endswith("/merge_requests/7/notes")


def test_a_gitlab_acknowledgement_is_an_award_emoji():
    rec = Recorder()
    assert _gitlab(rec).acknowledge(event(provider="gitlab", comment_id="12")) is True
    assert rec.seen[-1][1].endswith("/merge_requests/7/notes/12/award_emoji")
    assert rec.seen[-1][2] == {"name": "eyes"}


@pytest.mark.parametrize(("code", "payload", "expected"), [
    (200, {"access_level": 30}, "write"),
    (200, {"access_level": 50}, "write"),
    (200, {"access_level": 20}, "read"),
    (404, {}, "none"),
    (500, {}, "unknown"),
])
def test_a_gitlab_access_level_is_mapped_to_write_read_none(code, payload, expected):
    rec = Recorder({("GET", "/members/all/101"): (code, payload)})
    assert _gitlab(rec).actor_permission("acme/shop", actor_id="101") == expected


def test_a_gitlab_actor_without_a_numeric_id_is_unknown_without_a_request():
    rec = Recorder()
    assert _gitlab(rec).actor_permission("acme/shop", actor_id="alice") == "unknown"
    assert rec.seen == []


# ─── Bitbucket ──────────────────────────────────────────────────────

def test_a_bitbucket_reply_is_a_child_of_the_comment_that_asked():
    rec = Recorder()
    assert _bitbucket(rec).post_reply(event(provider="bitbucket", comment_id="31"), "Done.") == "900"
    method, path, sent = rec.seen[-1]
    assert (method, path) == ("POST", "/2.0/repositories/acme/shop/pullrequests/7/comments")
    assert sent["parent"] == {"id": 31}


def test_a_bitbucket_reply_hides_its_marker_from_the_reader():
    rec = Recorder()
    _bitbucket(rec).post_reply(event(provider="bitbucket"), "Done.")
    assert "<!--" not in _body(rec)


def test_bitbucket_has_no_reaction_so_the_caller_replies_instead():
    rec = Recorder()
    assert _bitbucket(rec).acknowledge(event(provider="bitbucket")) is False
    assert rec.seen == []


def test_a_bitbucket_edit_of_a_reply_is_a_put_on_the_comment():
    rec = Recorder()
    assert _bitbucket(rec).update_comment("acme/shop", 7, "88", "Updated.") is True
    assert rec.seen[-1][0:2] == ("PUT", "/2.0/repositories/acme/shop/pullrequests/7/comments/88")


def test_the_bitbucket_participants_are_the_author_and_the_reviewers():
    pr = {
        "author": {"uuid": "{a}", "account_id": "acc-a"},
        "reviewers": [{"uuid": "{b}"}],
        "participants": [{"role": "REVIEWER", "user": {"uuid": "{c}"}}],
    }
    rec = Recorder({("GET", "/pullrequests/7"): (200, pr)})
    assert _bitbucket(rec).pr_participants("acme/shop", 7) == {"{a}", "acc-a", "{b}", "{c}"}


def test_a_bitbucket_commenter_listed_as_a_participant_is_not_one_of_the_pr():
    # `participants` also lists whoever commented or approved: on a public
    # repository that is the stranger who just asked the bot for something.
    pr = {
        "author": {"uuid": "{a}"},
        "reviewers": [],
        "participants": [
            {"role": "PARTICIPANT", "user": {"uuid": "{stranger}"}, "participated_on": "x"},
            {"user": {"uuid": "{nameless}"}},
        ],
    }
    rec = Recorder({("GET", "/pullrequests/7"): (200, pr)})
    assert _bitbucket(rec).pr_participants("acme/shop", 7) == {"{a}"}


def test_a_bitbucket_permission_is_never_claimed():
    assert _bitbucket(Recorder()).actor_permission("acme/shop", actor_id="x") == "unknown"
