"""One unplaceable anchor destroyed a whole review, and the damage did not stop
at that review.

MEASURED, on the 14-PR Martian subset (runs under celmis-bench-work/runs/runG2):

  * `per_pr.json` — `github:celmis-bench/discourse-graphite#18`: findings 4,
    posted 0, status "complete" — the only PR of the 14 whose findings never
    reached the pull request. GitHub validates a review as ONE object:
    `post_review` builds one `comments` array and sends one POST, so a single
    comment on a line outside every hunk 422s the batch and takes the other
    three findings with it. The refused anchor is recorded as
    `app/controllers/admin/groups_controller.rb` line 117, on a file 104 lines
    long. One refused batch, four findings lost, out of 146 findings across
    the two runs (69 in runG2, 77 in BASE).

  * The second-order damage is worse. `_delete_stale_comments` runs only AFTER
    a successful post, so the failed review left the PREVIOUS run's comments
    standing and the scraper collected them. Of runG2's 69 collected celmis
    comments, exactly 4 carry no `*Why:*` line — every one of them on
    discourse-graphite#8, the golden PR behind #18 — because they predate the
    reasoning field entirely. The judge scored a review that run never posted.

So the anchors are made postable BEFORE the POST rather than recovered from
afterwards, and the recovery that remains was rebuilt as a bounded loop: there
were two sequential one-shot `if`s, and whichever 422 GitHub reported first was
the only one that could ever fire.

The second defect here is the review BODIES. A submitted review is immutable
and REST has no delete for it, so every re-run left one more entry on the
timeline — the one duplication no cleanup could reach. `PullRequestReview` is a
`Minimizable` type, so the previous runs' bodies are folded with
`minimizeComment(classifier: OUTDATED)` once the new review is up.

The fake below is the smallest server that can tell all of this apart: it
REJECTS the anchors GitHub would reject (so "the review posted" is a fact about
what it accepted, not a count of requests), it reports one 422 reason at a time
in a configurable order, it serves the REST reviews listing with authors and
bodies, and it implements enough of GraphQL to answer `isMinimized` and to
record what a mutation folded.
"""

from __future__ import annotations

import itertools
import json
import re

import httpx
import pytest

from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    HunkSide,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)
from src.review.providers import github as github_module
from src.review.providers.github import GitHubPRProvider
from src.review.settings import ReviewSettings

# Deliberately not the shipped default: anything matching a hardcoded marker
# instead of `settings.comment_marker` finds nothing here.
MARKER = "<!-- celmis:review:anchors-under-test -->"

BOT = "celmis-review-bot"
HUMAN = "dana"


@pytest.fixture
def settings(monkeypatch) -> ReviewSettings:
    cfg = ReviewSettings(
        comment_marker=MARKER, replace_on_synchronize=True, max_inline_comments=20,
    )
    monkeypatch.setattr(github_module, "get_review_settings", lambda: cfg)
    return cfg


def _hunk(path: str, new_start: int, new_count: int, **kw) -> Hunk:
    return Hunk(
        file_path=path,
        old_file_path=kw.pop("old_file_path", path),
        old_start=kw.pop("old_start", new_start),
        old_count=kw.pop("old_count", new_count),
        new_start=new_start,
        new_count=new_count,
        content=f"@@ -{new_start},{new_count} +{new_start},{new_count} @@\n",
        **kw,
    )


def _pr(hunks: list[Hunk] | None = None, url: str = "") -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="u", base_ref="main", base_sha="b1", head_ref="feat",
        head_sha="h1", state="open", url=url, hunks=hunks or [],
    )


def _finding(path: str, line: int, name: str = "f", side: HunkSide = HunkSide.RIGHT) -> Finding:
    return Finding(
        file_path=path, line=line, side=side,
        severity=FindingSeverity.WARNING, title=name, body="because",
        agent="architect", rule_id=f"arch.{name}",
    )


