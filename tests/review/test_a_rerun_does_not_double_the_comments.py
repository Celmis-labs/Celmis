"""Re-running a review posted a second full set of inline comments.

All three providers wrote the same marker onto every inline comment. Bitbucket
walked the pages and deleted everything carrying it; GitHub and GitLab never
looked for it at all — each deleted its summary and left the inline comments
standing, so a branch pushed three times carried three sets of them, up to
`max_inline_comments` each.

The second bug is in the fix — and Bitbucket, the template the fix was copied
from, had it too. Deleting on `marker in body` alone deletes a HUMAN comment
that merely quotes one of our findings: "Quote reply" copies the quoted
comment's RAW markdown, and our marker is an HTML comment inside it. So the
predicate is now marker AND authorship on every provider, the author coming
from a GET /user looked up once per provider — compared on Bitbucket by
uuid/account_id, the ids that survive an account rename — and when that lookup
cannot be read, nothing is deleted at all.

The third bug is in the second fix. Nothing inline was ever deleted before, so
no reply could be orphaned; once our own inline comments started going on every
push, a root a human had answered went with them and left the reply hanging.
A foreign reply now protects its root: GitHub marks replies with
`in_reply_to_id`, Bitbucket with `parent`, and GitLab needs one extra pass over
/discussions because its flat notes listing cannot see threads at all.

The fakes below are the smallest servers that can tell all of this apart: they
STORE what is posted and drop what is deleted, so "exactly one set remains after
a re-run" is a fact about their state rather than a count of requests; they serve
GET /user; every comment in them has an author, because a comment without one
does not exist on any of the three providers and a fixture without one cannot
show the difference between our comment and somebody else's; and they model
reply threads, because "a human reply protects its root" cannot be shown
without one.
"""

from __future__ import annotations

