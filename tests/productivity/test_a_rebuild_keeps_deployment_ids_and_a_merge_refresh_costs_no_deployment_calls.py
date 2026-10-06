"""Deployments are upserted, and a webhook refresh does not re-read the provider's deployments."""

from __future__ import annotations

from src.productivity import sync as ps
from src.productivity.providers.base import ProviderDeployment
from tests.productivity.support import (
    PROVIDER,
    REPO,
    WS,
    FakeProvider,
    at,
    deployments_in,
    enable,
    make_engine,
    pr,
    sync,
)


def test_a_second_rebuild_keeps_every_deployment_id_and_adds_only_the_new_one() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"])
    prs = [pr(1, target="master", merged=at(-5)), pr(2, target="master", merged=at(-3))]
    sync(engine, FakeProvider(prs))
    before = {d.external_id: d.id for d in deployments_in(engine)}
    prs.append(pr(3, target="master", merged=at(-1)))
    sync(engine, FakeProvider(prs), now=at(minutes=5))
    after = {d.external_id: d.id for d in deployments_in(engine)}
    assert len(after) == 3 and all(after[k] == v for k, v in before.items())


def test_a_deployment_that_vanished_is_deleted_and_its_prs_are_unlinked() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], deploy_source="provider")
    kept = ProviderDeployment("d1", at(-4), sha="s1")
    gone = ProviderDeployment("d2", at(-2), sha="s2")
    prs = [pr(1, target="master", merged=at(-3), merge_sha="s2")]
    sync(engine, FakeProvider(prs, provided=[kept, gone]))
    assert {d.external_id for d in deployments_in(engine)} == {"d1", "d2"}
    sync(engine, FakeProvider(prs, provided=[kept]), now=at(minutes=5))
    assert {d.external_id for d in deployments_in(engine)} == {"d1"}


def test_a_merge_refresh_rebuilds_from_the_stored_provider_deployments_without_asking_for_them() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], deploy_source="provider")
    prs = [pr(1, target="master", merged=at(-3), merge_sha="s1")]
    sync(engine, FakeProvider(prs, provided=[ProviderDeployment("d1", at(-2), sha="s1")]))
    provider = FakeProvider(prs, provided=[ProviderDeployment("d9", at(-1), sha="s9")])
    result = ps.refresh_pull_request(WS, PROVIDER, REPO, 1, provider_factory=lambda cfg: provider,
                                     engine=engine, now=at(minutes=5))
    assert result.status == "ok" and provider.deployment_calls == 0
    assert [d.external_id for d in deployments_in(engine)] == ["d1"]
