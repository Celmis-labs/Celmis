"""Bitbucket abbreviates hashes in PR payloads and spells them out in commit lists."""

from __future__ import annotations

from src.productivity import deploys
from src.productivity.settings import ProductivitySettings
from tests.productivity.support import FakeProvider, at, enable, make_engine, pr, prs_in, sync

FULL = "a1b2c3d4e5f6" + "0" * 28


def test_a_12_character_pr_hash_is_found_in_a_40_character_commit_list() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"], integration_branches=["develop"])
    prs = [pr(10, target="develop", merged=at(-8), head_sha="a1b2c3d4E5F6", merge_sha="ffffffffffff"),
           pr(12, target="master", source="develop", title="Release", merged=at(-6))]
    sync(engine, FakeProvider(prs, shas={12: [FULL]}))
    assert prs_in(engine)[10].deploy_link == "sha"


def test_a_provider_deployment_sha_of_either_length_names_its_release_pr() -> None:
    from src.productivity.providers.base import ProviderDeployment

    release = deploys.PRFact(number=12, state="merged", target_branch="master", merged_at=at(-6),
                             merge_commit_sha="a1b2c3d4e5f6", head_sha=None, kind="feature",
                             reverts_pr_number=None, commit_shas=frozenset({"0123456789ab"}))
    cfg = ProductivitySettings(production_branches=["master"], deploy_source="provider")
    plans, links = deploys.plan_deployments([release], cfg, [ProviderDeployment("d", at(-6), sha=FULL)])
    assert plans[0].pr_numbers == {12} and links[12].link == "direct"