import itertools
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from src.review.models import (
    Finding,
    FindingSeverity,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.providers import bitbucket as bitbucket_module
from src.review.providers import github as github_module
from src.review.providers import gitlab as gitlab_module
from src.review.providers.base import _format_finding_body
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from src.review.settings import ReviewSettings

# Deliberately not the shipped default: if any of this matched a hardcoded
# string instead of `settings.comment_marker`, these tests would find nothing to
# delete and the end-to-end assertions would fail.
MARKER = "<!-- celmis:review:under-test -->"
OTHER_BOT_MARKER = "<!-- some-other-linter -->"

# Who wrote what. BOT is the account the token posts as — GET /user answers with
# it — so a comment authored by BOT and carrying MARKER is one of ours and
# nothing else is.
BOT = "celmis-review-bot"
HUMAN = "dana"
OTHER_BOT = "sonarcloud[bot]"


def _install_settings(monkeypatch, **overrides) -> ReviewSettings:
    fields: dict = {
        "comment_marker": MARKER,
        "replace_on_synchronize": True,
        "max_inline_comments": 20,
    }
    fields.update(overrides)
    cfg = ReviewSettings(**fields)
    for module in (github_module, gitlab_module, bitbucket_module):
        monkeypatch.setattr(module, "get_review_settings", lambda cfg=cfg: cfg)
    return cfg


@pytest.fixture
def settings(monkeypatch) -> ReviewSettings:
    return _install_settings(monkeypatch)


@pytest.fixture
def settings_keep_history(monkeypatch) -> ReviewSettings:
    """`replace_on_synchronize=False` — every run's comments kept as a record.

    Its own fixture because the one above pins the flag True, which is how the
    OFF path came to ship with no test over it at all.
    """
    return _install_settings(monkeypatch, replace_on_synchronize=False)


def _patch_client(provider, transport: httpx.MockTransport) -> None:
    """Swap the internal httpx.Client for a mock-backed one (as test_providers)."""
    provider._http.close()
    provider._http = httpx.Client(
        transport=transport,
        timeout=10.0,
        headers={"User-Agent": "code-analyzer/0.1"},
    )


def _finding(i: int) -> Finding:
    return Finding(
        file_path=f"src/mod{i}.py", line=10 + i,
        severity=FindingSeverity.WARNING,
        title=f"finding {i}", body="because", agent="quality",
        rule_id=f"q.rule-{i}",
    )


def _batch(
    provider_name: str, repo: str, number: int, findings: int = 2, url: str = "",
) -> ReviewBatch:
    pr = PullRequest(
        provider=provider_name, repo=repo, number=number,
        title="t", description="d", author="u",
        base_ref="main", base_sha="base1", head_ref="feat", head_sha="head1",
        state="open", url=url,
    )
    return ReviewBatch(
        pull_request=pr,
        verdict=ReviewVerdict.COMMENT,
        findings=[_finding(i) for i in range(findings)],
    )


def _quote_reply(quoted: str, reply: str) -> str:
    """What "Quote reply" drops into the comment box.

    The quoted comment's RAW markdown, every line prefixed "> " — HTML comments
    included, because markdown does not render them and the copy is textual.
    Built from `_format_finding_body`'s real output rather than hand-written, so
    the fixture cannot drift away from what we actually post.
    """
    lines = [f"> {line}" if line else ">" for line in quoted.splitlines()]
    return "\n".join(lines) + f"\n\n{reply}"


# The three ways a viewer lookup can come back unusable. All three must delete
# nothing: an identity we cannot read is not an identity we can match against.
UNREADABLE_VIEWER = [
    pytest.param(500, {"message": "server error"}, id="lookup-fails"),
    pytest.param(200, {}, id="body-has-no-login"),
    pytest.param(200, ["not", "an", "object"], id="body-is-not-an-object"),
]


# ─── GitHub ─────────────────────────────────────────────────────────


class _FakeGitHub:
    """Enough of the GitHub REST surface to run post_review twice.

    Two collections, exactly as GitHub has them: `inline` is what
    /pulls/{n}/comments returns and /pulls/comments/{id} deletes; `issue` is
    what /issues/{n}/comments returns and /issues/comments/{id} deletes or
    patches. Confusing the two paths is the mistake this fake exists to catch.

    GET /user answers with `viewer`, and every comment carries a `user` — a
    seeded comment defaults to the viewer's own login, i.e. to something a
    previous run of ours left behind. Pass `author=` for anyone else, and
    `in_reply_to=` for a thread reply, which the real listing marks with
    `in_reply_to_id` exactly like this.
    """

    def __init__(self, *, page_size: int = 2, viewer: str = BOT) -> None:
        self.page_size = page_size
        self.viewer = viewer
        self.inline: list[dict] = []
        self.issue: list[dict] = []
        self.reviews: list[dict] = []
        self._ids = itertools.count(1000)
        self.list_status = 200
        self.delete_status = 204
        self.list_requests = 0
        self.user_status = 200
        self.user_body: object = {"login": viewer}
        self.user_requests = 0
        #: Fail every listing page >= this one — a listing that dies MIDWAY,
        #: which is not the same failure as one that never starts.
        self.fail_from_page: int | None = None
        #: Refuse any review whose `comments` is non-empty with the 422 the
        #: real API answers when one anchor points outside the diff.
        self.reject_inline = False

    # — seeding —
    def add_inline(
        self, body: str, *, author: str | None = None, in_reply_to: int | None = None,
    ) -> int:
        cid = next(self._ids)
        comment = {"id": cid, "body": body, "user": {"login": author or self.viewer}}
        if in_reply_to is not None:
            comment["in_reply_to_id"] = in_reply_to
        self.inline.append(comment)
        return cid

    def add_inline_raw(self, comment: dict) -> int:
        """Seed a comment exactly as given — for payloads the real API should
        never produce but a proxy or truncated cache can."""
        cid = comment.setdefault("id", next(self._ids))
        self.inline.append(comment)
        return cid

    def add_issue(self, body: str, *, author: str | None = None) -> int:
        cid = next(self._ids)
        self.issue.append(
            {"id": cid, "body": body, "user": {"login": author or self.viewer}},
        )
        return cid

    def bodies(self, items: list[dict]) -> list[str]:
        return [c["body"] for c in items]

    @staticmethod
    def ids(items: list[dict]) -> list[int]:
        return [c["id"] for c in items]

    # — transport —
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/user":
            self.user_requests += 1
            return httpx.Response(self.user_status, json=self.user_body)

        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/comments", path):
            return self._page(request, self.inline)
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            return self._page(request, self.issue)

        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews", path):
            payload = json.loads(request.content)
            if self.reject_inline and payload.get("comments"):
                return httpx.Response(
                    422, json={"message": "Pull request review thread line "
                                          "must be part of the diff"},
                )
            for comment in payload.get("comments") or []:
                self.add_inline(comment["body"])
            self.reviews.append(payload)
            return httpx.Response(
                200, json={"id": next(self._ids), "html_url": "https://gh/pr/1"},
            )
        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            cid = self.add_issue(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": cid})

        inline_one = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/comments/(\d+)", path)
        if inline_one and method == "DELETE":
            return self._delete(self.inline, int(inline_one.group(1)))

        issue_one = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path)
        if issue_one and method == "DELETE":
            return self._delete(self.issue, int(issue_one.group(1)))
        if issue_one and method == "PATCH":
            cid = int(issue_one.group(1))
            body = json.loads(request.content)["body"]
            for comment in self.issue:
                if comment["id"] == cid:
                    comment["body"] = body
                    return httpx.Response(200, json=comment)
            return httpx.Response(404, json={"message": "not found"})

        return httpx.Response(404, json={"message": f"unrouted {method} {path}"})

    def _page(self, request: httpx.Request, items: list[dict]) -> httpx.Response:
        self.list_requests += 1
        if self.list_status >= 400:
            return httpx.Response(self.list_status, json={"message": "boom"})
        page = int(request.url.params.get("page", 1))
        if self.fail_from_page and page >= self.fail_from_page:
            return httpx.Response(500, json={"message": "boom"})
        start = (page - 1) * self.page_size
        chunk = items[start:start + self.page_size]
        headers = {}
        if start + self.page_size < len(items):
            nxt = str(request.url.copy_set_param("page", page + 1))
            headers["Link"] = f'<{nxt}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _delete(self, items: list[dict], cid: int) -> httpx.Response:
        if self.delete_status >= 400:
            return httpx.Response(self.delete_status, json={"message": "nope"})
        items[:] = [c for c in items if c["id"] != cid]
        return httpx.Response(self.delete_status)


def _github(fake: _FakeGitHub) -> GitHubPRProvider:
    provider = GitHubPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestGitHubRerun:
    def test_a_human_quoting_one_of_our_findings_survives(self, settings) -> None:
        """The hole the author check closes, and the reason it exists.

        GitHub's "Quote reply" copies the quoted comment's raw markdown into the
        new comment — our marker is an HTML comment sitting inside that
        markdown, so a reviewer who quotes a finding to argue with it writes a
        comment that contains our marker. Under `marker in body` the next push
        deleted their words. Nothing about the body can distinguish the two;
        only the author can.
        """
        ours = _format_finding_body(_finding(0), MARKER)
        quoted = _quote_reply(ours, "Disagree — the caller already holds the lock.")
        assert MARKER in quoted, "the fixture stopped modelling the bug"

        fake = _FakeGitHub()
        stale_id = fake.add_inline(ours)                       # ours: replaced
        human_id = fake.add_inline(quoted, author=HUMAN)       # theirs: untouchable

        provider = _github(fake)
        provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        surviving = fake.ids(fake.inline)
        assert human_id in surviving, (
            "a human comment that merely QUOTES one of our findings was deleted"
        )
        assert stale_id not in surviving, "our own previous comment was not replaced"

    def test_a_human_quoting_our_summary_survives(self, settings) -> None:
        """Same copy, other collection. A quoted summary is an issue comment,
        and the cleanup deletes surplus issue comments outright — so if the
        author check is missing here, a reviewer's reply to the summary is what
        gets deleted, not merely dropped from an update."""
        fake = _FakeGitHub()
        ours = f"{MARKER}\n## 🤖 Code Review for PR #1"
        fake.add_issue(ours)
        human_id = fake.add_issue(
            _quote_reply(ours, "Numbers look off, see below."), author=HUMAN,
        )

        provider = _github(fake)
        provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert human_id in fake.ids(fake.issue), (
            "a human reply quoting the summary was deleted"
        )

    def test_only_the_comments_this_bot_wrote_are_removed(self, settings) -> None:
        """Marker AND author, never one of them: the marker says the comment has
        the shape of ours, the author says it is ours. A reviewer's note and
        another tool's comment fail on both counts and stay put."""
        fake = _FakeGitHub()
        fake.add_inline(f"stale finding\n\n{MARKER}")
        fake.add_inline("please rename this variable", author=HUMAN)
        fake.add_inline(f"complexity too high\n{OTHER_BOT_MARKER}", author=OTHER_BOT)

        provider = _github(fake)
        provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        surviving = fake.bodies(fake.inline)
        assert "please rename this variable" in surviving
        assert any(OTHER_BOT_MARKER in b for b in surviving)
        assert not any("stale finding" in b for b in surviving)

    def test_our_marker_under_another_account_is_not_ours(self, settings) -> None:
        """A second Celmis install, reviewing the same PR with its own token,
        writes the same default marker. Its comments are not ours to delete —
        it would delete ours right back and neither PR would ever settle."""
        fake = _FakeGitHub()
        theirs = fake.add_inline(f"stale finding\n\n{MARKER}", author="celmis-staging")

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert theirs in fake.ids(fake.inline)
        assert result["cleanup"]["deleted"] == 0

    @pytest.mark.parametrize(("status", "body"), UNREADABLE_VIEWER)
    def test_an_unreadable_viewer_deletes_nothing(
        self, settings, status, body,
    ) -> None:
        """Fail closed. Without knowing who we are, the marker alone is not
        proof of authorship — so a lookup that 500s, one that answers with no
        login, and one that answers with something that is not an object at all
        must each delete NOTHING, and say the cleanup did not finish rather
        than let "review posted" be read as "duplicates gone"."""
        fake = _FakeGitHub()
        fake.user_status = status
        fake.user_body = body
        stale_inline = fake.add_inline(f"stale finding\n\n{MARKER}")
        stale_summary = fake.add_issue(f"{MARKER}\n## old summary")

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert stale_inline in fake.ids(fake.inline), "deleted without knowing who we are"
        assert stale_summary in fake.ids(fake.issue)
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert len(fake.reviews) == 1, "the review itself must still post"

    def test_the_cleanup_reaches_past_the_first_page(self, settings) -> None:
        """A cleanup that reads page one and stops leaves duplicates behind AND
        reports success, which is worse than not cleaning at all — a large PR
        carries hundreds of comments against a page size of 100."""
        fake = _FakeGitHub(page_size=2)
        for i in range(5):
            fake.add_inline(f"stale {i}\n\n{MARKER}")
        fake.add_inline("a human, on the last page", author=HUMAN)

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert not any("stale" in b for b in fake.bodies(fake.inline)), (
            "comments past page one survived the cleanup"
        )
        assert "a human, on the last page" in fake.bodies(fake.inline)
        assert result["cleanup"]["deleted"] == 5
        assert result["cleanup"]["complete"] is True
        assert fake.user_requests == 1, (
            "GET /user once per provider, not once per page — this runs against "
            "a rate limit shared with the review itself"
        )

    def test_a_listing_that_fails_still_lets_the_review_post(self, settings) -> None:
        """A review with duplicate comments beats no review — but the caller is
        told the cleanup did not finish, so 'posted' is not read as 'tidy'."""
        fake = _FakeGitHub()
        fake.list_status = 500

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=2))
        provider.close()

        assert len(fake.reviews) == 1
        assert result["comments_posted"] == 2
        assert result["cleanup"]["complete"] is False

    def test_a_delete_that_fails_is_counted_not_raised(self, settings) -> None:
        fake = _FakeGitHub()
        fake.add_inline(f"stale finding\n\n{MARKER}")
        fake.delete_status = 403

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert len(fake.reviews) == 1, "a refused delete aborted the review"
        assert result["cleanup"]["failed"] == 1
        assert result["cleanup"]["complete"] is False
        assert any("stale finding" in b for b in fake.bodies(fake.inline))

    def test_a_malformed_author_neither_crashes_nor_matches(self, settings) -> None:
        """{"user": "octocat"} — a truncated cache, a proxy rewrite. The old
        predicate did `(comment.get("user") or {}).get("login")` and the
        AttributeError sailed past the except around the listing, taking the
        whole review down. A comment whose author cannot be read is not ours
        to delete, and not a reason to stop reviewing either."""
        fake = _FakeGitHub()
        weird = fake.add_inline_raw(
            {"body": f"who wrote this?\n\n{MARKER}", "user": "octocat"},
        )
        stale = fake.add_inline(f"stale finding\n\n{MARKER}")

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert len(fake.reviews) == 1, "a malformed neighbour crashed the review"
        assert weird in fake.ids(fake.inline), (
            "deleted a comment whose author cannot be read"
        )
        assert stale not in fake.ids(fake.inline), (
            "a malformed neighbour stopped the real cleanup"
        )
        assert result["cleanup"]["deleted"] == 1

    def test_a_human_reply_protects_its_root(self, settings) -> None:
        """Nothing inline was ever deleted before this cleanup existed, so no
        reply could be orphaned. Now our own inline comments go on every push
        — and a root a human answered is the context their reply hangs from.
        The listing marks replies with `in_reply_to_id`; a root with a foreign
        reply is kept, counted, and does not fail the cleanup."""
        fake = _FakeGitHub()
        root = fake.add_inline(_format_finding_body(_finding(0), MARKER))
        reply = fake.add_inline(
            "Good catch — fixing in the next push.", author=HUMAN, in_reply_to=root,
        )
        lone = fake.add_inline(f"stale finding\n\n{MARKER}")

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        surviving = fake.ids(fake.inline)
        assert root in surviving, "a root with a human reply was deleted"
        assert reply in surviving, "the human reply went down with its root"
        assert lone not in surviving, "an unanswered stale comment must still go"
        assert result["cleanup"]["kept_threaded"] == 1
        assert result["cleanup"]["complete"] is True

    def test_a_half_read_listing_deletes_nothing(self, settings) -> None:
        """Page one read, page two refused. The partial set MUST NOT be
        deleted: the unread pages may hold the human reply that would have
        protected one of these roots, and a cleanup that guesses is how
        replies get orphaned. Leave the duplicates, say it did not finish."""
        fake = _FakeGitHub(page_size=2)
        for i in range(5):
            fake.add_inline(f"stale {i}\n\n{MARKER}")
        fake.fail_from_page = 2

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert sum("stale" in b for b in fake.bodies(fake.inline)) == 5, (
            "a half-read listing deleted a partial set"
        )
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert len(fake.reviews) == 1, "the review itself must still post"

    def test_the_summary_is_updated_in_place(self, settings) -> None:
        """Qodo's publish_persistent_comment pattern. Deleting and re-posting
        the summary gives it a new id every push, so a link to it from a ticket
        or a chat message stops resolving; the body is rewritten instead."""
        fake = _FakeGitHub()
        original_id = fake.add_issue(f"{MARKER}\n## old summary")

        provider = _github(fake)
        result = provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert len(fake.issue) == 1
        assert fake.issue[0]["id"] == original_id
        assert result["summary_comment_id"] == original_id
        assert "old summary" not in fake.issue[0]["body"]
        assert MARKER in fake.issue[0]["body"]

    def test_a_rerun_leaves_exactly_one_set(self, settings) -> None:
        """The shape the bug report described: post, re-run, count."""
        fake = _FakeGitHub()
        provider = _github(fake)
        batch = _batch("github", "o/r", 1, findings=3)

        provider.post_review(batch)
        after_first = list(fake.bodies(fake.inline))
        provider.post_review(batch)
        provider.close()

        assert len(after_first) == 3
        assert len(fake.inline) == 3, (
            f"a re-run left {len(fake.inline)} inline comments where 3 belong"
        )
        assert len(fake.issue) == 1
        assert len(fake.reviews) == 2

    def test_the_review_body_is_a_pointer_not_a_second_summary(self, settings) -> None:
        """The duplication no cleanup can reach: a submitted review is
        immutable, so while its body carried the full summary, every re-run
        stacked one more full copy onto the PR timeline. The body is a pointer
        now — verdict plus a link — and however many times the review runs,
        the full summary exists exactly once, in the persistent comment."""
        fake = _FakeGitHub()
        provider = _github(fake)
        batch = _batch("github", "o/r", 1, findings=2, url="https://gh/o/r/pull/1")

        provider.post_review(batch)
        result = provider.post_review(batch)
        provider.close()

        summary_bodies = [b for b in fake.bodies(fake.issue) if "## 🤖 Code Review" in b]
        review_bodies = [r.get("body", "") for r in fake.reviews]
        assert len(summary_bodies) == 1, "the full summary must exist exactly once"
        assert not any("## 🤖 Code Review" in b for b in review_bodies), (
            "a review body carries the full summary again — immutable, so "
            "this is one copy per re-run and nothing can delete it"
        )
        # The timeline stays readable without a click: the verdict is on
        # every review body, first run and re-run alike.
        assert all("**COMMENT**" in b for b in review_bodies)
        # The re-run links to the comment the upsert keeps alive. Its id is
        # stable, so the link keeps resolving push after push.
        summary_id = result["summary_comment_id"]
        assert f"#issuecomment-{summary_id}" in review_bodies[1]

    def test_the_first_review_does_not_link_to_a_comment_that_is_not_there(
        self, settings,
    ) -> None:
        """On the first run the persistent comment does not exist until after
        the review is submitted — a review that fails to post must not update
        the summary first — so the first body points in words, not with a
        dead anchor."""
        fake = _FakeGitHub()
        provider = _github(fake)
        provider.post_review(
            _batch("github", "o/r", 1, findings=1, url="https://gh/o/r/pull/1"),
        )
        provider.close()

        body = fake.reviews[0].get("body", "")
        assert "#issuecomment-" not in body, "a link to a comment that does not exist yet"
        assert "**COMMENT**" in body
        assert "summary" in body.lower(), "the pointer does not say where the summary is"

    def test_unanchored_findings_land_in_the_persistent_comment(self, settings) -> None:
        """The 422 fallback used to fold the refused findings into the review
        body — which is immutable, so a PR whose anchors kept being refused
        collected one full findings list per push. They go into the persistent
        comment now, which is rewritten in place: one copy, however many runs."""
        fake = _FakeGitHub()
        fake.reject_inline = True
        provider = _github(fake)
        batch = _batch("github", "o/r", 1, findings=2)

        provider.post_review(batch)
        provider.post_review(batch)
        provider.close()

        assert len(fake.reviews) == 2, "the anchor-less retry must still post"
        assert all(r.get("comments") == [] for r in fake.reviews)
        assert not any("finding 0" in r.get("body", "") for r in fake.reviews), (
            "the refused findings are back in the immutable review body"
        )
        assert len(fake.issue) == 1
        summary = fake.issue[0]["body"]
        assert summary.count("could not be anchored") == 1
        assert "src/mod0.py" in summary and "finding 0" in summary, (
            "the refused findings were dropped instead of moved"
        )


