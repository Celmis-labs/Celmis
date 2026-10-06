"""An incremental review posts what is new and leaves everything else alone.

All three providers, the same facts, through fakes that STORE what is posted
(the ones of `test_a_rerun_does_not_double_the_comments`, extended with commits,
threads and resolution):

  * the comments of earlier runs stay - a review of two new commits must not
    wipe the review of the rest of the pull request;
  * a finding that is already our open comment at the same place (same
    fingerprint, within three lines) is not posted again, and does not use up a
    slot of the inline cap;
  * a thread on a line the new commits removed is resolved; one a person
    replied in stays open; a comment that merely carries our marker, from
    another account, is not ours;
  * a finding on the old side is demoted to the summary (the old side of an
    increment is not a place the pull request's diff has);
  * a thread listing that cannot be read posts everything and resolves nothing;
  * every post, incremental or whole, answers with `inline_comments`.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from src.review.models import Finding, FindingSeverity, HunkSide, ScopeInfo
from src.review.providers.base import _format_finding_body
from tests.review.test_a_rerun_does_not_double_the_comments import (  # noqa: F401 — fixtures
    HUMAN,
    MARKER,
    _batch,
    _bitbucket,
    _FakeBitbucket,
    _FakeGitHub,
    _FakeGitLab,
    _github,
    _gitlab,
    settings,
)

BASE = "1" * 40


def _scope(**removed) -> ScopeInfo:
    return ScopeInfo(base_sha=BASE, new_commits=2,
                     removed_lines={p: set(lines) for p, lines in removed.items()})


def _incremental(hub, findings: list[int], *, scope: ScopeInfo | None = None):
    """A batch of the findings `_batch` numbers `findings`, as an incremental review's."""
    batch = _batch(hub.provider_name, hub.repo, hub.number, findings=max(findings) + 1)
    batch.findings = [f for i, f in enumerate(batch.findings) if i in findings]
    batch.pull_request.scope = scope or _scope()
    return batch


# ─── GitHub ──────────────────────────────────────────────────────────


class _GitHubThreads(_FakeGitHub):
    """The REST fake plus review threads (GraphQL) and a review's comments."""

    def __init__(self) -> None:
        super().__init__()
        self.resolved: set[int] = set()
        self.threads_status = 200
        #: GitHub reports `line` only while the code is still there; once it is
        #: outdated `line` is null and `originalLine` is the position at creation.
        self.lines_are_current = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/graphql":
            return self._graphql(json.loads(request.content))
        by_review = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews/(\d+)/comments", path)
        if method == "GET" and by_review:
            rid = int(by_review.group(1))
            return httpx.Response(200, json=[c for c in self.inline if c.get("review_id") == rid])
        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews", path):
            payload = json.loads(request.content)
            rid = next(self._ids)
            for comment in payload.get("comments") or []:
                self.add_inline(comment["body"])
                self.inline[-1].update(path=comment["path"], line=comment["line"],
                                       review_id=rid)
            self.reviews.append(payload)
            return httpx.Response(200, json={"id": rid, "html_url": "https://gh/pr/1"})
        return super().__call__(request)

    def _graphql(self, payload: dict) -> httpx.Response:
        query = payload["query"]
        if "resolveReviewThread" in query:
            data = {}
            for i, key in enumerate(sorted(k for k in payload["variables"] if k.startswith("i"))):
                self.resolved.add(int(payload["variables"][key]["threadId"].removeprefix("T")))
                data[f"r{i}"] = {"thread": {"isResolved": True}}
            return httpx.Response(200, json={"data": data})
        if "reviewThreads" in query:
            if self.threads_status >= 400:
                return httpx.Response(self.threads_status, json={"message": "boom"})
            nodes = []
            for root in (c for c in self.inline if "in_reply_to_id" not in c):
                replies = [c for c in self.inline if c.get("in_reply_to_id") == root["id"]]
                nodes.append({
                    "id": f"T{root['id']}", "isResolved": root["id"] in self.resolved,
                    "path": root.get("path"),
                    "line": root.get("line") if self.lines_are_current else None,
                    "originalLine": root.get("line"), "diffSide": "RIGHT",
                    "comments": {"nodes": [
                        {"databaseId": c["id"], "body": c["body"],
                         "author": {"login": c["user"]["login"]}}
                        for c in [root, *replies]]},
                })
            return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                  "nodes": nodes}}}}})
        return httpx.Response(200, json={"data": {}})


