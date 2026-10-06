"""Bitbucket answers `/pullrequests/{id}/diff` with a 302 and an empty body.

The guarded HTTP client never follows a redirect by itself, so the provider
read an empty diff, the orchestrator called it "the pull request has no diff
content" and skipped a PR that had eight files in it (PR 4821). Pinned here:

  * the redirect is followed, hop by hop, to this API only, at most 3 times;
  * an empty diff for a PR whose diffstat lists files is an ERROR (after
    retries), on the provider and in the orchestrator, and the pull request is
    told — never a quiet skip; a PR with no files stays a quiet skip;
  * every diff GET goes through the one place that follows redirects, and
    GitHub / GitLab treat a 3xx as an error instead of an empty answer;
  * a git-quoted (Cyrillic) path is decoded, so hunks and anchors agree;
  * a description edit on a commit that is already reviewed starts nothing.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.db.models import ReviewPullRequest
from src.review.diff import parse_unified_diff
from src.review.models import PullRequest
from src.review.providers import bitbucket as bb_mod
from src.review.providers.base import EmptyDiffError, PullRequestProviderError
from src.review.providers.bitbucket import BitbucketPRProvider
from src.review.providers.github import GitHubPRProvider
from src.review.providers.gitlab import GitLabPRProvider
from src.review.stages import StageRecorder
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _keys,
    _orch,
    _Provider,
    _stage,
    bound,
    no_ledger,
    queue,
    store,
)
from tests.review.test_providers import SAMPLE_DIFF, _patch_client
from tests.review.test_the_pr_hears_the_review_begin_and_end import (  # noqa: F401
    _pr,
    env,
)

API = "https://api.bitbucket.org/2.0"
REPO = f"{API}/repositories/ws/r"

META = {
    "title": "Add feature", "description": "Body", "author": {"nickname": "carol"},
    "source": {"branch": {"name": "feat/x"}, "commit": {"hash": "aaa111bbb222"}},
    "destination": {"branch": {"name": "main"}, "commit": {"hash": "ccc333ddd444"}},
    "state": "OPEN",
    "links": {"html": {"href": "https://bitbucket.org/ws/r/pull-requests/4821"}},
}


class _Bitbucket:
    """A redirect-aware fake of the Bitbucket API: records every request and
    answers from a table of {path-suffix: callable(request) -> Response}."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, object] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                return answer(req) if callable(answer) else answer
        return httpx.Response(404, text="not routed: " + url)

    def paths(self) -> list[str]:
        return [str(r.url).removeprefix(API) for r in self.requests]


def _provider(fake: _Bitbucket) -> BitbucketPRProvider:
    p = BitbucketPRProvider(token="fake")
    _patch_client(p, httpx.MockTransport(fake))
    return p


def _redirecting() -> _Bitbucket:
    fake = _Bitbucket()
    fake.routes = {
        "/pullrequests/4821": httpx.Response(200, json=META),
        "/pullrequests/4821/diff": httpx.Response(
            302, headers={"Location": f"{REPO}/diff/aaa111bbb222..ccc333ddd444?topic=true"},
        ),
        "/diff/aaa111bbb222..ccc333ddd444?topic=true": httpx.Response(200, text=SAMPLE_DIFF),
    }
    return fake


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(bb_mod.time, "sleep", waits.append)
    return waits


# ─── the redirect ─────────────────────────────────────────────────────


def test_a_bitbucket_diff_that_redirects_is_followed() -> None:
    fake = _redirecting()
    pr = _provider(fake).fetch_pull_request("ws/r", 4821)

    assert [h.file_path for h in pr.hunks] == ["src/foo.py"]
    assert pr.raw_diff == SAMPLE_DIFF
    assert pr.reported_files is None, "a diff that arrived needs no diffstat"
    assert fake.paths() == [
        "/repositories/ws/r/pullrequests/4821",
        "/repositories/ws/r/pullrequests/4821/diff",
        "/repositories/ws/r/diff/aaa111bbb222..ccc333ddd444?topic=true",
    ]


def test_the_diff_is_asked_for_as_text_and_read_as_utf8() -> None:
    fake = _redirecting()
    fake.routes["/diff/aaa111bbb222..ccc333ddd444?topic=true"] = httpx.Response(
        200, content="+привіт\n".encode(), headers={"Content-Type": "text/plain"},
    )
    p = _provider(fake)
    assert p._get_diff("ws", "r", 4821) == "+привіт\n"
    assert fake.requests[0].headers["accept"].startswith("text/plain")