# ─── GitLab ─────────────────────────────────────────────────────────


class _FakeGitLab:
    """Enough of the GitLab v4 surface to run post_review twice.

    One collection, two kinds: a note posted to /discussions comes back from
    /notes tagged `type: "DiffNote"` with a `position`, a note posted to /notes
    comes back untagged. That is how the provider tells an inline comment (to be
    deleted) from the summary (to be updated in place).

    GET /user answers with `viewer`, and every note carries an `author` — a
    seeded note defaults to the viewer, i.e. to something a previous run of ours
    left behind. Pass `author=` for anyone else.

    Threads live on a separate endpoint, exactly as on the real API: the flat
    /notes payload says nothing about who replied to what, and GET /discussions
    returns the same notes grouped into discussions. `reply_to=` puts a note
    into the discussion of the note it answers.
    """

    def __init__(self, *, page_size: int = 2, viewer: str = BOT) -> None:
        self.page_size = page_size
        self.viewer = viewer
        self.notes: list[dict] = []
        self._ids = itertools.count(500)
        self.list_status = 200
        self.delete_status = 204
        self.list_requests = 0
        self.user_status = 200
        self.user_body: object = {"username": viewer}
        self.user_requests = 0
        self.discussions_status = 200
        self.discussion_requests = 0
        #: Fail every listing page >= this one — a listing that dies MIDWAY,
        #: which is not the same failure as one that never starts.
        self.fail_from_page: int | None = None
        #: note id → id of the note whose discussion it belongs to
        self._discussion_of: dict[int, int] = {}

    def add_note(
        self,
        body: str,
        *,
        inline: bool = False,
        system: bool = False,
        author: str | None = None,
        reply_to: int | None = None,
    ) -> int:
        nid = next(self._ids)
        self.notes.append({
            "id": nid,
            "body": body,
            "system": system,
            "type": "DiffNote" if inline else None,
            "position": {"new_line": 1} if inline else None,
            "author": {"username": author or self.viewer},
        })
        self._discussion_of[nid] = (
            self._discussion_of.get(reply_to, reply_to)
            if reply_to is not None else nid
        )
        return nid

    def add_note_raw(self, note: dict) -> int:
        """Seed a note exactly as given — for payloads the real API should
        never produce but a proxy or truncated cache can."""
        nid = note.setdefault("id", next(self._ids))
        self.notes.append(note)
        self._discussion_of[nid] = nid
        return nid

    def bodies(self) -> list[str]:
        return [n["body"] for n in self.notes]

    def ids(self) -> list[int]:
        return [n["id"] for n in self.notes]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        method = request.method

        if method == "GET" and url.endswith("/user"):
            self.user_requests += 1
            return httpx.Response(self.user_status, json=self.user_body)
        if method == "GET" and url.endswith("/versions"):
            return httpx.Response(200, json=[])
        if method == "POST" and url.endswith("/discussions"):
            nid = self.add_note(self._form(request)["body"], inline=True)
            return httpx.Response(201, json={"id": nid})
        if method == "GET" and url.endswith("/discussions"):
            return self._discussions_page(request)
        if method == "GET" and url.endswith("/notes"):
            return self._page(request)
        if method == "POST" and url.endswith("/notes"):
            nid = self.add_note(self._form(request)["body"])
            return httpx.Response(201, json={"id": nid})

        one = re.search(r"/notes/(\d+)$", url)
        if one and method == "DELETE":
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, json={"message": "nope"})
            nid = int(one.group(1))
            self.notes[:] = [n for n in self.notes if n["id"] != nid]
            return httpx.Response(self.delete_status)
        if one and method == "PUT":
            nid = int(one.group(1))
            for note in self.notes:
                if note["id"] == nid:
                    note["body"] = self._form(request)["body"]
                    return httpx.Response(200, json=note)
            return httpx.Response(404, json={"message": "not found"})

        return httpx.Response(404, json={"message": f"unrouted {method} {url}"})

    @staticmethod
    def _form(request: httpx.Request) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in parse_qs(request.content.decode()).items()
        }

    def _page(self, request: httpx.Request) -> httpx.Response:
        self.list_requests += 1
        if self.list_status >= 400:
            return httpx.Response(self.list_status, json={"message": "boom"})
        page = int(request.url.params.get("page", 1))
        if self.fail_from_page and page >= self.fail_from_page:
            return httpx.Response(500, json={"message": "boom"})
        start = (page - 1) * self.page_size
        chunk = self.notes[start:start + self.page_size]
        headers = {}
        if start + self.page_size < len(self.notes):
            headers["X-Next-Page"] = str(page + 1)
        return httpx.Response(200, json=chunk, headers=headers)

    def _discussions_page(self, request: httpx.Request) -> httpx.Response:
        self.discussion_requests += 1
        if self.discussions_status >= 400:
            return httpx.Response(self.discussions_status, json={"message": "boom"})
        page = int(request.url.params.get("page", 1))
        if self.fail_from_page and page >= self.fail_from_page:
            return httpx.Response(500, json={"message": "boom"})
        groups: dict[int, list[dict]] = {}
        for note in self.notes:
            key = self._discussion_of.get(note["id"], note["id"])
            groups.setdefault(key, []).append(note)
        discussions = [
            {"id": f"d{key}", "individual_notes": len(notes) == 1, "notes": notes}
            for key, notes in groups.items()
        ]
        start = (page - 1) * self.page_size
        chunk = discussions[start:start + self.page_size]
        headers = {}
        if start + self.page_size < len(discussions):
            headers["X-Next-Page"] = str(page + 1)
        return httpx.Response(200, json=chunk, headers=headers)


