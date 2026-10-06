"""Deployments from merges: grouping, the release PR's commits, and the flagged time fallback.

A repository with an integration branch: feature PRs merge into `develop`;
a release PR `develop -> master` is the deployment.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.models import ProductivityDeploymentPr
from tests.productivity.support import (
    FakeProvider,
    at,
    aware,
    deployments_in,
    enable,
    make_engine,
    pr,
    prs_in,
    sync,
)


def _prs():
    return [
        pr(10, target="develop", merged=at(-8), head_sha="h10", merge_sha="m10"),
        pr(11, target="develop", merged=at(-7), head_sha="h11", merge_sha="m11"),
        # the release PR: ships #10 (its commit list names m10) but not #11
        pr(12, target="master", source="develop", title="Release 2026-09-24", merged=at(-6),
           merge_sha="r12", head_sha="r12h"),
        pr(13, target="master", source="hotfix/x", title="Direct to master", merged=at(-2), merge_sha="m13"),
        pr(14, target="master", source="feature/y", merged=at(-2, minutes=10), merge_sha="m14"),
        # a PR into a feature branch is not on the way to production at all
        pr(15, target="feature/big", merged=at(-5)),
    ]


def _run():
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    sync(engine, FakeProvider(_prs(), shas={12: ["m10", "other"]}))
    return engine


def test_a_merge_into_a_production_branch_is_a_deployment() -> None:
    deps = deployments_in(_run())
    assert [(d.source, aware(d.deployed_at)) for d in deps] == [
        ("merge", at(-6)), ("merge", at(-2, minutes=10))]


def test_merges_within_the_grouping_gap_are_one_deployment_at_the_last_merge() -> None:
    engine = _run()
    burst = deployments_in(engine)[1]
    assert aware(burst.deployed_at) == at(-2, minutes=10)
    with Session(engine) as s:
        carried = sorted(r.pr_number for r in s.query(ProductivityDeploymentPr).filter_by(deployment_id=burst.id))
    assert carried == [13, 14]


def test_a_pr_in_the_release_commit_list_is_linked_by_sha() -> None:
    row = prs_in(_run())[10]
    assert row.deploy_link == "sha"
    assert aware(row.prod_deployed_at) == at(-6)


def test_a_pr_the_release_does_not_name_falls_back_to_time_and_says_so() -> None:
    engine = _run()
    row = prs_in(engine)[11]
    # merged at -7 days, first deployment after is the release at -6 days
    assert row.deploy_link == "time"
    assert aware(row.prod_deployed_at) == at(-6)


def test_a_pr_merged_straight_into_production_deploys_at_its_own_merge() -> None:
    rows = prs_in(_run())
    assert rows[12].deploy_link == "direct" and aware(rows[12].prod_deployed_at) == at(-6)
    assert rows[13].deploy_link == "direct" and aware(rows[13].prod_deployed_at) == at(-2, minutes=10)


def test_a_pr_on_its_way_nowhere_has_no_deployment() -> None:
    row = prs_in(_run())[15]
    assert row.prod_deployed_at is None and row.deploy_link is None


def test_a_pr_merged_after_the_last_deployment_is_undeployed_not_guessed() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    sync(engine, FakeProvider([
        pr(1, target="master", source="develop", merged=at(-6), merge_sha="r1"),
        pr(2, target="develop", merged=at(-1), merge_sha="m2")]))
    assert prs_in(engine)[2].prod_deployed_at is None


def test_the_release_commit_list_is_only_read_for_prs_merged_into_production() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    provider = FakeProvider(_prs(), shas={12: ["m10"]})
    sync(engine, provider)
    assert sorted(provider.sha_calls) == [12, 13, 14]


def test_without_integration_branches_no_commit_list_is_read_at_all() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"])
    provider = FakeProvider(_prs())
    sync(engine, provider)
    assert provider.sha_calls == []


def test_a_rerun_rebuilds_the_same_deployments_without_doubling_them() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    for _ in range(2):
        sync(engine, FakeProvider(_prs(), shas={12: ["m10"]}))
    assert len(deployments_in(engine)) == 2


def test_the_default_production_branches_are_main_and_master() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(1, target="main", merged=at(-3)), pr(2, target="develop", merged=at(-2))]))
    assert len(deployments_in(engine)) == 1


def test_provider_deployments_link_a_pr_by_the_sha_they_deployed() -> None:
    from src.productivity.providers.base import ProviderDeployment

    engine = make_engine()
    enable(engine, deploy_source="provider", production_branches=["master"], integration_branches=["develop"])
    provided = [ProviderDeployment("dep-1", at(-5), sha="r12"), ProviderDeployment("dep-2", at(-1), sha="zzz")]
    sync(engine, FakeProvider(_prs(), shas={12: ["m10"]}, provided=provided))
    deps = deployments_in(engine)
    assert [(d.source, d.external_id) for d in deps] == [("provider", "dep-1"), ("provider", "dep-2")]
    rows = prs_in(engine)
    assert rows[10].deploy_link == "sha" and aware(rows[10].prod_deployed_at) == at(-5)
    assert rows[11].deploy_link == "time"