def test_a_redirect_to_another_host_is_refused_and_no_token_goes_there() -> None:
    fake = _redirecting()
    fake.routes["/pullrequests/4821/diff"] = httpx.Response(
        302, headers={"Location": "https://evil.example/diff"},
    )
    with pytest.raises(PullRequestProviderError, match="another host"):
        _provider(fake).fetch_pull_request("ws/r", 4821)
    assert all(r.url.host == "api.bitbucket.org" for r in fake.requests)


def test_a_redirect_loop_stops_after_three_hops() -> None:
    fake = _redirecting()
    loop = httpx.Response(302, headers={"Location": f"{REPO}/pullrequests/4821/diff"})
    fake.routes["/pullrequests/4821/diff"] = loop
    with pytest.raises(PullRequestProviderError, match="more than 3 times"):
        _provider(fake).fetch_pull_request("ws/r", 4821)
    assert sum(r.url.path.endswith("/4821/diff") for r in fake.requests) == 4


def test_a_redirect_without_a_location_is_an_error_not_an_empty_diff() -> None:
    fake = _redirecting()
    fake.routes["/pullrequests/4821/diff"] = httpx.Response(302)
    with pytest.raises(PullRequestProviderError, match="no Location"):
        _provider(fake).fetch_pull_request("ws/r", 4821)


# ─── an empty diff that should not be empty ───────────────────────────


def _empty(files: int | None) -> _Bitbucket:
    fake = _redirecting()
    fake.routes["/diff/aaa111bbb222..ccc333ddd444?topic=true"] = httpx.Response(200, text="")
    stat = (httpx.Response(200, json={"values": [{}] * files, "size": files})
            if files is not None else httpx.Response(500, text="boom"))
    fake.routes["/pullrequests/4821/diffstat?pagelen=100"] = stat
    return fake


def test_a_pr_with_files_but_no_diff_is_an_error_not_a_skip(_no_sleep) -> None:
    fake = _empty(8)
    with pytest.raises(EmptyDiffError, match="diffstat lists 8 files") as caught:
        _provider(fake).fetch_pull_request("ws/r", 4821)

    assert _no_sleep == [2.0, 5.0], "two retries of the plain endpoint, 2 s then 5 s"
    exc = caught.value
    assert exc.files == 8
    assert exc.pr is not None and exc.pr.head_sha == "aaa111bbb222"
    assert exc.pr.reported_files == 8 and exc.pr.raw_diff == ""


def test_a_late_diff_is_picked_up_by_the_retry(_no_sleep) -> None:
    fake = _redirecting()
    answers = iter(["", "", SAMPLE_DIFF])
    fake.routes["/diff/aaa111bbb222..ccc333ddd444?topic=true"] = (
        lambda req: httpx.Response(200, text=next(answers)))
    fake.routes["/pullrequests/4821/diffstat?pagelen=100"] = httpx.Response(
        200, json={"values": [{}], "size": 1})

    pr = _provider(fake).fetch_pull_request("ws/r", 4821)

    assert pr.hunks and pr.reported_files == 1
    assert _no_sleep == [2.0]


def test_a_pr_with_no_files_and_no_diff_is_still_a_quiet_skip(_no_sleep) -> None:
    pr = _provider(_empty(0)).fetch_pull_request("ws/r", 4821)
    assert (pr.raw_diff, pr.hunks, pr.reported_files) == ("", [], 0)
    assert _no_sleep == []


def test_an_empty_diff_with_an_unreadable_diffstat_is_an_error_not_a_quiet_skip(_no_sleep) -> None:
    with pytest.raises(EmptyDiffError, match="diffstat could not be read") as caught:
        _provider(_empty(None)).fetch_pull_request("ws/r", 4821)

    assert _no_sleep == [2.0, 5.0], "retried like a diffstat that listed files"
    exc = caught.value
    assert exc.files is None
    assert exc.pr is not None and exc.pr.reported_files is None and exc.pr.raw_diff == ""


def test_a_late_diff_is_picked_up_even_when_the_diffstat_cannot_be_read(_no_sleep) -> None:
    fake = _redirecting()
    answers = iter(["", "", SAMPLE_DIFF])
    fake.routes["/diff/aaa111bbb222..ccc333ddd444?topic=true"] = (
        lambda req: httpx.Response(200, text=next(answers)))
    fake.routes["/pullrequests/4821/diffstat?pagelen=100"] = httpx.Response(503, text="later")
    pr = _provider(fake).fetch_pull_request("ws/r", 4821)
    assert pr.hunks and pr.reported_files is None