def _gitlab(fake: _FakeGitLab) -> GitLabPRProvider:
    provider = GitLabPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestGitLabRerun:
    def test_a_human_quoting_one_of_our_findings_survives(self, settings) -> None:
        """The same hole GitHub had, through the same door: quoting a note
        copies its raw markdown, marker and all, so the body of a reviewer's
        rebuttal is indistinguishable from ours. Only the author separates
        them."""
        ours = _format_finding_body(_finding(0), MARKER)
        quoted = _quote_reply(ours, "Disagree — the caller already holds the lock.")
        assert MARKER in quoted, "the fixture stopped modelling the bug"

        fake = _FakeGitLab()
        stale_id = fake.add_note(ours, inline=True)
        human_id = fake.add_note(quoted, inline=True, author=HUMAN)

        provider = _gitlab(fake)
        provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        surviving = fake.ids()
        assert human_id in surviving, (
            "a human note that merely QUOTES one of our findings was deleted"
        )
        assert stale_id not in surviving, "our own previous note was not replaced"

    def test_a_human_quoting_our_summary_survives(self, settings) -> None:
        """A quoted summary is a top-level note, and a top-level note is what
        the cleanup hands back as the summary to rewrite. Without the author
        check, a reviewer's quoted reply gets our new summary PUT over it."""
        fake = _FakeGitLab()
        ours = f"{MARKER}\n## 🤖 Code Review for MR !5"
        fake.add_note(ours)
        human_body = _quote_reply(ours, "Numbers look off, see below.")
        human_id = fake.add_note(human_body, author=HUMAN)

        provider = _gitlab(fake)
        provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        theirs = [n for n in fake.notes if n["id"] == human_id]
        assert theirs, "a human reply quoting the summary was deleted"
        assert theirs[0]["body"] == human_body, (
            "our summary was written over a human's note"
        )

    def test_only_the_notes_this_bot_wrote_are_removed(self, settings) -> None:
        """A human's note and another tool's note carry no marker of ours and
        no authorship of ours. A system note is skipped whatever it contains —
        GitLab writes those, and they are not ours to delete."""
        fake = _FakeGitLab()
        fake.add_note(f"stale finding\n\n{MARKER}", inline=True)
        fake.add_note("please rename this variable", inline=True, author=HUMAN)
        fake.add_note(
            f"complexity too high\n{OTHER_BOT_MARKER}", inline=True, author=OTHER_BOT,
        )
        fake.add_note(f"changed the description {MARKER}", system=True)

        provider = _gitlab(fake)
        provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        surviving = fake.bodies()
        assert "please rename this variable" in surviving
        assert any(OTHER_BOT_MARKER in b for b in surviving)
        assert any(b.startswith("changed the description") for b in surviving)
        assert not any(b.startswith("stale finding") for b in surviving)

    def test_our_marker_under_another_account_is_not_ours(self, settings) -> None:
        """A second Celmis install reviewing the same MR with its own token
        writes the same default marker. Deleting its notes would start a war
        neither install ever wins."""
        fake = _FakeGitLab()
        theirs = fake.add_note(
            f"stale finding\n\n{MARKER}", inline=True, author="celmis-staging",
        )

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert theirs in fake.ids()
        assert result["cleanup"]["deleted"] == 0

    @pytest.mark.parametrize(("status", "body"), UNREADABLE_VIEWER)
    def test_an_unreadable_viewer_deletes_nothing(
        self, settings, status, body,
    ) -> None:
        """Fail closed, same as GitHub: an identity we cannot read is not an
        identity we can match a note's author against, so nothing goes."""
        fake = _FakeGitLab()
        fake.user_status = status
        fake.user_body = body
        stale_inline = fake.add_note(f"stale finding\n\n{MARKER}", inline=True)
        stale_summary = fake.add_note(f"{MARKER}\n## old summary")

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        surviving = fake.ids()
        assert stale_inline in surviving, "deleted without knowing who we are"
        assert stale_summary in surviving
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert result["discussions_posted"] == 1, "the review itself must still post"

    def test_the_cleanup_reaches_past_the_first_page(self, settings) -> None:
        fake = _FakeGitLab(page_size=2)
        for i in range(5):
            fake.add_note(f"stale {i}\n\n{MARKER}", inline=True)
        fake.add_note("a human, on the last page", inline=True, author=HUMAN)

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert not any(b.startswith("stale ") for b in fake.bodies()), (
            "notes past page one survived the cleanup"
        )
        assert "a human, on the last page" in fake.bodies()
        assert result["cleanup"]["deleted"] == 5
        assert result["cleanup"]["complete"] is True
        assert fake.user_requests == 1, (
            "GET /user once per provider, not once per page"
        )

    def test_a_listing_that_fails_still_lets_the_review_post(self, settings) -> None:
        fake = _FakeGitLab()
        fake.list_status = 500

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=2))
        provider.close()

        assert result["discussions_posted"] == 2
        assert result["cleanup"]["complete"] is False

    def test_a_delete_that_fails_is_counted_not_raised(self, settings) -> None:
        fake = _FakeGitLab()
        fake.add_note(f"stale finding\n\n{MARKER}", inline=True)
        fake.delete_status = 403

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert result["discussions_posted"] == 1, "a refused delete aborted the review"
        assert result["cleanup"]["failed"] == 1
        assert result["cleanup"]["complete"] is False
        assert any(b.startswith("stale finding") for b in fake.bodies())

    def test_a_malformed_author_neither_crashes_nor_matches(self, settings) -> None:
        """{"author": "octocat"} — a truncated cache, a proxy rewrite. The old
        predicate did `(note.get("author") or {}).get("username")` and the
        AttributeError sailed past the except around the listing, taking the
        whole review down. A note whose author cannot be read is not ours to
        delete, and not a reason to stop reviewing either."""
        fake = _FakeGitLab()
        weird = fake.add_note_raw({
            "body": f"who wrote this?\n\n{MARKER}",
            "system": False,
            "type": "DiffNote",
            "position": {"new_line": 1},
            "author": "octocat",
        })
        stale = fake.add_note(f"stale finding\n\n{MARKER}", inline=True)

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert result["discussions_posted"] == 1, (
            "a malformed neighbour crashed the review"
        )
        assert weird in fake.ids(), "deleted a note whose author cannot be read"
        assert stale not in fake.ids(), (
            "a malformed neighbour stopped the real cleanup"
        )
        assert result["cleanup"]["deleted"] == 1

    def test_a_human_reply_protects_its_root(self, settings) -> None:
        """Nothing inline was ever deleted before this cleanup existed, so no
        reply could be orphaned. Now our own inline notes go on every push —
        and a root a human answered is the context their reply hangs from.
        The flat /notes listing cannot see that; the /discussions pass can."""
        fake = _FakeGitLab()
        root = fake.add_note(_format_finding_body(_finding(0), MARKER), inline=True)
        reply = fake.add_note(
            "Good catch — fixing in the next push.",
            inline=True, author=HUMAN, reply_to=root,
        )
        lone = fake.add_note(f"stale finding\n\n{MARKER}", inline=True)

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        surviving = fake.ids()
        assert root in surviving, "a root with a human reply was deleted"
        assert reply in surviving, "the human reply went down with its root"
        assert lone not in surviving, "an unanswered stale note must still go"
        assert result["cleanup"]["kept_threaded"] == 1
        assert result["cleanup"]["complete"] is True
        assert fake.user_requests == 1, (
            "GET /user once per provider, across both listing passes"
        )

    def test_a_thread_listing_that_fails_withholds_the_deletes(self, settings) -> None:
        """/notes read fine, /discussions did not. The flat listing cannot
        tell a safe delete from an orphaning one on its own — so when the
        answer cannot be read, no delete is made and the cleanup says it did
        not finish."""
        fake = _FakeGitLab()
        stale = fake.add_note(f"stale finding\n\n{MARKER}", inline=True)
        fake.discussions_status = 500

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert stale in fake.ids(), (
            "deleted without knowing whether a human had answered it"
        )
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert result["discussions_posted"] == 1, "the review itself must still post"

    def test_a_half_read_listing_deletes_nothing(self, settings) -> None:
        """Page one read, page two refused. The partial set MUST NOT be
        deleted: the unread pages may hold the reply that would have protected
        one of these roots. Leave the duplicates, say it did not finish."""
        fake = _FakeGitLab(page_size=2)
        for i in range(5):
            fake.add_note(f"stale {i}\n\n{MARKER}", inline=True)
        fake.fail_from_page = 2

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert sum(b.startswith("stale ") for b in fake.bodies()) == 5, (
            "a half-read listing deleted a partial set"
        )
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert result["discussions_posted"] == 1

    def test_the_summary_note_is_updated_in_place(self, settings) -> None:
        """The old code overwrote it with "_(superseded by new review)_" and
        posted a fresh one, so every re-run left a dead stub behind — the same
        pile-up in a quieter costume."""
        fake = _FakeGitLab()
        original_id = fake.add_note(f"{MARKER}\n## old summary")

        provider = _gitlab(fake)
        result = provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        top_level = [n for n in fake.notes if n["type"] is None]
        assert len(top_level) == 1
        assert top_level[0]["id"] == original_id
        assert result["summary_note_id"] == original_id
        assert "old summary" not in top_level[0]["body"]
        assert "superseded" not in top_level[0]["body"]

    def test_a_rerun_leaves_exactly_one_set(self, settings) -> None:
        fake = _FakeGitLab()
        provider = _gitlab(fake)
        batch = _batch("gitlab", "group/proj", 5, findings=3)

        provider.post_review(batch)
        after_first = [n for n in fake.notes if n["type"] == "DiffNote"]
        provider.post_review(batch)
        provider.close()

        inline = [n for n in fake.notes if n["type"] == "DiffNote"]
        top_level = [n for n in fake.notes if n["type"] is None]
        assert len(after_first) == 3
        assert len(inline) == 3, (
            f"a re-run left {len(inline)} inline notes where 3 belong"
        )
        assert len(top_level) == 1