class _GitHubHub:
    provider_name, repo, number = "github", "o/r", 1

    def __init__(self) -> None:
        self.fake = _GitHubThreads()
        self.provider = _github(self.fake)

    def inline_ids(self) -> list[int]:
        return [c["id"] for c in self.fake.inline if "in_reply_to_id" not in c]

    def inline_count(self) -> int:
        return len(self.inline_ids())

    def root_for(self, path: str) -> int:
        return next(c["id"] for c in self.fake.inline if c.get("path") == path)

    def reply(self, root: int) -> None:
        self.fake.add_inline("I disagree", author=HUMAN, in_reply_to=root)

    def seed_foreign(self, body: str, path: str, line: int) -> int:
        cid = self.fake.add_inline(body, author=HUMAN)
        self.fake.inline[-1].update(path=path, line=line)
        return cid

    def resolved(self) -> set[int]:
        return set(self.fake.resolved)

    def summary(self) -> str:
        return self.fake.issue[0]["body"]

    def break_thread_listing(self) -> None:
        self.fake.threads_status = 500


# ─── GitLab ──────────────────────────────────────────────────────────


class _GitLabThreads(_FakeGitLab):
    """The v4 fake plus positioned diff discussions and `PUT .../discussions/{id}`."""

    def __init__(self) -> None:
        super().__init__()
        self.resolved_discussions: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url).split("?")[0], request.method
        if method == "POST" and url.endswith("/discussions"):
            form = self._form(request)
            nid = self.add_note(form["body"], inline=True)
            self.notes[-1]["position"] = {
                "new_path": form["position[new_path]"],
                "new_line": int(form["position[new_line]"]) if "position[new_line]" in form else None,
            }
            return httpx.Response(201, json={"id": f"d{nid}", "notes": [self.notes[-1]]})
        one = re.search(r"/discussions/(d\d+)$", url)
        if one and method == "PUT":
            self.resolved_discussions.add(one.group(1))
            return httpx.Response(200, json={"id": one.group(1)})
        return super().__call__(request)

    def _discussions_page(self, request: httpx.Request) -> httpx.Response:
        resp = super()._discussions_page(request)
        if resp.status_code != 200:
            return resp
        body = resp.json()
        for disc in body:
            for note in disc["notes"][:1]:
                note["resolved"] = disc["id"] in self.resolved_discussions
        return httpx.Response(200, json=body, headers=resp.headers)


class _GitLabHub:
    provider_name, repo, number = "gitlab", "group/proj", 5

    def __init__(self) -> None:
        self.fake = _GitLabThreads()
        self.provider = _gitlab(self.fake)

    def _diff_notes(self) -> list[dict]:
        return [n for n in self.fake.notes if n["type"] == "DiffNote" and n["id"]
                in {k for k, v in self.fake._discussion_of.items() if k == v}]

    def inline_ids(self) -> list[int]:
        return [n["id"] for n in self._diff_notes()]

    def inline_count(self) -> int:
        return len(self.inline_ids())

    def root_for(self, path: str) -> int:
        return next(n["id"] for n in self._diff_notes() if n["position"]["new_path"] == path)

    def reply(self, root: int) -> None:
        self.fake.add_note("I disagree", author=HUMAN, reply_to=root)

    def seed_foreign(self, body: str, path: str, line: int) -> int:
        nid = self.fake.add_note(body, inline=True, author=HUMAN)
        self.fake.notes[-1]["position"] = {"new_path": path, "new_line": line}
        return nid

    def resolved(self) -> set[int]:
        return {int(d.removeprefix("d")) for d in self.fake.resolved_discussions}

    def summary(self) -> str:
        return next(n["body"] for n in self.fake.notes if n["type"] is None)

    def break_thread_listing(self) -> None:
        self.fake.discussions_status = 500


# ─── Bitbucket ───────────────────────────────────────────────────────


class _BitbucketThreads(_FakeBitbucket):
    """The 2.0 fake plus `POST .../comments/{id}/resolve` and resolutions."""

    def __init__(self) -> None:
        super().__init__()
        self.resolve_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        one = re.fullmatch(
            r"/2.0/repositories/[^/]+/[^/]+/pullrequests/\d+/comments/(\d+)/resolve", path)
        if one and method == "POST":
            if self.resolve_status >= 400:
                return httpx.Response(self.resolve_status, json={"error": "nope"})
            for comment in self.comments:
                if comment["id"] == int(one.group(1)):
                    comment["resolution"] = {"type": "comment_resolution"}
            return httpx.Response(200, json={})
        return super().__call__(request)