def _batch(pr: PullRequest, findings: list[Finding], verdict=ReviewVerdict.COMMENT) -> ReviewBatch:
    return ReviewBatch(pull_request=pr, verdict=verdict, findings=findings)


class _FakeGitHub:
    """Enough of GitHub to run post_review against real anchor validation.

    `covered` is the set of (path, side, line) the server will place a comment
    on — the same question the real API asks of the diff. Anything else is the
    422 that used to cost the batch.

    `report_order` is which 422 reason the server volunteers first when more
    than one applies; the real API reports one at a time and does not promise
    which.
    """

    def __init__(self, *, viewer: str = BOT) -> None:
        self.viewer = viewer
        self.inline: list[dict] = []
        self.issue: list[dict] = []
        self.reviews: list[dict] = []          # payloads this run got ACCEPTED
        self.review_objects: list[dict] = []   # what GET /pulls/N/reviews returns
        self._ids = itertools.count(1000)
        #: (path, side, line) the server accepts an anchor on. None = accept all.
        self.covered: set[tuple[str, str, int]] | None = None
        #: Refuse APPROVE/REQUEST_CHANGES the way GitHub does on an own PR.
        self.own_pr_blocked = False
        self.report_order: tuple[str, ...] = ("own_pr", "anchors")
        self.rejections: list[str] = []
        #: node_id -> isMinimized, the state GraphQL reports and mutates.
        self.minimized: dict[str, bool] = {}
        #: node ids the isMinimized query answers `null` for — GraphQL's shape
        #: for "no node with that id", which a deleted or dismissed review gives.
        self.probe_blind: set[str] = set()
        #: node ids whose mutation alias comes back null: GraphQL's partial
        #: failure, HTTP 200 with the field empty and the reason in `errors`.
        self.mutation_fails: set[str] = set()
        self.graphql_queries: list[dict] = []
        self.graphql_status = 200
        self.graphql_body: object | None = None
        self.reviews_list_status = 200

    # — seeding —
    def add_review(
        self, body: str, *, author: str | None = None, node_id: str | None = None,
        minimized: bool = False,
    ) -> str:
        rid = next(self._ids)
        node = node_id or f"PRR_{rid}"
        self.review_objects.append({
            "id": rid, "node_id": node, "body": body,
            "user": {"login": author or self.viewer}, "state": "COMMENTED",
        })
        self.minimized[node] = minimized
        return node

    # — transport —
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == "/user":
            return httpx.Response(200, json={"login": self.viewer})
        if method == "POST" and path == "/graphql":
            return self._graphql(request)
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews", path):
            if self.reviews_list_status >= 400:
                return httpx.Response(self.reviews_list_status, json={"message": "boom"})
            return httpx.Response(200, json=self.review_objects)
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/comments", path):
            return httpx.Response(200, json=self.inline)
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            return httpx.Response(200, json=self.issue)

        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews", path):
            return self._submit(json.loads(request.content))
        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
            cid = next(self._ids)
            self.issue.append({
                "id": cid, "body": json.loads(request.content)["body"],
                "user": {"login": self.viewer},
            })
            return httpx.Response(201, json={"id": cid})

        issue_one = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/comments/(\d+)", path)
        if issue_one and method == "PATCH":
            cid = int(issue_one.group(1))
            for c in self.issue:
                if c["id"] == cid:
                    c["body"] = json.loads(request.content)["body"]
                    return httpx.Response(200, json=c)
            return httpx.Response(404, json={"message": "not found"})
        if issue_one and method == "DELETE":
            self.issue[:] = [c for c in self.issue if c["id"] != int(issue_one.group(1))]
            return httpx.Response(204)
        inline_one = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/comments/(\d+)", path)
        if inline_one and method == "DELETE":
            self.inline[:] = [c for c in self.inline if c["id"] != int(inline_one.group(1))]
            return httpx.Response(204)

        return httpx.Response(404, json={"message": f"unrouted {method} {path}"})

    def _submit(self, payload: dict) -> httpx.Response:
        for reason in self.report_order:
            if reason == "own_pr" and self.own_pr_blocked and payload.get("event") in (
                "APPROVE", "REQUEST_CHANGES",
            ):
                self.rejections.append("own_pr")
                return httpx.Response(422, json={
                    "message": "Can not approve your own pull request",
                })
            if reason == "anchors" and self.covered is not None:
                bad = [
                    c for c in payload.get("comments") or []
                    if (c["path"], c["side"], c["line"]) not in self.covered
                ]
                if bad:
                    self.rejections.append("anchors")
                    return httpx.Response(422, json={
                        "message": "pull_request_review_thread.line must be part "
                                   "of the diff",
                        "refused": [f'{c["path"]}:{c["line"]}' for c in bad],
                    })
        rid = next(self._ids)
        for c in payload.get("comments") or []:
            self.inline.append({
                "id": next(self._ids), "body": c["body"], "path": c["path"],
                "line": c["line"], "side": c["side"], "user": {"login": self.viewer},
            })
        self.reviews.append(payload)
        node = f"PRR_{rid}"
        self.review_objects.append({
            "id": rid, "node_id": node, "body": payload.get("body", ""),
            "user": {"login": self.viewer}, "state": payload.get("event"),
        })
        self.minimized[node] = False
        return httpx.Response(200, json={"id": rid, "html_url": "https://gh/pr/1"})

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        doc = json.loads(request.content)
        self.graphql_queries.append(doc)
        if self.graphql_status >= 400:
            return httpx.Response(self.graphql_status, json={"message": "boom"})
        if self.graphql_body is not None:
            return httpx.Response(200, json=self.graphql_body)
        query = doc.get("query", "")
        variables = doc.get("variables") or {}
        if query.lstrip().startswith("query"):
            return httpx.Response(200, json={"data": {"nodes": [
                {"id": nid, "isMinimized": self.minimized[nid]}
                if nid in self.minimized and nid not in self.probe_blind else None
                for nid in variables.get("ids") or []
            ]}})
        data, errors = {}, []
        for alias, payload in variables.items():
            assert payload["classifier"] == "OUTDATED", payload
            subject = payload["subjectId"]
            if subject in self.mutation_fails:
                data[f"m{alias[1:]}"] = None
                errors.append({"message": f"cannot minimize {subject}"})
                continue
            self.minimized[subject] = True
            data[f"m{alias[1:]}"] = {"minimizedComment": {"isMinimized": True}}
        body: dict = {"data": data}
        if errors:
            body["errors"] = errors
        return httpx.Response(200, json=body)

    @property
    def anchors(self) -> list[tuple[str, int]]:
        """(path, line) of every inline comment the server actually placed."""
        return [(c["path"], c["line"]) for c in self.inline]