# ─── Bitbucket ──────────────────────────────────────────────────────


class _FakeBitbucket:
    """Enough of the Bitbucket 2.0 surface to run post_review twice.

    One collection for everything, exactly as Bitbucket has it: inline
    comments, the summary and thread replies all come back from
    /pullrequests/{id}/comments and go down the same DELETE path. A reply
    carries `parent: {"id": …}` in that same listing.

    GET /user answers with the viewer's account object, and every comment
    carries a `user` with the same `uuid`/`account_id`/`nickname` fields the
    real API shares between the two payloads — a seeded comment defaults to
    the viewer, i.e. to something a previous run of ours left behind.
    """

    def __init__(self, *, page_size: int = 2) -> None:
        self.page_size = page_size
        self.viewer_user = {
            "uuid": "{uuid-bot}",
            "account_id": "acc-bot",
            "nickname": BOT,
            "display_name": "Celmis Review Bot",
        }
        self.comments: list[dict] = []
        self._ids = itertools.count(9000)
        self.list_status = 200
        self.delete_status = 204
        self.list_requests = 0
        self.user_status = 200
        self.user_body: object = dict(self.viewer_user)
        self.user_requests = 0
        #: Fail every listing page >= this one — a listing that dies MIDWAY,
        #: which is not the same failure as one that never starts.
        self.fail_from_page: int | None = None

    @staticmethod
    def _user_for(author: str) -> dict:
        return {
            "uuid": f"{{uuid-{author}}}",
            "account_id": f"acc-{author}",
            "nickname": author,
            "display_name": author,
        }

    # — seeding —
    def add_comment(
        self,
        body: str,
        *,
        author: str | None = None,
        parent: int | None = None,
        inline: bool = False,
    ) -> int:
        cid = next(self._ids)
        comment: dict = {
            "id": cid,
            "content": {"raw": body},
            "user": self._user_for(author) if author else dict(self.viewer_user),
        }
        if parent is not None:
            comment["parent"] = {"id": parent}
        if inline:
            comment["inline"] = {"path": "src/x.py", "to": 1}
        self.comments.append(comment)
        return cid

    def add_comment_raw(self, comment: dict) -> int:
        """Seed a comment exactly as given — for payloads the real API should
        never produce but a proxy or truncated cache can, and for authors
        built field by field (an impostor nickname, a renamed account)."""
        cid = comment.setdefault("id", next(self._ids))
        self.comments.append(comment)
        return cid

    def bodies(self) -> list[str]:
        return [c["content"]["raw"] for c in self.comments]

    def ids(self) -> list[int]:
        return [c["id"] for c in self.comments]

    # — transport —
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/2.0/user":
            self.user_requests += 1
            return httpx.Response(self.user_status, json=self.user_body)

        listing = re.fullmatch(
            r"/2.0/repositories/[^/]+/[^/]+/pullrequests/\d+/comments", path,
        )
        if listing and method == "GET":
            return self._page(request)
        if listing and method == "POST":
            payload = json.loads(request.content)
            cid = next(self._ids)
            comment: dict = {
                "id": cid,
                "content": {"raw": payload["content"]["raw"]},
                "user": dict(self.viewer_user),
            }
            if payload.get("inline"):
                comment["inline"] = dict(payload["inline"])
            self.comments.append(comment)
            return httpx.Response(201, json={"id": cid})

        one = re.fullmatch(
            r"/2.0/repositories/[^/]+/[^/]+/pullrequests/\d+/comments/(\d+)", path,
        )
        if one and method == "DELETE":
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, json={"message": "nope"})
            cid = int(one.group(1))
            self.comments[:] = [c for c in self.comments if c["id"] != cid]
            return httpx.Response(self.delete_status)
        if one and method == "PUT":
            cid = int(one.group(1))
            body = json.loads(request.content)["content"]["raw"]
            for comment in self.comments:
                if comment["id"] == cid:
                    comment["content"] = {"raw": body}
                    return httpx.Response(200, json=comment)
            return httpx.Response(404, json={"message": "not found"})

        return httpx.Response(404, json={"message": f"unrouted {method} {path}"})

    def _page(self, request: httpx.Request) -> httpx.Response:
        self.list_requests += 1
        if self.list_status >= 400:
            return httpx.Response(self.list_status, json={"message": "boom"})
        page = int(request.url.params.get("page", 1))
        if self.fail_from_page and page >= self.fail_from_page:
            return httpx.Response(500, json={"message": "boom"})
        start = (page - 1) * self.page_size
        chunk = self.comments[start:start + self.page_size]
        body: dict = {"values": chunk, "page": page, "pagelen": self.page_size}
        if start + self.page_size < len(self.comments):
            body["next"] = str(request.url.copy_set_param("page", page + 1))
        return httpx.Response(200, json=body)


