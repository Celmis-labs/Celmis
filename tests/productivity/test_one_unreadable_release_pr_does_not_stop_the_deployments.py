"""A 404 on one release PR's commit list is that PR's problem, not the repository's."""

from __future__ import annotations

from tests.productivity.support import (
    FakeProvider,
    at,
    aware,
    deployments_in,
    enable,
    make_engine,
    pr,
    prs_in,
    state_of,
    sync,
)


def _prs():
    return [
        pr(10, target="develop", merged=at(-8), head_sha="h10", merge_sha="m10"),
        pr(12, target="master", source="develop", title="Release A", merged=at(-6), merge_sha="r12"),
        pr(13, target="master", source="develop", title="Release B", merged=at(-2), merge_sha="r13"),
    ]


def _run(sha_errors):
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    result = sync(engine, FakeProvider(_prs(), shas={12: ["m10"], 13: ["x"]}, sha_errors=sha_errors))
    return engine, result


def test_deployments_are_still_rebuilt_when_one_release_pr_cannot_be_read() -> None:
    engine, result = _run({13})
    assert result.status == "ok"
    assert [aware(d.deployed_at) for d in deployments_in(engine)] == [at(-6), at(-2)]


def test_the_release_prs_that_can_be_read_keep_their_commit_lists() -> None:
    engine, _ = _run({13})
    rows = prs_in(engine)
    assert rows[12].commit_shas == ["m10"] and rows[13].commit_shas is None
    assert rows[10].deploy_link == "sha"


def test_the_unreadable_one_is_reported_and_tried_again_on_the_next_run() -> None:
    engine, _ = _run({13})
    assert "commit list failed for 1 PR" in state_of(engine).last_error
    provider = FakeProvider(_prs(), shas={12: ["m10"], 13: ["x"]})
    sync(engine, provider, now=at(minutes=5))
    assert provider.sha_calls == [13]
    assert prs_in(engine)[13].commit_shas == ["x"]