def _provider(fake: _FakeGitHub) -> GitHubPRProvider:
    provider = GitHubPRProvider(token="fake")
    provider._http.close()
    provider._http = httpx.Client(
        transport=httpx.MockTransport(fake), timeout=10.0,
    )
    return provider


# ─── (a) every anchor is postable before the POST ────────────────────


class TestAnchorsAreSnappedNotRefused:
    def test_the_measured_incident_now_posts_all_four_findings(self, settings) -> None:
        """discourse-graphite#18: findings 4, posted 0. The anchor was
        `app/controllers/admin/groups_controller.rb` line 117 on a 104-line
        file, and it took the other three findings with it."""
        path = "app/controllers/admin/groups_controller.rb"
        pr = _pr([_hunk(path, 20, 21), _hunk(path, 84, 21)])   # 20-40, 84-104
        fake = _FakeGitHub()
        fake.covered = {
            (path, "RIGHT", n)
            for n in list(range(20, 41)) + list(range(84, 105))
        }
        provider = _provider(fake)
        result = provider.post_review(_batch(pr, [
            _finding(path, 117, "outside"),
            _finding(path, 25, "a"),
            _finding(path, 90, "b"),
            _finding(path, 100, "c"),
        ]))
        provider.close()

        assert fake.rejections == [], "an anchor was still refused"
        assert len(fake.inline) == 4, (
            f"{len(fake.inline)} of 4 findings survived — the batch is all-or-nothing"
        )
        assert (path, 104) in fake.anchors, "line 117 did not snap to the last covered line"
        assert result["anchors_snapped"] == 1
        assert result["comments_posted"] == 4

    def test_an_anchor_already_inside_a_hunk_is_left_alone(self, settings) -> None:
        """Snapping must be invisible to the 145 findings of 146 that were
        already placeable — a comment that moves is a comment that looks new."""
        pr = _pr([_hunk("src/a.py", 10, 5)])                   # 10-14
        fake = _FakeGitHub()
        fake.covered = {("src/a.py", "RIGHT", n) for n in range(10, 15)}
        provider = _provider(fake)
        result = provider.post_review(_batch(pr, [
            _finding("src/a.py", 10, "first"),
            _finding("src/a.py", 12, "middle"),
            _finding("src/a.py", 14, "last"),
        ]))
        provider.close()

        assert fake.anchors == [("src/a.py", 10), ("src/a.py", 12), ("src/a.py", 14)]
        assert result["anchors_snapped"] == 0

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (1, 10),      # before the first hunk -> its first line
            (9, 10),      # one short of it
            (16, 14),     # between two hunks, nearer the earlier one
            (19, 20),     # between two hunks, nearer the later one
            (17, 14),     # equidistant (3 either way) -> the lower line
            (99, 24),     # past the last hunk -> its last line
        ],
    )
    def test_it_snaps_to_the_nearest_covered_line(self, settings, line, expected) -> None:
        """Two hunks, 10-14 and 20-24. Nearest wins; a tie goes to the lower
        line so a re-run puts the same finding in the same place."""
        pr = _pr([_hunk("src/a.py", 10, 5), _hunk("src/a.py", 20, 5)])
        fake = _FakeGitHub()
        fake.covered = {
            ("src/a.py", "RIGHT", n) for n in list(range(10, 15)) + list(range(20, 25))
        }
        provider = _provider(fake)
        provider.post_review(_batch(pr, [_finding("src/a.py", line, "x")]))
        provider.close()

        assert fake.anchors == [("src/a.py", expected)]

    def test_a_finding_is_snapped_never_dropped(self, settings) -> None:
        """The recall argument. A finding outside the diff is the class
        architect exists to produce — the untouched caller the change breaks —
        so it must arrive somewhere, never be filtered for being awkward."""
        pr = _pr([_hunk("src/a.py", 10, 5)])
        fake = _FakeGitHub()
        fake.covered = {("src/a.py", "RIGHT", n) for n in range(10, 15)}
        provider = _provider(fake)
        result = provider.post_review(_batch(pr, [
            _finding("src/a.py", 900, "the-far-caller"),
        ]))
        provider.close()

        assert result["comments_posted"] == 1
        assert len(fake.inline) == 1
        assert "the-far-caller" in fake.inline[0]["body"]

    def test_a_file_with_no_hunks_at_all_does_not_crash(self, settings) -> None:
        """A finding on a file the PR does not touch has nowhere to snap TO.
        It must not raise and it must not vanish: the 422 fold carries it into
        the persistent summary, where it is still readable."""
        pr = _pr([_hunk("src/a.py", 10, 5)])
        fake = _FakeGitHub()
        fake.covered = {("src/a.py", "RIGHT", n) for n in range(10, 15)}
        provider = _provider(fake)
        result = provider.post_review(_batch(pr, [
            _finding("src/untouched.py", 42, "elsewhere"),
        ]))
        provider.close()

        assert result["review_id"], "the review did not post"
        summary = "\n".join(c["body"] for c in fake.issue)
        assert "src/untouched.py" in summary and "elsewhere" in summary

    def test_a_pr_with_no_hunks_at_all_does_not_crash(self, settings) -> None:
        """The empty-diff case: `_anchorable_ranges` over zero hunks."""
        fake = _FakeGitHub()
        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), [_finding("src/a.py", 3, "x")]))
        provider.close()
        assert result["anchors_snapped"] == 0

    def test_a_pure_deletion_hunk_offers_no_new_side_line(self, settings) -> None:
        """`@@ -30,4 +29,0 @@` — nothing was added, so there is no RIGHT line
        to anchor on and new_count 0 must not be read as covering line 29."""
        pr = _pr([
            _hunk("src/a.py", 29, 0, old_start=30, old_count=4),
            _hunk("src/a.py", 60, 3),
        ])
        fake = _FakeGitHub()
        fake.covered = {("src/a.py", "RIGHT", n) for n in range(60, 63)}
        provider = _provider(fake)
        provider.post_review(_batch(pr, [_finding("src/a.py", 29, "x")]))
        provider.close()

        assert fake.rejections == [], "it anchored on a line the deletion hunk does not have"
        assert fake.anchors == [("src/a.py", 60)]

    def test_a_left_side_finding_snaps_against_the_old_file(self, settings) -> None:
        """A deleted file is reviewed on side=LEFT, where the postable span is
        old_start..old_start+old_count-1 — a different range from the new
        side's, and snapping to the new side's would refuse the batch."""
        pr = _pr([_hunk("src/gone.py", 1, 0, old_start=200, old_count=10)])
        fake = _FakeGitHub()
        fake.covered = {("src/gone.py", "LEFT", n) for n in range(200, 210)}
        provider = _provider(fake)
        provider.post_review(_batch(pr, [
            _finding("src/gone.py", 400, "x", side=HunkSide.LEFT),
        ]))
        provider.close()

        assert fake.rejections == []
        assert fake.anchors == [("src/gone.py", 209)]