def _bitbucket(fake: _FakeBitbucket) -> BitbucketPRProvider:
    provider = BitbucketPRProvider(token="fake")
    _patch_client(provider, httpx.MockTransport(fake))
    return provider


class TestBitbucketRerun:
    def test_a_human_quoting_one_of_our_findings_survives(self, settings) -> None:
        """Bitbucket was the template the GitHub/GitLab cleanups were written
        from — and the template itself still matched on the marker alone.
        "Quote reply" copies the quoted comment's raw markdown, marker and
        all, so a reviewer's rebuttal held our marker in their own comment
        and was deleted on the next push."""
        ours = _format_finding_body(_finding(0), MARKER)
        quoted = _quote_reply(ours, "Disagree — the caller already holds the lock.")
        assert MARKER in quoted, "the fixture stopped modelling the bug"

        fake = _FakeBitbucket()
        stale_id = fake.add_comment(ours, inline=True)
        human_id = fake.add_comment(quoted, inline=True, author=HUMAN)

        provider = _bitbucket(fake)
        provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        surviving = fake.ids()
        assert human_id in surviving, (
            "a human comment that merely QUOTES one of our findings was deleted"
        )
        assert stale_id not in surviving, "our own previous comment was not replaced"

    def test_only_the_comments_this_bot_wrote_are_removed(self, settings) -> None:
        fake = _FakeBitbucket()
        fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)
        fake.add_comment("please rename this variable", inline=True, author=HUMAN)
        fake.add_comment(
            f"complexity too high\n{OTHER_BOT_MARKER}", inline=True, author=OTHER_BOT,
        )

        provider = _bitbucket(fake)
        provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        surviving = fake.bodies()
        assert "please rename this variable" in surviving
        assert any(OTHER_BOT_MARKER in b for b in surviving)
        assert not any(b.startswith("stale finding") for b in surviving)

    def test_our_marker_under_another_account_is_not_ours(self, settings) -> None:
        """A second Celmis install, reviewing the same PR with its own token,
        writes the same default marker. Its comments are not ours to delete —
        it would delete ours right back and neither PR would ever settle."""
        fake = _FakeBitbucket()
        theirs = fake.add_comment(
            f"stale finding\n\n{MARKER}", inline=True, author="celmis-staging",
        )

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert theirs in fake.ids()
        assert result["cleanup"]["deleted"] == 0

    def test_a_nickname_is_not_an_identity(self, settings) -> None:
        """Authorship is matched on uuid/account_id and never on nickname: a
        nickname is user-editable, so anyone can wear ours. Same nickname,
        different stable ids — not ours, not deleted."""
        fake = _FakeBitbucket()
        impostor = fake.add_comment_raw({
            "content": {"raw": f"stale finding\n\n{MARKER}"},
            "user": {
                "uuid": "{uuid-impostor}",
                "account_id": "acc-impostor",
                "nickname": BOT,
            },
        })

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert impostor in fake.ids(), "a borrowed nickname was treated as us"
        assert result["cleanup"]["deleted"] == 0

    def test_a_renamed_account_still_owns_its_comments(self, settings) -> None:
        """The other direction of the same trap: our account renamed keeps its
        uuid, so the comments it wrote under the old nickname are still ours
        to replace — otherwise one rename strands every old set forever.
        Inline, so the claim under test stays authorship: a top-level comment
        of ours is the summary and is rewritten in place, not deleted."""
        fake = _FakeBitbucket()
        old_name = fake.add_comment_raw({
            "content": {"raw": f"stale finding\n\n{MARKER}"},
            "user": {"uuid": fake.viewer_user["uuid"], "nickname": "old-nickname"},
            "inline": {"path": "src/x.py", "to": 1},
        })

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert old_name not in fake.ids(), (
            "a rename orphaned our own previous comments"
        )
        assert result["cleanup"]["deleted"] == 1

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            *UNREADABLE_VIEWER,
            pytest.param(
                200, {"nickname": BOT, "display_name": "Celmis Review Bot"},
                id="body-has-only-mutable-names",
            ),
        ],
    )
    def test_an_unreadable_viewer_deletes_nothing(
        self, settings, status, body,
    ) -> None:
        """Fail closed, same as the other two — plus Bitbucket's own case: an
        answer with nothing but mutable names in it is no identity either."""
        fake = _FakeBitbucket()
        fake.user_status = status
        fake.user_body = body
        stale_inline = fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)
        stale_summary = fake.add_comment(f"{MARKER}\n## old summary")

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        surviving = fake.ids()
        assert stale_inline in surviving, "deleted without knowing who we are"
        assert stale_summary in surviving
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert result["inline_posted"] == 1, "the review itself must still post"

    def test_the_cleanup_reaches_past_the_first_page(self, settings) -> None:
        fake = _FakeBitbucket(page_size=2)
        for i in range(5):
            fake.add_comment(f"stale {i}\n\n{MARKER}", inline=True)
        fake.add_comment("a human, on the last page", inline=True, author=HUMAN)

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert not any(b.startswith("stale ") for b in fake.bodies()), (
            "comments past page one survived the cleanup"
        )
        assert "a human, on the last page" in fake.bodies()
        assert result["cleanup"]["deleted"] == 5
        assert result["cleanup"]["complete"] is True
        assert fake.user_requests == 1, (
            "GET /user once per provider, not once per page"
        )

    def test_a_listing_that_fails_still_lets_the_review_post(self, settings) -> None:
        """This listing used to swallow any >=400 with a bare `break` — the
        caller deleted whatever partial set came back and reported nothing.
        Now a failed listing means no deletes, a posted review, and a cleanup
        that says it did not finish."""
        fake = _FakeBitbucket()
        fake.list_status = 500

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=2))
        provider.close()

        assert result["inline_posted"] == 2
        assert result["cleanup"]["complete"] is False

    def test_a_half_read_listing_deletes_nothing(self, settings) -> None:
        """Page one read, page two refused — the exact shape the old `break`
        turned into a silent partial delete. The unread pages may hold the
        reply that would have protected one of these roots, so nothing goes,
        and the cleanup says it did not finish."""
        fake = _FakeBitbucket(page_size=2)
        for i in range(5):
            fake.add_comment(f"stale {i}\n\n{MARKER}", inline=True)
        fake.fail_from_page = 2

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert sum(b.startswith("stale ") for b in fake.bodies()) == 5, (
            "a half-read listing deleted a partial set"
        )
        assert result["cleanup"]["deleted"] == 0
        assert result["cleanup"]["complete"] is False
        assert result["inline_posted"] == 1, "the review itself must still post"

    def test_a_delete_that_fails_is_counted_not_raised(self, settings) -> None:
        fake = _FakeBitbucket()
        fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)
        fake.delete_status = 403

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert result["inline_posted"] == 1, "a refused delete aborted the review"
        assert result["cleanup"]["failed"] == 1
        assert result["cleanup"]["complete"] is False
        assert any(b.startswith("stale finding") for b in fake.bodies())

    def test_a_malformed_author_neither_crashes_nor_matches(self, settings) -> None:
        """{"user": "octocat"} — a truncated cache, a proxy rewrite. The
        GitHub twin of this predicate crashed the whole review on that shape;
        Bitbucket's is written with the isinstance guard from the start. A
        comment whose author cannot be read is not ours to delete, and not a
        reason to stop reviewing either."""
        fake = _FakeBitbucket()
        weird = fake.add_comment_raw({
            "content": {"raw": f"who wrote this?\n\n{MARKER}"},
            "user": "octocat",
        })
        stale = fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert result["inline_posted"] == 1, (
            "a malformed neighbour crashed the review"
        )
        assert weird in fake.ids(), "deleted a comment whose author cannot be read"
        assert stale not in fake.ids(), (
            "a malformed neighbour stopped the real cleanup"
        )
        assert result["cleanup"]["deleted"] == 1

    def test_a_human_reply_protects_its_root(self, settings) -> None:
        """Bitbucket hands the thread structure over for free — a reply
        carries `parent.id` in the same listing — so the rule costs no extra
        request: a root somebody else answered is kept, counted, and is not
        a failure."""
        fake = _FakeBitbucket()
        root = fake.add_comment(_format_finding_body(_finding(0), MARKER), inline=True)
        reply = fake.add_comment(
            "Good catch — fixing in the next push.",
            inline=True, author=HUMAN, parent=root,
        )
        lone = fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        surviving = fake.ids()
        assert root in surviving, "a root with a human reply was deleted"
        assert reply in surviving, "the human reply went down with its root"
        assert lone not in surviving, "an unanswered stale comment must still go"
        assert result["cleanup"]["kept_threaded"] == 1
        assert result["cleanup"]["complete"] is True

    def test_a_rerun_leaves_exactly_one_set(self, settings) -> None:
        fake = _FakeBitbucket()
        provider = _bitbucket(fake)
        batch = _batch("bitbucket", "ws/r", 3, findings=3)

        provider.post_review(batch)
        after_first = len(fake.comments)
        provider.post_review(batch)
        provider.close()

        assert after_first == 4  # 3 inline + 1 summary
        assert len(fake.comments) == 4, (
            f"a re-run left {len(fake.comments)} comments where 4 belong"
        )
        inline = [c for c in fake.comments if c.get("inline")]
        assert len(inline) == 3

    def test_the_summary_is_updated_in_place(self, settings) -> None:
        """Qodo's persistent-comment pattern, at last on the third provider.
        Bitbucket re-POSTed its summary every run — new id every push, so a
        link to it from a ticket or a chat message stopped resolving — while
        GitHub PATCHed and GitLab PUT theirs. Bitbucket comments support PUT
        just the same; the oldest summary of OURS is rewritten in place."""
        fake = _FakeBitbucket()
        original_id = fake.add_comment(f"{MARKER}\n## old summary")

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        top_level = [c for c in fake.comments if not c.get("inline")]
        assert len(top_level) == 1, "a second summary was posted next to ours"
        assert top_level[0]["id"] == original_id
        assert result["summary_comment_id"] == original_id
        assert "old summary" not in top_level[0]["content"]["raw"]
        assert MARKER in top_level[0]["content"]["raw"]

    def test_a_replied_to_summary_is_the_upsert_target_not_a_casualty(
        self, settings,
    ) -> None:
        """Wave-3's worry, verbatim: a summary a human replied to was kept by
        the thread protection AND a fresh summary was posted anyway — two
        summaries visible on the PR. The GitLab decision applies: a PUT
        preserves the thread, only a DELETE orphans it, so the replied-to
        summary is simply the upsert target — not counted kept_threaded, not
        duplicated."""
        fake = _FakeBitbucket()
        summary = fake.add_comment(f"{MARKER}\n## old summary")
        reply = fake.add_comment(
            "The numbers here look off.", author=HUMAN, parent=summary,
        )

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        top_level = [
            c for c in fake.comments if not c.get("inline") and not c.get("parent")
        ]
        assert len(top_level) == 1, "a second summary was posted next to the thread"
        assert top_level[0]["id"] == summary
        assert "old summary" not in top_level[0]["content"]["raw"]
        assert reply in fake.ids(), "the human reply went down with the thread"
        assert result["summary_comment_id"] == summary
        assert result["cleanup"]["kept_threaded"] == 0, (
            "the upsert target was booked as a threading casualty"
        )
        assert result["cleanup"]["complete"] is True

    def test_surplus_summaries_are_deleted_unless_somebody_answered(
        self, settings,
    ) -> None:
        """What the post-every-run era left behind: several summaries of ours
        on one PR. The oldest is the persistent one and is rewritten; a plain
        surplus one is deleted; a surplus one a human answered stays, because
        deleting it would orphan their words."""
        fake = _FakeBitbucket()
        oldest = fake.add_comment(f"{MARKER}\n## summary v1")
        surplus_plain = fake.add_comment(f"{MARKER}\n## summary v2")
        surplus_replied = fake.add_comment(f"{MARKER}\n## summary v3")
        reply = fake.add_comment(
            "Still relevant?", author=HUMAN, parent=surplus_replied,
        )

        provider = _bitbucket(fake)
        result = provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        surviving = fake.ids()
        assert oldest in surviving
        assert "summary v1" not in fake.bodies()[surviving.index(oldest)], (
            "the persistent summary still shows the previous run"
        )
        assert surplus_plain not in surviving, "a lone surplus summary must go"
        assert surplus_replied in surviving, "a replied-to surplus was deleted"
        assert reply in surviving
        assert result["summary_comment_id"] == oldest
        assert result["cleanup"]["kept_threaded"] == 1
        assert result["cleanup"]["deleted"] == 1