def test_a_dropped_connection_on_the_first_diff_is_the_providers_error_not_httpxs() -> None:
    fake = _redirecting()

    def reset(req):
        raise httpx.ReadTimeout("slow")

    fake.routes["/pullrequests/4821/diff"] = reset
    with pytest.raises(PullRequestProviderError, match="ReadTimeout"):
        _provider(fake).fetch_pull_request("ws/r", 4821)


def test_a_dropped_connection_while_retrying_does_not_end_the_retries(_no_sleep) -> None:
    fake = _empty(8)
    calls = {"n": 0}
    first = fake.routes["/pullrequests/4821/diff"]

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return first
        raise httpx.ConnectError("reset")

    fake.routes["/pullrequests/4821/diff"] = flaky
    with pytest.raises(EmptyDiffError):
        _provider(fake).fetch_pull_request("ws/r", 4821)
    assert _no_sleep == [2.0, 5.0]


def test_a_diff_error_status_names_the_status() -> None:
    fake = _redirecting()
    fake.routes["/diff/aaa111bbb222..ccc333ddd444?topic=true"] = httpx.Response(555, text="too big")
    with pytest.raises(PullRequestProviderError, match="diff error 555"):
        _provider(fake).fetch_pull_request("ws/r", 4821)


def test_an_orchestrator_run_with_files_but_no_diff_fails_and_says_so(monkeypatch) -> None:
    pr: PullRequest = _pr(hunks=[])
    pr.raw_diff = ""
    pr.reported_files = 8
    noted: list[tuple[int, str]] = []

    class _P(_Provider):
        def upsert_feedback_comment(self, pr, reason):
            noted.append((pr.number, reason))
            return 1

    orch = _orch(monkeypatch, [], policy=POLICY)
    rec = StageRecorder()
    with pytest.raises(PullRequestProviderError):
        orch.review("github", "o/r", 1, provider=_P(pr), stages=rec)

    gate = _stage(rec, "gate_hunks")
    assert gate["status"] == "failed"
    assert "8 changed files" in gate["reason"]
    assert noted and "8 changed files" in noted[0][1]
    assert _keys(rec)[-1] == "gate_hunks"


def test_an_empty_bitbucket_diff_with_unknown_files_fails_the_run_too(monkeypatch) -> None:
    pr: PullRequest = _pr(hunks=[])
    pr.provider = "bitbucket"
    pr.raw_diff = ""
    pr.reported_files = None
    noted: list[str] = []

    class _P(_Provider):
        def upsert_feedback_comment(self, pr, reason):
            noted.append(reason)
            return 1

    orch = _orch(monkeypatch, [], policy=POLICY)
    rec = StageRecorder()
    with pytest.raises(EmptyDiffError):
        orch.review("bitbucket", "o/r", 1, provider=_P(pr), stages=rec)
    assert _stage(rec, "gate_hunks")["status"] == "failed"
    assert noted and "could not say how many files" in noted[0]


def test_the_lost_diff_note_is_written_in_the_review_language(monkeypatch) -> None:
    pr: PullRequest = _pr(hunks=[])
    pr.raw_diff = ""
    pr.reported_files = 8
    noted: list[tuple[str, str | None]] = []

    class _P(_Provider):
        def upsert_feedback_comment(self, pr, reason):
            noted.append((reason, self.review_language))
            return 1

    orch = _orch(monkeypatch, [], policy={**POLICY, "review_language": "uk"})
    with pytest.raises(PullRequestProviderError):
        orch.review("github", "o/r", 1, provider=_P(pr), stages=StageRecorder())
    assert noted and "8 змінених файлів" in noted[0][0]
    assert noted[0][1] == "uk"


def test_an_orchestrator_run_whose_provider_lost_the_diff_tells_the_pr(monkeypatch) -> None:
    pr: PullRequest = _pr(hunks=[])
    noted: list[str] = []

    class _P(_Provider):
        def fetch_pull_request(self, repo, number):
            raise EmptyDiffError("empty", pr=pr, files=3)

        def upsert_feedback_comment(self, pr, reason):
            noted.append(reason)
            return 1

    rec = StageRecorder()
    with pytest.raises(EmptyDiffError):
        _orch(monkeypatch, [], policy=POLICY).review(
            "github", "o/r", 1, provider=_P(pr), stages=rec)

    assert _stage(rec, "fetch_pr")["status"] == "failed"
    assert noted and "3 changed files" in noted[0]


