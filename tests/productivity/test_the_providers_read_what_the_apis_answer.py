"""The three adapters against canned API answers: what they ask, what they make of the reply, how they stop."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.productivity.providers.base import ProviderError, PRRecord, parse_ts
from src.productivity.providers.bitbucket import BitbucketProductivity
from src.productivity.providers.github import GitHubProductivity
from src.productivity.providers.gitlab import GitLabProductivity
from src.productivity.ratelimit import RateGate, RateLimited
from src.productivity.settings import BUILTIN

SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _gate() -> RateGate:
    return RateGate(3600, burst=1000, sleep=lambda s: None)


def _json(data, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers or {})


# ─── Bitbucket ────────────────────────────────────────────────────────

BB = "https://api.bitbucket.org/2.0/repositories/acme/app"


def _bb(handler, **kw) -> BitbucketProductivity:
    return BitbucketProductivity("acme/app", client=_client(handler), gate=_gate(),
                                 email=kw.get("email", "dev@acme.test"), token="tok")


def _bb_pr(i, state="MERGED", updated="2026-09-10T10:00:00.123456+00:00"):
    return {"id": i, "title": f"PR {i}", "description": "d", "state": state,
            "created_on": "2026-09-01T08:00:00+00:00", "updated_on": updated,
            "author": {"account_id": "acc-1", "nickname": "ann", "display_name": "Ann"},
            "source": {"branch": {"name": "feature/x"}, "commit": {"hash": "h1"}},
            "destination": {"branch": {"name": "master"}}, "merge_commit": {"hash": "m1"},
            "links": {"html": {"href": f"https://bitbucket.org/acme/app/pull-requests/{i}"}}}


def test_bitbucket_lists_oldest_update_first_and_follows_next_on_the_same_host() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("page") == "2":
            return _json({"values": [_bb_pr(2, "OPEN")]})
        return _json({"values": [_bb_pr(1)], "next": f"{BB}/pullrequests?page=2"})

    pages = list(_bb(handler).list_pull_requests(SINCE))
    assert [[p.number for p in page] for page in pages] == [[1], [2]]
    first = seen[0].url.params
    assert first["sort"] == "updated_on"
    assert first["q"] == "updated_on >= 2026-09-01T00:00:00+00:00"
    assert set(first.get_list("state")) == {"OPEN", "MERGED", "DECLINED", "SUPERSEDED"}
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_bitbucket_without_an_email_uses_a_bearer_token() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return _json({"values": []})

    list(_bb(handler, email="").list_pull_requests(SINCE))
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_a_bitbucket_record_is_normalised_and_the_merge_time_is_marked_approximate() -> None:
    page = next(_bb(lambda r: _json({"values": [_bb_pr(1)]})).list_pull_requests(SINCE))
    rec = page[0]
    assert rec.state == "merged" and rec.author_key == "acc-1" and rec.target_branch == "master"
    assert rec.merge_commit_sha == "m1" and rec.head_sha == "h1"
    assert rec.merged_at == rec.updated_on and rec.merged_at_approx is True
    assert rec.detail is None


def test_a_bitbucket_link_that_leaves_the_api_host_is_refused() -> None:
    def handler(request):
        return _json({"values": [_bb_pr(1)], "next": "https://evil.example/steal?token=1"})

    with pytest.raises(ProviderError):
        list(_bb(handler).list_pull_requests(SINCE))


def test_a_bitbucket_redirect_is_an_error_not_something_followed() -> None:
    with pytest.raises(ProviderError, match="redirect"):
        list(_bb(lambda r: httpx.Response(302, headers={"location": "https://x"})).list_pull_requests(SINCE))


def test_a_bitbucket_429_stops_with_the_time_the_provider_named() -> None:
    gate = _gate()
    provider = BitbucketProductivity("acme/app", gate=gate, token="tok", email="e",
                                     client=_client(lambda r: httpx.Response(429, headers={"Retry-After": "90"})))
    with pytest.raises(RateLimited) as caught:
        list(provider.list_pull_requests(SINCE))
    wait = (caught.value.until - datetime.now(UTC)).total_seconds()
    assert 80 < wait <= 91
    with pytest.raises(RateLimited):         # and the shared gate now refuses everybody
        gate.acquire(max_wait=5)


def test_bitbucket_detail_reads_the_oldest_commit_from_the_last_page_and_the_exact_merge_time() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/commits"):
            if request.url.params.get("page") == "3":
                return _json({"values": [{"hash": "c1", "date": "2026-08-30T09:00:00+00:00"},
                                         {"hash": "c0", "date": "2026-08-31T09:00:00+00:00"}]})
            return _json({"size": 120, "next": f"{BB}/pullrequests/1/commits?page=2",
                          "values": [{"hash": "c9", "date": "2026-09-09T09:00:00+00:00"}]})
        if path.endswith("/activity"):
            return _json({"values": [
                {"update": {"state": "OPEN", "date": "2026-09-01T08:00:00+00:00"}},
                {"comment": {"id": 11, "created_on": "2026-09-02T10:00:00+00:00", "user": {"account_id": "acc-2"},
                             "content": {"raw": "<!-- code-analyzer:review -->"}}},
                {"comment": {"id": 12, "created_on": "2026-09-03T10:00:00+00:00", "deleted": True,
                             "user": {"account_id": "acc-2"}, "content": {"raw": "gone"}}},
                {"approval": {"date": "2026-09-04T10:00:00+00:00", "user": {"account_id": "acc-3", "display_name": "Bo"}}},
                {"update": {"state": "MERGED", "date": "2026-09-05T11:00:00+00:00"}},
            ]})
        if path.endswith("/diffstat"):
            return _json({"values": [{"lines_added": 10, "lines_removed": 2}, {"lines_added": 5, "lines_removed": 0}]})
        raise AssertionError(path)

    provider = _bb(handler)
    detail = provider.pr_detail(PRRecord(1, "t", "merged", None, None))
    assert detail.first_commit_at == parse_ts("2026-08-30T09:00:00+00:00")
    assert detail.commits_count == 120
    assert detail.merged_at == parse_ts("2026-09-05T11:00:00+00:00")
    assert (detail.additions, detail.deletions, detail.files_changed) == (15, 2, 2)
    kinds = [(a.kind, a.actor_key) for a in detail.activity]
    assert kinds == [("comment", "acc-2"), ("approval", "acc-3")]      # the deleted comment is gone
    assert detail.activity[0].raw.startswith("<!-- code-analyzer:review")
    assert len(calls) == 4                    # commits (first + last page), activity, diffstat: not a walk of 3 pages


def test_bitbucket_estimates_a_backfill_from_the_size_of_the_listing() -> None:
    provider = _bb(lambda r: _json({"size": 1031, "values": []}))
    assert provider.count_pull_requests(SINCE) == 1031


def test_bitbucket_deployments_are_the_successful_production_ones_since_the_date() -> None:
    def handler(request):
        if request.url.path.endswith("/environments/"):
            return _json({"values": [{"uuid": "{prod}", "name": "Production", "environment_type": {"name": "Production"}},
                                     {"uuid": "{stg}", "name": "Staging", "environment_type": {"name": "Staging"}}]})
        return _json({"values": [
            {"uuid": "{d1}", "environment": {"uuid": "{prod}"}, "release": {"commit": {"hash": "abc"}},
             "state": {"status": {"name": "SUCCESSFUL"}, "completed_on": "2026-09-10T10:00:00+00:00"}},
            {"uuid": "{d2}", "environment": {"uuid": "{stg}"}, "state": {"status": {"name": "SUCCESSFUL"},
                                                                         "completed_on": "2026-09-11T10:00:00+00:00"}},
            {"uuid": "{d3}", "environment": {"uuid": "{prod}"}, "state": {"status": {"name": "FAILED"},
                                                                          "completed_on": "2026-09-12T10:00:00+00:00"}},
            {"uuid": "{d4}", "environment": {"uuid": "{prod}"}, "state": {"status": {"name": "SUCCESSFUL"},
                                                                          "completed_on": "2026-01-01T10:00:00+00:00"}},
        ]})

    got = _bb(handler).deployments(SINCE, BUILTIN)
    assert [(d.external_id, d.sha) for d in got] == [("{d1}", "abc")]



def test_a_bitbucket_redirect_inside_the_api_is_followed_with_the_gate_spent_on_each_hop() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/diffstat"):
            return httpx.Response(302, headers={"location": f"{BB}/diffstat/h1..m1?topic=true"})
        assert request.headers["authorization"].startswith("Basic ")      # the token went to the same API only
        return _json({"values": [{"lines_added": 4, "lines_removed": 1}]})

    got = _bb(handler)._get(f"{BB}/pullrequests/1/diffstat")
    assert got.status_code == 200 and len(seen) == 2


def test_a_bitbucket_redirect_off_the_api_host_or_endless_is_still_an_error() -> None:
    off = _bb(lambda r: httpx.Response(302, headers={"location": "https://evil.example/x"}))
    with pytest.raises(ProviderError, match="redirect"):
        off._get(f"{BB}/pullrequests/1/diffstat")
    loop = _bb(lambda r: httpx.Response(302, headers={"location": f"{BB}/again"}))
    with pytest.raises(ProviderError, match="redirect"):
        loop._get(f"{BB}/pullrequests/1/diffstat")
    bare = _bb(lambda r: httpx.Response(302))
    with pytest.raises(ProviderError, match="redirect"):
        bare._get(f"{BB}/pullrequests/1/diffstat")


def test_bitbucket_detail_survives_a_diffstat_that_redirects_to_the_spec_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return _json({"size": 1, "values": [{"hash": "c1", "date": "2026-08-30T09:00:00+00:00"}]})
        if path.endswith("/activity"):
            return _json({"values": [{"update": {"state": "MERGED", "date": "2026-09-05T11:00:00+00:00"}}]})
        if path.endswith("/diffstat"):
            return httpx.Response(302, headers={"location": f"{BB}/diffstat/h1..m1?topic=true"})
        if "/diffstat/" in path:
            return _json({"values": [{"lines_added": 7, "lines_removed": 3}]})
        raise AssertionError(path)

    detail = _bb(handler).pr_detail(PRRecord(1, "t", "merged", None, None))
    assert (detail.additions, detail.deletions, detail.files_changed) == (7, 3, 1)


def test_bitbucket_detail_keeps_the_pr_when_only_the_diffstat_cannot_be_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return _json({"size": 1, "values": [{"hash": "c1", "date": "2026-08-30T09:00:00+00:00"}]})
        if path.endswith("/activity"):
            return _json({"values": [{"update": {"state": "MERGED", "date": "2026-09-05T11:00:00+00:00"}}]})
        return httpx.Response(404)

    detail = _bb(handler).pr_detail(PRRecord(1, "t", "merged", None, None))
    assert detail.merged_at == parse_ts("2026-09-05T11:00:00+00:00") and detail.commits_count == 1
    assert (detail.additions, detail.deletions, detail.files_changed) == (None, None, None)


def test_bitbucket_reads_a_long_activity_feed_past_ten_pages_and_says_when_it_still_is_cut(caplog) -> None:
    def feed(pages: int):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page") or 1)
            if not request.url.path.endswith("/activity"):
                return _json({"values": [], "size": 0})
            more = {"next": f"{BB}/pullrequests/1/activity?page={page + 1}"} if page < pages else {}
            return _json({"values": [{"approval": {"date": "2026-09-04T10:00:00+00:00",
                                                   "user": {"account_id": f"acc-{page}"}}}], **more})
        return handler

    detail = _bb(feed(25)).pr_detail(PRRecord(1, "t", "merged", None, None))
    assert len(detail.activity) == 25 and "activity_truncated" not in caplog.text
    with caplog.at_level("WARNING"):
        cut = _bb(feed(60)).pr_detail(PRRecord(1, "t", "merged", None, None))
    assert len(cut.activity) == 40 and "bitbucket_activity_truncated pr=1" in caplog.text


# ─── GitHub ───────────────────────────────────────────────────────────

def _gh_node(number, updated, state="MERGED"):
    return {
        "number": number, "title": f"PR {number}", "body": "b", "url": f"https://github.com/acme/app/pull/{number}",
        "state": state, "isDraft": False, "createdAt": "2026-09-01T08:00:00Z", "updatedAt": updated,
        "mergedAt": "2026-09-05T11:00:00Z" if state == "MERGED" else None, "closedAt": None,
        "additions": 30, "deletions": 4, "changedFiles": 3, "baseRefName": "main", "headRefName": "feature/x",
        "headRefOid": "h", "mergeCommit": {"oid": "m"}, "author": {"login": "ann", "__typename": "User"},
        "commits": {"totalCount": 2, "nodes": [{"commit": {"authoredDate": "2026-08-31T09:00:00Z",
                                                            "committedDate": "2026-08-31T09:00:00Z"}}]},
        "reviews": {"nodes": [
            {"databaseId": 1, "state": "APPROVED", "submittedAt": "2026-09-03T10:00:00Z", "body": "",
             "author": {"login": "bob", "__typename": "User"}, "comments": {"nodes": []}},
            {"databaseId": 2, "state": "COMMENTED", "submittedAt": "2026-09-02T10:00:00Z", "body": "",
             "author": {"login": "bob", "__typename": "User"},
             "comments": {"nodes": [{"body": "x\n<!-- celmis:finding fp=0123456789abcdef sha=0123456789ab -->"}]}},
            {"databaseId": 3, "state": "PENDING", "submittedAt": None, "body": "", "author": {"login": "bob"},
             "comments": {"nodes": []}},
        ]},
        "comments": {"nodes": [{"databaseId": 9, "createdAt": "2026-09-02T12:00:00Z", "body": "ok",
                                "author": {"login": "dependabot[bot]", "__typename": "Bot"}}]},
    }


def _gh(handler) -> GitHubProductivity:
    return GitHubProductivity("acme/app", client=_client(handler), gate=_gate(), token="tok")


def test_github_returns_everything_in_the_list_and_oldest_first_and_stops_at_since() -> None:
    queries = []

    def handler(request):
        body = json.loads(request.content)
        queries.append(body)
        after = body["variables"]["after"]
        nodes = ([_gh_node(3, "2026-09-20T00:00:00Z"), _gh_node(2, "2026-09-10T00:00:00Z")] if after is None
                 else [_gh_node(1, "2026-08-01T00:00:00Z")])
        return _json({"data": {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": after is None, "endCursor": "c1"}, "nodes": nodes}}}})

    pages = list(_gh(handler).list_pull_requests(SINCE))
    assert [p.number for page in pages for p in page] == [2, 3]
    assert len(queries) == 2


def test_a_github_record_carries_its_detail_and_tells_bots_from_people() -> None:
    node = _gh_node(5, "2026-09-10T00:00:00Z")
    rec = GitHubProductivity._record(node)
    d = rec.detail
    assert rec.state == "merged" and rec.merge_commit_sha == "m" and rec.author_key == "ann"
    assert (d.additions, d.deletions, d.files_changed, d.commits_count) == (30, 4, 3, 2)
    assert d.first_commit_at == parse_ts("2026-08-31T09:00:00Z")
    by_id = {a.external_id: a for a in d.activity}
    assert set(by_id) == {"approval:1", "review:2", "comment:9"}     # the pending review is not an act yet
    assert by_id["comment:9"].bot_account is True and by_id["approval:1"].bot_account is False
    assert "celmis:finding" in by_id["review:2"].raw


def test_github_graphql_rate_limit_error_becomes_a_rate_limit() -> None:
    provider = _gh(lambda r: _json({"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}))
    with pytest.raises(RateLimited):
        list(provider.list_pull_requests(SINCE))


def test_github_secondary_limit_on_rest_is_a_rate_limit_with_the_reset_time() -> None:
    reset = str(int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()))
    provider = _gh(lambda r: httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}, text="x"))
    with pytest.raises(RateLimited) as caught:
        provider.pr_commit_shas(1)
    assert caught.value.until > datetime.now(UTC) + timedelta(minutes=9)


def test_github_commit_list_follows_link_headers_on_the_same_host() -> None:
    def handler(request):
        if request.url.params.get("page") == "2":
            return _json([{"sha": "s3"}])
        return _json([{"sha": "s1"}, {"sha": "s2"}],
                     headers={"Link": '<https://api.github.com/repos/acme/app/pulls/1/commits?page=2>; rel="next"'})

    assert _gh(handler).pr_commit_shas(1) == ["s1", "s2", "s3"]


def test_github_deployments_are_the_ones_whose_latest_status_is_success() -> None:
    def handler(request):
        if request.url.path.endswith("/deployments"):
            return _json([{"id": 1, "sha": "a", "ref": "main", "environment": "production", "created_at": "2026-09-10T10:00:00Z"},
                          {"id": 2, "sha": "b", "ref": "main", "environment": "production", "created_at": "2026-09-11T10:00:00Z"}])
        state = "success" if "/1/" in request.url.path else "failure"
        return _json([{"state": state, "created_at": "2026-09-10T10:05:00Z"}])

    got = _gh(handler).deployments(SINCE, BUILTIN)
    assert [(d.external_id, d.sha, d.deployed_at) for d in got] == [("1", "a", parse_ts("2026-09-10T10:05:00Z"))]


# ─── GitLab ───────────────────────────────────────────────────────────

GL_API = "https://gitlab.com/api/v4"


def _gl(handler) -> GitLabProductivity:
    return GitLabProductivity("acme/group/app", client=_client(handler), gate=_gate(), token="tok", api_base=GL_API)


def _gl_mr(iid, state="merged"):
    return {"iid": iid, "title": f"MR {iid}", "description": "d", "web_url": f"https://gitlab.com/x/-/merge_requests/{iid}",
            "state": state, "draft": False, "author": {"id": 77, "username": "ann"},
            "source_branch": "feature/x", "target_branch": "main", "created_at": "2026-09-01T08:00:00.000Z",
            "updated_at": "2026-09-10T10:00:00.000Z", "merged_at": "2026-09-05T11:00:00.000Z", "closed_at": None,
            "sha": "h", "merge_commit_sha": "m", "squash_commit_sha": None}


def test_gitlab_lists_with_updated_after_and_follows_the_next_page_header() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("page") == "2":
            return _json([_gl_mr(2, "opened")])
        return _json([_gl_mr(1)], headers={"X-Next-Page": "2"})

    pages = list(_gl(handler).list_pull_requests(SINCE))
    assert [[p.number for p in page] for page in pages] == [[1], [2]]
    q = seen[0].url.params
    assert q["updated_after"] == "2026-09-01T00:00:00Z" and q["sort"] == "asc" and q["state"] == "all"
    assert seen[0].url.raw_path.startswith(b"/api/v4/projects/acme%2Fgroup%2Fapp/merge_requests")
    assert seen[0].headers["private-token"] == "tok"


def test_a_gitlab_merge_request_is_normalised_with_its_real_merge_time_and_no_size() -> None:
    rec = next(_gl(lambda r: _json([_gl_mr(1)])).list_pull_requests(SINCE))[0]
    assert rec.state == "merged" and rec.author_key == "77" and rec.merge_commit_sha == "m"
    assert rec.merged_at == parse_ts("2026-09-05T11:00:00.000Z") and rec.merged_at_approx is False


def test_gitlab_detail_takes_the_oldest_commit_the_notes_and_the_approval_system_note() -> None:
    def handler(request):
        path = request.url.path
        if path.endswith("/commits"):
            if request.url.params.get("page") == "2":
                return _json([{"id": "c0", "authored_date": "2026-08-30T09:00:00Z"}])
            return _json([{"id": "c9", "authored_date": "2026-09-04T09:00:00Z"}],
                         headers={"X-Total": "101", "X-Total-Pages": "2"})
        return _json([
            {"id": 1, "system": True, "body": "approved this merge request", "created_at": "2026-09-03T10:00:00Z",
             "author": {"id": 5, "username": "bob"}},
            {"id": 2, "system": True, "body": "added 1 commit", "created_at": "2026-09-03T11:00:00Z",
             "author": {"id": 5, "username": "bob"}},
            {"id": 3, "system": False, "body": "please fix", "created_at": "2026-09-03T12:00:00Z",
             "author": {"id": 5, "username": "bob"}},
        ])

    detail = _gl(handler).pr_detail(PRRecord(1, "t", "merged", None, None, merged_at=parse_ts("2026-09-05T11:00:00Z")))
    assert detail.first_commit_at == parse_ts("2026-08-30T09:00:00Z") and detail.commits_count == 101
    assert [(a.kind, a.external_id) for a in detail.activity] == [("approval", "approval:1"), ("comment", "comment:3")]
    assert detail.additions is None and detail.deletions is None


def test_a_gitlab_429_is_a_rate_limit() -> None:
    with pytest.raises(RateLimited):
        list(_gl(lambda r: httpx.Response(429, headers={"Retry-After": "30"})).list_pull_requests(SINCE))


def test_a_gitlab_next_link_to_another_host_is_not_followed() -> None:
    def handler(request):
        return _json([_gl_mr(1)], headers={"Link": '<https://evil.example/api/v4/x?page=2>; rel="next"'})

    pages = list(_gl(handler).list_pull_requests(SINCE))
    assert len(pages) == 1


def test_a_self_hosted_gitlab_is_asked_at_its_own_address() -> None:
    seen = []
    provider = GitLabProductivity("g/p", client=_client(lambda r: seen.append(r) or _json([])), gate=_gate(),
                                  token="t", api_base="https://git.acme.test/gitlab/api/v4")
    list(provider.list_pull_requests(SINCE))
    assert seen[0].url.host == "git.acme.test" and seen[0].url.path.startswith("/gitlab/api/v4/projects/")


def test_bitbucket_detail_walks_the_commit_pages_when_the_api_leaves_out_size() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return _json({"next": f"{BB}/pullrequests/1/commits?page=2",
                          "values": [{"hash": "c9", "date": "2026-09-09T09:00:00+00:00"}]})
        if path.endswith("/activity") or path.endswith("/diffstat"):
            return _json({"values": []})
        if request.url.params.get("page") == "2":
            return _json({"next": f"{BB}/pullrequests/1/commits?page=3",
                          "values": [{"hash": "c5", "date": "2026-09-05T09:00:00+00:00"}]})
        raise AssertionError(request.url)

    def paged(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits") and request.url.params.get("page") == "3":
            return _json({"values": [{"hash": "c0", "date": "2026-08-30T09:00:00+00:00"}]})
        if request.url.path.endswith("/commits") and request.url.params.get("page") == "2":
            return _json({"next": f"{BB}/pullrequests/1/commits?page=3",
                          "values": [{"hash": "c5", "date": "2026-09-05T09:00:00+00:00"}]})
        return handler(request)

    detail = _bb(paged).pr_detail(PRRecord(1, "t", "merged", None, None))
    assert detail.first_commit_at == parse_ts("2026-08-30T09:00:00+00:00")
    assert detail.commits_count == 3


def test_github_refuses_a_list_it_had_to_cut_short_rather_than_lose_the_oldest_prs() -> None:
    def handler(request):
        return _json({"data": {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": True, "endCursor": "c"},
            "nodes": [_gh_node(1, "2026-09-20T00:00:00Z")]}}}})

    with pytest.raises(ProviderError, match="shorten the backfill window"):
        list(_gh(handler).list_pull_requests(SINCE))