# ─── (b) the 422 recovery fires in either order ──────────────────────


class TestBoth422ArmsFire:
    @pytest.mark.parametrize("order", [("own_pr", "anchors"), ("anchors", "own_pr")])
    def test_a_pr_that_is_both_ours_and_unanchorable_still_posts(
        self, settings, order,
    ) -> None:
        """Two sequential one-shot `if`s meant whichever 422 GitHub reported
        FIRST was the only one that could fire: the second looked at the
        already-retried response and saw a status that no longer matched. Both
        must fire, in whatever order the API volunteers them."""
        fake = _FakeGitHub()
        fake.own_pr_blocked = True
        fake.covered = set()          # refuse every anchor
        fake.report_order = order
        provider = _provider(fake)

        result = provider.post_review(
            _batch(_pr([]), [_finding("src/a.py", 5, "x")],
                   verdict=ReviewVerdict.REQUEST_CHANGES),
        )
        provider.close()

        assert set(fake.rejections) == {"own_pr", "anchors"}, (
            f"only {fake.rejections} fired — the other arm never ran"
        )
        assert result["review_id"], "the review was lost"
        assert fake.reviews[-1]["event"] == "COMMENT"
        assert fake.reviews[-1]["comments"] == []
        summary = "\n".join(c["body"] for c in fake.issue)
        assert "src/a.py" in summary, "the refused finding was dropped, not folded"

    def test_the_loop_is_bounded_when_nothing_can_fix_the_422(self, settings) -> None:
        """A 422 neither arm addresses must raise once, not spin."""
        class _AlwaysRefuse(_FakeGitHub):
            def _submit(self, payload: dict) -> httpx.Response:
                self.rejections.append("mystery")
                return httpx.Response(422, json={"message": "something else entirely"})

        fake = _AlwaysRefuse()
        provider = _provider(fake)
        with pytest.raises(github_module.PullRequestProviderError):
            provider.post_review(_batch(_pr([]), [_finding("src/a.py", 5, "x")]))
        provider.close()

        # One initial POST plus the single anchor-drop retry, and then it stops.
        assert len(fake.rejections) == 2, fake.rejections