# ─── replace_on_synchronize = False ─────────────────────────────────


class TestReplaceOnSynchronizeOff:
    """The flag exists for the team that wants every run kept as a record.

    Every other test here pins it True, so this path — no cleanup at all, and a
    summary appended rather than upserted — shipped with nothing over it. What
    it must NOT do is quietly keep listing and deleting anyway.
    """

    def test_github_keeps_both_runs_and_appends_a_new_summary(
        self, settings_keep_history,
    ) -> None:
        fake = _FakeGitHub()
        provider = _github(fake)
        batch = _batch("github", "o/r", 1, findings=2)

        provider.post_review(batch)
        second = provider.post_review(batch)
        provider.close()

        assert len(fake.inline) == 4, "the flag is off — both runs' comments stay"
        assert len(fake.issue) == 2, "the summary is appended, not rewritten"
        assert fake.issue[0]["id"] != fake.issue[1]["id"]
        assert second["summary_comment_id"] == fake.issue[1]["id"]
        assert fake.list_requests == 0, "comments were listed with the flag off"
        assert fake.user_requests == 0, "the viewer was looked up for nothing"
        # Skipped is not incomplete: there were no duplicates to miss, so a
        # caller must not be told the cleanup came up short.
        # The review-body counters ride along in the same stats and read the
        # same way with the flag off: nothing was listed, so nothing folded.
        assert second["cleanup"] == {
            "deleted": 0, "failed": 0, "kept_threaded": 0, "complete": True,
            "minimized": 0, "minimize_failed": 0, "already_minimized": 0,
        }

    def test_github_leaves_a_previous_runs_comments_alone(
        self, settings_keep_history,
    ) -> None:
        fake = _FakeGitHub()
        ours = fake.add_inline(f"stale finding\n\n{MARKER}")
        summary = fake.add_issue(f"{MARKER}\n## old summary")

        provider = _github(fake)
        provider.post_review(_batch("github", "o/r", 1, findings=1))
        provider.close()

        assert ours in fake.ids(fake.inline)
        assert summary in fake.ids(fake.issue)
        assert "old summary" in fake.issue[0]["body"], (
            "the old summary was rewritten with the flag off"
        )

    def test_gitlab_keeps_both_runs_and_appends_a_new_summary(
        self, settings_keep_history,
    ) -> None:
        fake = _FakeGitLab()
        provider = _gitlab(fake)
        batch = _batch("gitlab", "group/proj", 5, findings=2)

        provider.post_review(batch)
        second = provider.post_review(batch)
        provider.close()

        inline = [n for n in fake.notes if n["type"] == "DiffNote"]
        top_level = [n for n in fake.notes if n["type"] is None]
        assert len(inline) == 4, "the flag is off — both runs' notes stay"
        assert len(top_level) == 2, "the summary is appended, not rewritten"
        assert second["summary_note_id"] == top_level[1]["id"]
        assert fake.list_requests == 0, "notes were listed with the flag off"
        assert fake.user_requests == 0, "the viewer was looked up for nothing"
        assert second["cleanup"] == {
            "deleted": 0, "failed": 0, "kept_threaded": 0, "complete": True,
        }

    def test_gitlab_leaves_a_previous_runs_notes_alone(
        self, settings_keep_history,
    ) -> None:
        fake = _FakeGitLab()
        ours = fake.add_note(f"stale finding\n\n{MARKER}", inline=True)
        summary = fake.add_note(f"{MARKER}\n## old summary")

        provider = _gitlab(fake)
        provider.post_review(_batch("gitlab", "group/proj", 5, findings=1))
        provider.close()

        assert ours in fake.ids()
        kept = [n for n in fake.notes if n["id"] == summary]
        assert kept and "old summary" in kept[0]["body"], (
            "the old summary was rewritten with the flag off"
        )

    def test_bitbucket_keeps_both_runs(self, settings_keep_history) -> None:
        fake = _FakeBitbucket()
        provider = _bitbucket(fake)
        batch = _batch("bitbucket", "ws/r", 3, findings=2)

        provider.post_review(batch)
        second = provider.post_review(batch)
        provider.close()

        assert len(fake.comments) == 6, "the flag is off — both runs' comments stay"
        assert fake.list_requests == 0, "comments were listed with the flag off"
        assert fake.user_requests == 0, "the viewer was looked up for nothing"
        # Skipped is not incomplete: there were no duplicates to miss, so a
        # caller must not be told the cleanup came up short.
        assert second["cleanup"] == {
            "deleted": 0, "failed": 0, "kept_threaded": 0, "complete": True,
        }

    def test_bitbucket_leaves_a_previous_runs_comments_alone(
        self, settings_keep_history,
    ) -> None:
        fake = _FakeBitbucket()
        ours = fake.add_comment(f"stale finding\n\n{MARKER}", inline=True)
        summary = fake.add_comment(f"{MARKER}\n## old summary")

        provider = _bitbucket(fake)
        provider.post_review(_batch("bitbucket", "ws/r", 3, findings=1))
        provider.close()

        assert ours in fake.ids()
        assert summary in fake.ids()


