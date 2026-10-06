"""Each provider's comment webhook becomes the same `CommentEvent`.

The payloads under fixtures/pr_comments are the shapes the three providers
send; the extractors read them, never trust them, and never raise.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.review.commands.events import (
    extract_bitbucket_comment,
    extract_github_comment,
    extract_gitlab_note,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pr_comments"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_a_bitbucket_top_level_comment_is_read():
    ev = extract_bitbucket_comment(
        _load("bitbucket_comment_created_top_level"), "pullrequest:comment_created",
        delivery="uuid-1")
    assert (ev.provider, ev.repo, ev.pr_number, ev.comment_id) == (
        "bitbucket", "acme/shop", 42, "1001")
    assert ev.body == "@celmis review --force"
    assert ev.kind == "issue" and ev.parent_id is None and not ev.edited
    assert ev.actor_name == "alice" and "acc-alice" in ev.actor_ids
    assert (ev.head_sha, ev.base_ref, ev.pr_state) == ("abc123def456", "develop", "open")
    assert ev.repo_private is True and ev.event_key == "bb:uuid-1"


def test_a_bitbucket_inline_reply_knows_its_thread_and_line():
    ev = extract_bitbucket_comment(
        _load("bitbucket_comment_created_inline_reply"), "pullrequest:comment_created")
    assert ev.kind == "inline" and ev.parent_id == "900" and ev.thread_id == "900"
    assert (ev.path, ev.line) == ("src/cart.py", 17)


def test_a_bitbucket_edit_is_marked_as_one():
    ev = extract_bitbucket_comment(
        _load("bitbucket_comment_updated"), "pullrequest:comment_updated")
    assert ev.edited and ev.comment_id == "1001"


def test_a_github_pull_request_comment_is_read():
    ev = extract_github_comment(_load("github_issue_comment"), "issue_comment",
                                delivery="d-1")
    assert (ev.provider, ev.repo, ev.pr_number, ev.comment_id) == (
        "github", "acme/shop", 42, "5001")
    assert ev.kind == "issue" and ev.actor_name == "alice" and ev.actor_assoc == "MEMBER"
    assert ev.repo_private is False and ev.event_key == "gh:d-1"


def test_a_comment_on_a_plain_github_issue_is_not_a_pull_request_comment():
    payload = _load("github_issue_comment")
    del payload["issue"]["pull_request"]
    assert extract_github_comment(payload, "issue_comment") is None


def test_a_github_review_reply_hangs_off_the_thread_root():
    ev = extract_github_comment(_load("github_review_comment_reply"),
                                "pull_request_review_comment")
    assert ev.kind == "inline" and ev.parent_id == "4900" and ev.thread_id == "4900"
    assert (ev.path, ev.line, ev.head_sha, ev.base_ref) == (
        "src/cart.py", 17, "abc123def456", "develop")
    assert ev.actor_assoc == "NONE"


def test_only_a_created_or_edited_github_comment_is_read():
    payload = _load("github_issue_comment")
    payload["action"] = "deleted"
    assert extract_github_comment(payload, "issue_comment") is None


def test_a_gitlab_diff_note_is_read():
    ev = extract_gitlab_note(_load("gitlab_note_diff"))
    assert (ev.provider, ev.repo, ev.pr_number, ev.comment_id) == (
        "gitlab", "acme/shop", 42, "9001")
    assert ev.kind == "inline" and ev.thread_id == "d1d1d1"
    assert (ev.path, ev.line, ev.actor_name) == ("src/cart.py", 17, "carol")
    assert ev.repo_private is True and ev.pr_state == "open"


def test_a_gitlab_system_note_is_not_a_comment():
    assert extract_gitlab_note(_load("gitlab_note_system")) is None


@pytest.mark.parametrize("breakage", [
    lambda p: p.pop("repository", None) or p.pop("project", None),
    lambda p: p.clear(),
])
def test_a_malformed_delivery_is_none_and_never_raises(breakage):
    for extract, name, event in (
        (extract_bitbucket_comment, "bitbucket_comment_created_top_level",
         "pullrequest:comment_created"),
        (extract_github_comment, "github_issue_comment", "issue_comment"),
    ):
        payload = _load(name)
        breakage(payload)
        assert extract(payload, event) is None
    for junk in (None, [], "text", 7):
        assert extract_bitbucket_comment(junk, "pullrequest:comment_created") is None
        assert extract_github_comment(junk, "issue_comment") is None
        assert extract_gitlab_note(junk) is None


def test_a_comment_without_text_is_none():
    payload = _load("bitbucket_comment_created_top_level")
    payload["comment"]["content"]["raw"] = "   "
    assert extract_bitbucket_comment(payload, "pullrequest:comment_created") is None


@pytest.mark.parametrize("level", [0, 10, 20])
def test_only_a_private_gitlab_project_counts_as_private(level):
    payload = _load("gitlab_note_diff")
    payload["project"]["visibility_level"] = level
    # Internal is open to every account on the instance, public to everyone:
    # neither says that this commenter has access.
    assert extract_gitlab_note(payload).repo_private is (level == 0)


@pytest.mark.parametrize("name", [
    "project_42_bot", "project_42_bot_a1b2c3d4e5f6", "group_7_bot_9f8e7d6c",
])
def test_a_gitlab_access_token_bot_is_recognised_by_its_name(name):
    payload = _load("gitlab_note_diff")
    payload["user"]["username"] = name
    assert extract_gitlab_note(payload).actor_is_bot is True


@pytest.mark.parametrize("name", ["carol", "project_manager_bot", "my_project_42_bot"])
def test_a_person_whose_name_looks_a_little_like_a_bot_is_not_one(name):
    payload = _load("gitlab_note_diff")
    payload["user"]["username"] = name
    assert extract_gitlab_note(payload).actor_is_bot is False