def test_an_orchestrator_run_with_no_files_and_no_diff_is_still_a_skip(monkeypatch) -> None:
    pr = _pr(hunks=[])
    pr.raw_diff = ""
    pr.reported_files = 0
    orch = _orch(monkeypatch, [], policy=POLICY)
    rec = StageRecorder()
    out = orch.review("github", "o/r", 1, provider=_Provider(pr), stages=rec)
    assert out.batch.run_status.value == "skipped"
    assert _stage(rec, "gate_hunks")["status"] == "skipped"


# ─── every diff GET follows, GitHub and GitLab refuse a 3xx ───────────


def test_every_pr_diff_get_follows_its_redirect() -> None:
    import inspect

    src = Path(bb_mod.__file__).read_text()
    # no plain GET in the module reaches a diff endpoint
    for m in re.finditer(r"self\._http\.get\(\s*([^\n]*)", src):
        assert "/diff" not in m.group(1), f"a diff GET that does not follow: {m.group(0)}"
    # the three that do reach one all go through the hop-by-hop follower
    for fn in (BitbucketPRProvider._get_diff, BitbucketPRProvider._get_spec_diff,
               BitbucketPRProvider._get_diffstat_files):
        body = inspect.getsource(fn)
        assert "self._get_follow(" in body and "self._http" not in body, fn.__name__
    # and the follower is the only place that reads a Location, and checks it
    assert src.count('headers.get("location")') == 1
    follower = inspect.getsource(BitbucketPRProvider._get_follow)
    assert "hostname" in follower and "_MAX_REDIRECTS" in follower
    # the one `/src/` read in apply_fix follows, for that request only
    fix = (Path(bb_mod.__file__).parents[2] / "api/routers/apply_fix.py").read_text()
    assert fix.count("follow_redirects=True") == 1


def test_a_github_or_gitlab_redirect_is_an_error_not_an_empty_diff() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/9"})

    for provider, repo in ((GitHubPRProvider(token="fake"), "o/r"),
                           (GitLabPRProvider(token="fake"), "g/p")):
        _patch_client(provider, httpx.MockTransport(handler))
        with pytest.raises(PullRequestProviderError, match="redirect"):
            provider.fetch_pull_request(repo, 1)
        provider.close()

    def meta_ok(req: httpx.Request) -> httpx.Response:
        if "diff" in req.headers.get("accept", ""):
            return httpx.Response(302, headers={"Location": "https://x/y"})
        return httpx.Response(200, json={"title": "t", "changed_files": 4})

    gh = GitHubPRProvider(token="fake")
    _patch_client(gh, httpx.MockTransport(meta_ok))
    with pytest.raises(PullRequestProviderError, match="GitHub diff answered with a redirect"):
        gh.fetch_pull_request("o/r", 1)
    gh.close()