class _BitbucketHub:
    provider_name, repo, number = "bitbucket", "ws/r", 3

    def __init__(self) -> None:
        self.fake = _BitbucketThreads()
        self.provider = _bitbucket(self.fake)

    def _roots(self) -> list[dict]:
        return [c for c in self.fake.comments if c.get("inline") and not c.get("parent")]

    def inline_ids(self) -> list[int]:
        return [c["id"] for c in self._roots()]

    def inline_count(self) -> int:
        return len(self.inline_ids())

    def root_for(self, path: str) -> int:
        return next(c["id"] for c in self._roots() if c["inline"]["path"] == path)

    def reply(self, root: int) -> None:
        self.fake.add_comment("I disagree", author=HUMAN, parent=root)

    def seed_foreign(self, body: str, path: str, line: int) -> int:
        cid = self.fake.add_comment(body, author=HUMAN, inline=True)
        self.fake.comments[-1]["inline"] = {"path": path, "to": line}
        return cid

    def resolved(self) -> set[int]:
        return {c["id"] for c in self.fake.comments if c.get("resolution")}

    def summary(self) -> str:
        return next(c["content"]["raw"] for c in self.fake.comments
                    if not c.get("inline") and not c.get("parent"))

    def break_thread_listing(self) -> None:
        self.fake.list_status = 500


HUBS = pytest.mark.parametrize("make_hub", [_GitHubHub, _GitLabHub, _BitbucketHub],
                               ids=["github", "gitlab", "bitbucket"])


def _first_review(hub, findings: int = 2, *, head: str = BASE) -> dict:
    """The whole-PR review that leaves the comments an increment must respect,
    posted at `head` (by default the commit an increment starts from)."""
    batch = _batch(hub.provider_name, hub.repo, hub.number, findings=findings)
    batch.pull_request.head_sha = head
    return hub.provider.post_review(batch)


# ─── the tests ───────────────────────────────────────────────────────


