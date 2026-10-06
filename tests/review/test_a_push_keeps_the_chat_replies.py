"""A push re-runs the review, and the review cleans up its own earlier comments
by the review marker. A reply to a command carries the chat marker instead, so
that cleanup never matches it: the conversation survives the next push."""

from __future__ import annotations

from src.review import markers
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from tests.review.comment_support import routed_transport as _transport
from tests.review.test_a_rerun_does_not_double_the_comments import MARKER, _patch_client

BOT = "bot"


def test_a_chat_reply_does_not_carry_the_review_marker():
    reply = markers.with_chat_marker("Here is the answer.")
    assert markers.has_marker(reply, "chat:v1")
    assert not markers.has_marker(reply, MARKER)


def test_the_review_summary_is_not_taken_for_a_chat_reply():
    summary = f"{MARKER}\n\n## Review\n\nLooks fine."
    assert markers.has_marker(summary, MARKER)
    assert not markers.has_marker(summary, "chat:v1")


def test_github_cleanup_does_not_match_a_chat_reply():
    comment = {"id": 1, "body": markers.with_chat_marker("answer"), "user": {"login": BOT}}
    assert GitHubPRProvider._is_ours(comment, MARKER, BOT) is False


def test_gitlab_cleanup_does_not_match_a_chat_reply():
    note = {"id": 1, "body": markers.with_chat_marker("answer"), "author": {"username": BOT}}
    assert GitLabPRProvider._is_ours(note, MARKER, BOT) is False


def test_bitbucket_cleanup_does_not_match_a_chat_reply():
    comment = {
        "id": 1, "content": {"raw": markers.hide(markers.with_chat_marker("answer"))},
        "user": {"uuid": "{bot}"},
    }
    assert BitbucketPRProvider._is_ours(comment, MARKER, frozenset({"{bot}"})) is False


def test_a_quote_of_a_chat_reply_is_not_one_either():
    reply = markers.with_chat_marker("answer")
    quoted = "\n".join(f"> {line}" for line in reply.splitlines())
    assert not markers.has_marker(quoted, "chat:v1")


# ─── A person who talks through the token protects the thread ────────
#
# On an install where the token is a person's own account, the question to the
# reviewer is written by the very account that wrote the finding. With no
# marker on it, it is a person's words, and the push that replaces the finding
# must not tear the conversation out from under it.



FINDING_TEXT = (
    f"{MARKER}\n\nPossible race.\n\n"
    f"{markers.finding_marker('0123456789abcdef', 'abc123def456')}")


def test_a_github_finding_with_a_question_under_it_is_not_deleted_on_a_push():
    inline = [
        {"id": 1, "body": FINDING_TEXT, "user": {"login": BOT}},
        {"id": 2, "body": "@celmis why?", "user": {"login": BOT}, "in_reply_to_id": 1},
        {"id": 3, "body": markers.with_chat_marker("Because."), "user": {"login": BOT},
         "in_reply_to_id": 1},
    ]
    p = GitHubPRProvider(token="fake")
    _patch_client(p, _transport({
        "/pulls/7/comments": inline, "/issues/7/comments": [], "/user": {"login": BOT}}))
    marked, protected, complete = p._marked_comments("acme", "shop", 7, MARKER)
    assert complete
    assert [cid for _kind, cid in marked] == [1]     # the chat reply is not a review comment
    assert protected == {1}


def test_a_github_finding_nobody_answered_is_replaced_as_before():
    inline = [{"id": 1, "body": FINDING_TEXT, "user": {"login": BOT}}]
    p = GitHubPRProvider(token="fake")
    _patch_client(p, _transport({
        "/pulls/7/comments": inline, "/issues/7/comments": [], "/user": {"login": BOT}}))
    _marked, protected, _complete = p._marked_comments("acme", "shop", 7, MARKER)
    assert protected == set()


def test_a_bitbucket_finding_with_a_question_under_it_is_not_deleted_on_a_push():
    def row(cid, raw, parent=None):
        out = {"id": cid, "content": {"raw": raw}, "user": {"uuid": "{bot}"},
               "inline": {"path": "a.py", "to": 3}}
        if parent:
            out["parent"] = {"id": parent}
        return out

    values = [row(1, markers.hide(FINDING_TEXT)), row(2, "@celmis why?", parent=1),
              row(3, markers.hide(markers.with_chat_marker("Because.")), parent=2)]
    p = BitbucketPRProvider(token="fake")
    _patch_client(p, _transport({
        "/pullrequests/7/comments": {"values": values}, "/2.0/user": {"uuid": "{bot}"}}))
    marked, protected, complete = p._marked_comments("acme", "shop", 7, MARKER)
    assert complete
    assert [cid for _kind, cid in marked] == [1]
    assert protected == {1}


def test_a_gitlab_discussion_with_a_question_in_it_is_protected_on_a_push():
    discussion = {"id": "d1", "notes": [
        {"id": 1, "body": FINDING_TEXT, "author": {"username": BOT}},
        {"id": 2, "body": "@celmis why?", "author": {"username": BOT}},
        {"id": 3, "body": markers.with_chat_marker("Because."), "author": {"username": BOT}},
    ]}
    p = GitLabPRProvider(token="fake")
    _patch_client(p, _transport({
        "/discussions": [discussion], "/user": {"username": BOT}}))
    protected, complete = p._protected_note_ids("acme%2Fshop", 7)
    assert complete and protected == {1, 2, 3}


def test_a_gitlab_discussion_of_only_our_own_marked_notes_is_not_protected():
    discussion = {"id": "d1", "notes": [
        {"id": 1, "body": FINDING_TEXT, "author": {"username": BOT}},
        {"id": 3, "body": markers.with_chat_marker("Note."), "author": {"username": BOT}},
    ]}
    p = GitLabPRProvider(token="fake")
    _patch_client(p, _transport({
        "/discussions": [discussion], "/user": {"username": BOT}}))
    protected, _ = p._protected_note_ids("acme%2Fshop", 7)
    assert protected == set()