def test_github_and_gitlab_report_how_many_files_the_pr_has() -> None:
    def gh(req: httpx.Request) -> httpx.Response:
        if "diff" in req.headers.get("accept", ""):
            return httpx.Response(200, text=SAMPLE_DIFF)
        return httpx.Response(200, json={"title": "t", "changed_files": 4})

    def gl(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/raw_diffs"):
            return httpx.Response(200, text=SAMPLE_DIFF)
        return httpx.Response(200, json={"title": "t", "changes_count": "1000+"})

    g1 = GitHubPRProvider(token="fake")
    _patch_client(g1, httpx.MockTransport(gh))
    assert g1.fetch_pull_request("o/r", 1).reported_files == 4
    g2 = GitLabPRProvider(token="fake")
    _patch_client(g2, httpx.MockTransport(gl))
    assert g2.fetch_pull_request("g/p", 1).reported_files is None, "'1000+' is not a count"


# ─── Cyrillic paths ───────────────────────────────────────────────────

_QUOTED = (
    'diff --git "a/\\321\\202\\320\\265\\321\\201\\321\\202.py" '
    '"b/\\321\\202\\320\\265\\321\\201\\321\\202.py"\n'
    "index 111..222 100644\n"
    '--- "a/\\321\\202\\320\\265\\321\\201\\321\\202.py"\n'
    '+++ "b/\\321\\202\\320\\265\\321\\201\\321\\202.py"\n'
    "@@ -1,2 +1,3 @@\n a\n+b\n c\n"
)


def test_a_cyrillic_path_survives_the_diff_and_the_anchor() -> None:
    hunks, skipped = parse_unified_diff(_QUOTED)
    assert skipped == []
    assert [h.file_path for h in hunks] == ["тест.py"]
    assert hunks[0].old_file_path == "тест.py"

    # the anchor a finding on that file is posted with is the same string
    from src.review.providers.base import _anchorable_ranges

    pr = PullRequest(provider="bitbucket", repo="ws/r", number=1, title="", description="",
                     author="", base_ref="main", base_sha="b", head_ref="f", head_sha="h",
                     state="open", hunks=hunks, raw_diff=_QUOTED)
    anchors = {(p, getattr(side, "value", side)) for p, side in _anchorable_ranges(pr)}
    assert ("тест.py", "RIGHT") in anchors


def test_a_plain_or_escaped_path_is_decoded_only_when_quoted() -> None:
    from src.review.diff import _strip_diff_prefix

    assert _strip_diff_prefix("b/src/a.py") == "src/a.py"
    assert _strip_diff_prefix('"b/we\\"ird\\\\name\\t.py"') == 'we"ird\\name\t.py'
    assert _strip_diff_prefix('"b/\\321\\202.py"') == "т.py"
    assert _strip_diff_prefix("/dev/null") == "/dev/null"


# ─── a description edit does not start another review ────────────────


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


@pytest.fixture
def pr_rows(monkeypatch):
    import src.review.issues as issues_mod

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(eng)
    monkeypatch.setattr(issues_mod, "_ENGINE", eng)
    yield eng
    eng.dispose()


def _seen(engine, *, head: str | None, status: str, number: int = 4821,
          posted: bool = True) -> None:
    """A PR whose last run read `head`. `posted` says its comments went up,
    which is what moves the baseline the guard reads (a dry run leaves it)."""
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        s.add(ReviewPullRequest(
            workspace_id="ws-1", provider="bitbucket", repo="acme/payments",
            number=number, state="open", reviews_count=1, head_sha=head,
            last_review_status=status,
            last_reviewed_sha=head if posted and status == "complete" else None,
        ))
        s.commit()


def _deliver(event: str, head: str, *, number: int = 4821):
    from src.review.webhook import _dispatch_review

    asyncio.run(_dispatch_review("bitbucket", "acme/payments", number, head_sha=head,
                                 event=event, expected_workspace_id="ws-1"))


def test_a_description_edit_does_not_start_another_review(
    bound, store, queue, no_ledger, pr_rows,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg(provider="bitbucket", repo_slug="acme-payments"))
    full = "aaa111bbb222" + "0" * 28
    _seen(pr_rows, head=full, status="complete")

    _deliver("pullrequest:updated", "aaa111bbb222")  # Bitbucket sends 12 characters
    assert queue == [], "same commit, already reviewed: an edit is not a push"
    assert store.list_for_pr("ws-1", "bitbucket", "acme/payments", 4821) == []

    _deliver("pullrequest:updated", "eee555fff666")
    assert len(queue) == 1, "a new commit is reviewed"


def test_a_dry_run_or_an_undelivered_review_does_not_stop_the_real_one(
    bound, store, queue, no_ledger, pr_rows,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg(provider="bitbucket", repo_slug="acme-payments"))
    # The run read the commit and finished, but nothing was posted.
    _seen(pr_rows, head="aaa111bbb222", status="complete", posted=False)
    _deliver("pullrequest:updated", "aaa111bbb222")
    assert len(queue) == 1, "nothing was ever shown on the PR for this commit"


def test_a_failed_skipped_or_unseen_pr_is_reviewed_again_on_an_update(
    bound, store, queue, no_ledger, pr_rows,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg(provider="bitbucket", repo_slug="acme-payments"))
    _seen(pr_rows, head="aaa111bbb222", status="failed", number=1)
    _seen(pr_rows, head="aaa111bbb222", status="skipped", number=2)
    for n in (1, 2, 3):  # 3 was never seen
        _deliver("pullrequest:updated", "aaa111bbb222", number=n)
    assert [q["payload"]["pr_number"] for q in queue] == [1, 2, 3]


def test_only_a_delivery_that_means_any_change_is_held_back(
    bound, store, queue, no_ledger, pr_rows,  # noqa: F811
):
    s, cfg = bound
    s.upsert(cfg(provider="bitbucket", repo_slug="acme-payments"))
    _seen(pr_rows, head="aaa111bbb222", status="complete")
    # a push-shaped or explicit delivery is never held back, even on the same commit
    _deliver("pullrequest:created", "aaa111bbb222")
    assert len(queue) == 1


def test_a_database_that_cannot_answer_does_not_stop_a_review(
    bound, store, queue, no_ledger, monkeypatch,  # noqa: F811
):
    import src.review.pr_state as pr_state_mod

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(pr_state_mod, "load", boom)
    s, cfg = bound
    s.upsert(cfg(provider="bitbucket", repo_slug="acme-payments"))
    _deliver("pullrequest:updated", "aaa111bbb222")
    assert len(queue) == 1