@HUBS
def test_every_post_answers_with_the_comments_it_created(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()

    response = _first_review(hub, findings=2)

    created = response["inline_comments"]
    assert sorted(c.path for c in created) == ["src/mod0.py", "src/mod1.py"]
    assert {c.comment_id for c in created} == set(hub.inline_ids())
    assert all(len(c.fingerprint) == 16 and len(c.finding_key) == 64 for c in created)
    assert "incremental" not in response, "a whole review reports nothing incremental"


@HUBS
def test_a_whole_review_still_replaces_the_comments_of_the_earlier_run(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)

    second = _first_review(hub, findings=2)

    assert hub.inline_count() == 2
    assert len(second["inline_comments"]) == 2


@HUBS
def test_an_incremental_post_adds_only_what_is_new_and_deletes_nothing(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)
    before = set(hub.inline_ids())

    # Finding 0 is already an open comment; finding 2 is new.
    response = hub.provider.post_review(_incremental(hub, [0, 2]))

    after = set(hub.inline_ids())
    assert before <= after, "an earlier comment was deleted"
    assert len(after - before) == 1, "the finding that was already posted was posted again"
    assert response["incremental"] is True
    assert response["findings_already_posted"] == 1
    (created,) = response["inline_comments"]
    assert created.path == "src/mod2.py" and created.comment_id in after - before


@HUBS
def test_a_duplicate_does_not_use_up_a_slot_of_the_inline_cap(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)
    batch = _incremental(hub, [0, 1, 2])
    batch.max_inline_comments = 1

    response = hub.provider.post_review(batch)

    (created,) = response["inline_comments"]
    assert created.path == "src/mod2.py", "the cap was spent on findings that were already comments"


@HUBS
def test_a_finding_a_few_lines_off_is_still_the_same_comment_a_far_one_is_new(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=1)
    near = _incremental(hub, [0])
    near.findings[0].line += 3
    far = _incremental(hub, [0])
    far.findings[0].line += 20

    assert hub.provider.post_review(near)["inline_comments"] == []
    assert len(hub.provider.post_review(far)["inline_comments"]) == 1


@HUBS
def test_a_thread_on_a_line_the_new_commits_removed_is_resolved(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)
    gone, kept = hub.root_for("src/mod1.py"), hub.root_for("src/mod0.py")

    response = hub.provider.post_review(
        _incremental(hub, [2], scope=_scope(**{"src/mod1.py": [11]})))

    assert hub.resolved() == {gone}
    assert kept not in hub.resolved()
    assert response["threads_resolved"] == 1
    assert hub.inline_count() == 3, "resolving is not deleting"
    assert "Incremental review" in hub.summary()
    assert "**2** still open" in hub.summary() and "**1** resolved" in hub.summary()


@HUBS
def test_a_thread_posted_on_an_earlier_push_is_not_judged_by_this_increments_line_numbers(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2, head="2" * 40)  # not the commit the increment starts from

    response = hub.provider.post_review(
        _incremental(hub, [2], scope=_scope(**{"src/mod1.py": [11]})))

    assert hub.resolved() == set(), "its line number may belong to another numbering"
    assert response["threads_resolved"] == 0


SHIFTED = """\
diff --git a/src/mod1.py b/src/mod1.py
--- a/src/mod1.py
+++ b/src/mod1.py
@@ -1,1 +1,11 @@
 first
+a
+b
+c
+d
+e
+f
+g
+h
+i
+j
"""


@HUBS
def test_a_finding_on_code_that_moved_down_is_still_the_comment_already_there(settings, make_hub) -> None:  # noqa: F811
    from src.review.diff import parse_unified_diff

    hub = make_hub()
    _first_review(hub, findings=2)
    before = set(hub.inline_ids())
    batch = _incremental(hub, [1])
    batch.pull_request.hunks = parse_unified_diff(SHIFTED)[0]
    batch.findings[0].line += 10  # ten lines were inserted above it

    response = hub.provider.post_review(batch)

    assert set(hub.inline_ids()) == before, "the same finding was posted a second time"
    assert response["findings_already_posted"] == 1


@HUBS
def test_two_findings_of_one_title_close_together_are_one_comment_by_design(settings, make_hub) -> None:  # noqa: F811
    # The fingerprint is rule + file + title, not the line: a second instance of
    # the same finding within the window is judged to be the one already there.
    hub = make_hub()
    _first_review(hub, findings=1)
    second_instance = _incremental(hub, [0])
    second_instance.findings[0].line += 2

    assert hub.provider.post_review(second_instance)["inline_comments"] == []


@HUBS
def test_a_thread_a_person_replied_in_stays_open_whatever_happened_to_the_line(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)
    hub.reply(hub.root_for("src/mod1.py"))

    response = hub.provider.post_review(
        _incremental(hub, [2], scope=_scope(**{"src/mod1.py": [11]})))

    assert hub.resolved() == set()
    assert response["threads_resolved"] == 0


@HUBS
def test_our_marker_on_somebody_elses_comment_is_not_ours(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    finding = _batch(hub.provider_name, hub.repo, hub.number, findings=1).findings[0]
    foreign = hub.seed_foreign(
        _format_finding_body(finding, MARKER, head_sha="abc123"), finding.file_path, finding.line)

    response = hub.provider.post_review(
        _incremental(hub, [0], scope=_scope(**{finding.file_path: [finding.line]})))

    assert len(response["inline_comments"]) == 1, "a quoted finding suppressed the real one"
    assert foreign not in hub.resolved(), "a person's comment was resolved"
    assert response["findings_already_posted"] == 0


@HUBS
def test_a_finding_on_the_old_side_goes_to_the_summary_not_to_a_line(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    batch = _incremental(hub, [0])
    batch.findings[0].side = HunkSide.LEFT
    batch.findings[0].title = "Removed guard"

    response = hub.provider.post_review(batch)

    assert response["inline_comments"] == []
    assert response["findings_demoted_to_summary"] == 1
    assert "Removed guard" in hub.summary()


@HUBS
def test_a_thread_listing_that_fails_posts_everything_and_resolves_nothing(settings, make_hub) -> None:  # noqa: F811
    hub = make_hub()
    _first_review(hub, findings=2)
    hub.break_thread_listing()

    response = hub.provider.post_review(
        _incremental(hub, [0, 2], scope=_scope(**{"src/mod1.py": [11]})))

    assert len(response["inline_comments"]) == 2, "without the list nothing can be called a duplicate"
    assert hub.resolved() == set()
    assert hub.inline_count() == 4
    assert "still open" not in hub.summary(), "the banner must not claim counts it could not read"


def test_a_bitbucket_without_resolution_support_is_told_so_and_the_review_goes_on(settings) -> None:  # noqa: F811
    hub = _BitbucketHub()
    _first_review(hub, findings=2)
    hub.fake.resolve_status = 404

    response = hub.provider.post_review(
        _incremental(hub, [2], scope=_scope(**{"src/mod1.py": [11]})))

    assert response["threads_resolved"] == 0
    assert response["threads_resolve_unsupported"] == 1
    assert len(response["inline_comments"]) == 1


def test_a_finding_marker_names_the_commit_it_was_posted_on(settings) -> None:  # noqa: F811
    from src.review.markers import parse_finding_marker

    finding = Finding(file_path="a.py", line=1, severity=FindingSeverity.ERROR,
                      title="t", body="b", agent="defect", rule_id="defect.x")

    body = _format_finding_body(finding, MARKER, head_sha="0123456789abcdef")

    short, sha = parse_finding_marker(body)
    assert len(short) == 16 and sha == "0123456789ab"
    assert parse_finding_marker(_format_finding_body(finding, MARKER, head_sha="head1"))[1] == "0" * 12, (
        "a head that is not a commit id still leaves the comment findable")