# ─── defect 2: the review bodies fold away ───────────────────────────


class TestPreviousReviewBodiesAreFolded:
    def test_our_previous_review_is_minimized_and_a_human_review_is_not(
        self, settings,
    ) -> None:
        """The same two-part proof the comment cleanup uses. The token here is
        a PERSON's, so their own reviews are on this listing; authorship alone
        would fold the human's words away."""
        fake = _FakeGitHub()
        ours = fake.add_review(f"{MARKER}\n**COMMENT** — see the summary")
        theirs = fake.add_review("Looks good to me, ship it", author=HUMAN)
        unmarked = fake.add_review("**COMMENT** — see the summary")  # ours, no marker

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert fake.minimized[ours] is True
        assert fake.minimized[theirs] is False, "a human's review was folded away"
        assert fake.minimized[unmarked] is False, "the marker was not required"
        assert result["cleanup"]["minimized"] == 1
        assert result["cleanup"]["minimize_failed"] == 0

    def test_the_review_just_posted_is_never_folded(self, settings) -> None:
        """Listed BEFORE the POST, folded after — the same ordering the deletes
        use, and for the same reason: a review that fails to post must not
        leave the pull request with nothing to read."""
        fake = _FakeGitHub()
        provider = _provider(fake)
        provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert list(fake.minimized.values()) == [False], (
            "the run folded away its own review"
        )

    def test_a_rerun_does_not_refold_what_is_already_folded(self, settings) -> None:
        """REST's review object has no minimized field — the state exists only
        on the GraphQL type — so the check is a GraphQL query, and it has to
        happen or every push mutates every past review again."""
        fake = _FakeGitHub()
        old = fake.add_review(f"{MARKER}\nbody", minimized=True)

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert result["cleanup"]["already_minimized"] == 1
        assert result["cleanup"]["minimized"] == 0
        mutations = [
            q for q in fake.graphql_queries
            if q["query"].lstrip().startswith("mutation")
        ]
        assert mutations == [], "an already-folded review was mutated again"
        assert fake.minimized[old] is True

    def test_every_stale_review_folds_in_one_round_trip(self, settings) -> None:
        """`minimizeComment` takes one subject, so N aliases in one document
        rather than N requests — the timeline of a long-lived PR is exactly
        where this would otherwise cost a request per push per review."""
        fake = _FakeGitHub()
        nodes = [fake.add_review(f"{MARKER}\nrun {i}") for i in range(5)]

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert all(fake.minimized[n] for n in nodes)
        assert result["cleanup"]["minimized"] == 5
        assert len(fake.graphql_queries) == 2, (
            "one isMinimized query and one mutation document, nothing per review"
        )

    def test_the_review_body_carries_the_marker_that_proves_it_is_ours(
        self, settings,
    ) -> None:
        """Without it the next run has only the author to go on. Driven through
        the real payload rather than read off the formatter."""
        fake = _FakeGitHub()
        provider = _provider(fake)
        provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert MARKER in fake.reviews[0]["body"]
        # Still a pointer, not a second copy of the summary.
        assert "## 🤖 Code Review" not in fake.reviews[0]["body"]

    def test_a_graphql_failure_does_not_fail_the_post(self, settings) -> None:
        """A review with a long timeline beats no review."""
        fake = _FakeGitHub()
        fake.add_review(f"{MARKER}\nbody")
        fake.graphql_status = 502

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), [_finding("src/a.py", 1, "x")]))
        provider.close()

        assert result["review_id"], "a GraphQL 502 took the review down"
        assert result["cleanup"]["minimize_failed"] == 1
        assert result["cleanup"]["minimized"] == 0

    def test_a_graphql_error_payload_does_not_fail_the_post(self, settings) -> None:
        """GraphQL answers a partly-failed request with HTTP 200 and `errors`;
        a mutation that reports nothing folded is a failure, not a success."""
        fake = _FakeGitHub()
        fake.add_review(f"{MARKER}\nbody")
        fake.graphql_body = {"data": None, "errors": [{"message": "nope"}]}

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert result["review_id"]
        assert result["cleanup"]["minimize_failed"] == 1

    def test_a_mutation_that_folded_nothing_is_not_counted_as_folded(
        self, settings,
    ) -> None:
        """GraphQL answers a partly-failed mutation with HTTP 200, the field
        null and the reason in `errors`. Counting the request rather than the
        reply is how a stat comes to say "5 folded" over an untouched
        timeline."""
        fake = _FakeGitHub()
        kept = fake.add_review(f"{MARKER}\nrun 1")
        folded = fake.add_review(f"{MARKER}\nrun 2")
        fake.mutation_fails = {kept}

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert fake.minimized[kept] is False
        assert fake.minimized[folded] is True
        assert result["cleanup"] == {
            **result["cleanup"], "minimized": 1, "minimize_failed": 1,
        }

    def test_a_node_the_probe_cannot_see_is_never_mutated(self, settings) -> None:
        """`nodes(ids:)` answers null for an id it cannot resolve — a review
        deleted between the REST listing and the query. Guessing is how a
        mutation reaches something it was never meant to touch, so an id whose
        state is unknown is counted failed and left alone."""
        fake = _FakeGitHub()
        ghost = fake.add_review(f"{MARKER}\ngone")
        fake.probe_blind = {ghost}

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        mutated = [
            q for q in fake.graphql_queries
            if q["query"].lstrip().startswith("mutation")
        ]
        assert mutated == [], "an id the probe could not resolve was mutated"
        assert result["cleanup"]["minimize_failed"] == 1
        assert result["cleanup"]["minimized"] == 0

    def test_a_graphql_connection_error_does_not_fail_the_post(self, settings) -> None:
        """The other way the endpoint goes wrong: no HTTP answer at all. A
        transport error must be caught where the status check cannot see it."""
        fake = _FakeGitHub()
        fake.add_review(f"{MARKER}\nbody")
        base = fake.__call__

        def refuse(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/graphql":
                raise httpx.ConnectError("no route to host", request=request)
            return base(request)

        provider = GitHubPRProvider(token="fake")
        provider._http.close()
        provider._http = httpx.Client(transport=httpx.MockTransport(refuse), timeout=5.0)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert result["review_id"], "a dead GraphQL endpoint took the review down"
        assert result["cleanup"]["minimize_failed"] == 1

    def test_a_graphql_body_that_is_not_an_object_folds_nothing(self, settings) -> None:
        """A captive proxy's page, or a list from a mis-routed request. An
        answer we cannot read is not permission to mutate anything."""
        fake = _FakeGitHub()
        old_node = fake.add_review(f"{MARKER}\nbody")
        fake.graphql_body = ["not", "an", "object"]

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert result["review_id"]
        assert fake.minimized[old_node] is False
        assert result["cleanup"]["minimize_failed"] == 1

    def test_an_unreadable_reviews_listing_folds_nothing_and_still_posts(
        self, settings,
    ) -> None:
        fake = _FakeGitHub()
        fake.add_review(f"{MARKER}\nbody")
        fake.reviews_list_status = 500

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert result["review_id"]
        assert result["cleanup"]["minimized"] == 0
        assert result["cleanup"]["minimize_failed"] == 0

    def test_the_flag_off_folds_nothing(self, settings, monkeypatch) -> None:
        """`replace_on_synchronize=False` means every run's output is kept as a
        record — folding the bodies away is the same decision as deleting the
        comments and must obey the same switch."""
        cfg = ReviewSettings(comment_marker=MARKER, replace_on_synchronize=False)
        monkeypatch.setattr(github_module, "get_review_settings", lambda: cfg)
        fake = _FakeGitHub()
        old = fake.add_review(f"{MARKER}\nbody")

        provider = _provider(fake)
        result = provider.post_review(_batch(_pr([]), []))
        provider.close()

        assert fake.minimized[old] is False
        assert fake.graphql_queries == []
        assert result["cleanup"]["minimized"] == 0