# ─── The provider-agnostic lookup ───────────────────────────────────


class TestMarkedIdLookup:
    """`find_marked_comment_ids` is the provider-agnostic contract: a caller
    can ask any provider what it has left on a PR without knowing which one it
    is holding — and every provider answers with OUR comments only, because
    the marker alone identifies a shape, not an author."""

    def test_github_reports_both_collections(self, settings) -> None:
        fake = _FakeGitHub()
        inline_id = fake.add_inline(f"finding\n\n{MARKER}")
        fake.add_inline("a human", author=HUMAN)
        summary_id = fake.add_issue(f"{MARKER}\nsummary")

        provider = _github(fake)
        found = provider.find_marked_comment_ids("o/r", 1, MARKER)
        provider.close()

        assert found == [inline_id, summary_id]

    def test_gitlab_reports_both_kinds(self, settings) -> None:
        fake = _FakeGitLab()
        inline_id = fake.add_note(f"finding\n\n{MARKER}", inline=True)
        fake.add_note("a human", inline=True, author=HUMAN)
        summary_id = fake.add_note(f"{MARKER}\nsummary")

        provider = _gitlab(fake)
        found = provider.find_marked_comment_ids("group/proj", 5, MARKER)
        provider.close()

        assert found == [inline_id, summary_id]

    def test_bitbucket_reports_only_ours(self, settings) -> None:
        fake = _FakeBitbucket()
        inline_id = fake.add_comment(f"finding\n\n{MARKER}", inline=True)
        fake.add_comment(f"> {MARKER}\n\nquoted rebuttal", author=HUMAN)
        summary_id = fake.add_comment(f"{MARKER}\nsummary")

        provider = _bitbucket(fake)
        found = provider.find_marked_comment_ids("ws/r", 3, MARKER)
        provider.close()

        assert found == [inline_id, summary_id]

    def test_gitlab_never_offers_an_inline_note_as_the_summary(self, settings) -> None:
        """The id this returns gets a summary PUT into it. An inline note
        anchored to line 42 is not a summary slot."""
        fake = _FakeGitLab()
        fake.add_note(f"finding\n\n{MARKER}", inline=True)
        summary_id = fake.add_note(f"{MARKER}\nsummary")

        provider = _gitlab(fake)
        existing = provider.find_existing_review_comment("group/proj", 5, MARKER)
        provider.close()

        assert existing == summary_id

    def test_bitbucket_never_offers_an_inline_comment_as_the_summary(
        self, settings,
    ) -> None:
        """Same rule, third provider — and here it is load-bearing for the
        upsert: the oldest comment of ours used to be the answer whatever its
        kind, and now that the answer gets a summary PUT into it, an inline
        finding on line 42 must never be it."""
        fake = _FakeBitbucket()
        fake.add_comment(f"finding\n\n{MARKER}", inline=True)
        summary_id = fake.add_comment(f"{MARKER}\nsummary")

        provider = _bitbucket(fake)
        existing = provider.find_existing_review_comment("ws/r", 3, MARKER)
        provider.close()

        assert existing == summary_id
