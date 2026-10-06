"""Deployments, which PR rode in which one, and which deployments failed.

Pure functions over plain values, so each rule is a test with a name.

WHAT A DEPLOYMENT IS (`deploy_source`):
    merge     a merge into a production branch (default). Merges closer than
              `deploy_group_minutes` are one deployment — a release PR followed
              by a hotfix a minute later is one rollout, not two. The time is
              the LAST merge of the burst, when the final state went live.
    provider  the provider's own deployment records (environment "production").
    tags      tags matching `tag_pattern`.
Merge-derived numbers approximate a rollout; the UI labels them so.

WHICH PR IN WHICH DEPLOYMENT. A PR merged into a production branch is in the
deployment of that merge ('direct'). A PR merged into an INTEGRATION branch is
in the deployment whose commits contain its merge or head commit ('sha', the
release PR's commit list). Without such a match it is in the first deployment
after its merge, flagged 'time' — the approximation is visible, not hidden.

A FAILED DEPLOYMENT is one a revert or hotfix PR undid within
`failure_window_days`. The failed deployment is that of the PR the revert
names (`reverts_pr_number`), else the latest one before the fix. The fix's own
deployment time is when it recovered, which gives MTTR.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.productivity.providers.base import ProviderDeployment
from src.productivity.settings import ProductivitySettings


@dataclass(frozen=True)
class PRFact:
    number: int
    state: str
    target_branch: str | None
    merged_at: datetime | None
    merge_commit_sha: str | None = None
    head_sha: str | None = None
    kind: str = "feature"
    reverts_pr_number: int | None = None
    #: Commit shas of the PR (read only for PRs merged into production).
    commit_shas: frozenset[str] = frozenset()


@dataclass
class DeploymentPlan:
    external_id: str
    deployed_at: datetime
    source: str
    sha: str | None = None
    branch: str | None = None
    environment: str = "production"
    pr_numbers: set[int] = field(default_factory=set)
    shas: set[str] = field(default_factory=set)
    is_failure: bool = False
    failed_by_pr_number: int | None = None
    recovered_at: datetime | None = None


@dataclass(frozen=True)
class PRLink:
    external_id: str
    deployed_at: datetime
    link: str  # direct | sha | time


def branch_matches(branch: str | None, patterns: Iterable[str]) -> bool:
    return bool(branch) and any(fnmatch.fnmatchcase(branch, p) for p in patterns)  # type: ignore[arg-type]


def _short(sha: str | None) -> str:
    """12 lowercase characters: Bitbucket's pull request payloads abbreviate hashes
    that its commit lists spell out in full, so only a prefix compares across both."""
    return (sha or "").strip().lower()[:12]


def _shas(pr: PRFact) -> set[str]:
    return {_short(s) for s in (pr.merge_commit_sha, pr.head_sha, *pr.commit_shas) if s}


def _from_merges(prs: Sequence[PRFact], settings: ProductivitySettings) -> list[DeploymentPlan]:
    merged = sorted(
        (p for p in prs if p.state == "merged" and p.merged_at
         and branch_matches(p.target_branch, settings.production_branches)),
        key=lambda p: (p.merged_at, p.number))
    gap = timedelta(minutes=settings.deploy_group_minutes)
    plans: list[DeploymentPlan] = []
    for pr in merged:
        last = plans[-1] if plans else None
        if last is not None and pr.merged_at - last.deployed_at <= gap and last.branch == pr.target_branch:
            last.deployed_at = pr.merged_at
            last.sha = pr.merge_commit_sha or last.sha
        else:
            last = DeploymentPlan(
                external_id=f"merge:{pr.number}", deployed_at=pr.merged_at, source="merge",
                sha=pr.merge_commit_sha, branch=pr.target_branch)
            plans.append(last)
        last.pr_numbers.add(pr.number)
        last.shas |= _shas(pr)
    return plans


def _from_provider(
    prs: Sequence[PRFact], provided: Iterable[ProviderDeployment],
) -> list[DeploymentPlan]:
    by_sha: dict[str, PRFact] = {}
    for pr in prs:
        for sha in (pr.merge_commit_sha, pr.head_sha):
            if sha:
                by_sha[_short(sha)] = pr
    plans: list[DeploymentPlan] = []
    for d in sorted(provided, key=lambda d: (d.deployed_at, d.external_id)):
        if d.status != "success":
            continue
        plan = DeploymentPlan(
            external_id=d.external_id, deployed_at=d.deployed_at,
            source="tag" if d.source == "tag" else "provider", sha=d.sha, branch=d.branch,
            environment=d.environment)
        release = by_sha.get(_short(d.sha)) if d.sha else None
        if d.sha:
            plan.shas.add(_short(d.sha))
        if release is not None:
            plan.shas |= _shas(release)
            plan.pr_numbers.add(release.number)
        plans.append(plan)
    return plans


def plan_deployments(
    prs: Sequence[PRFact], settings: ProductivitySettings,
    provided: Iterable[ProviderDeployment] = (),
) -> tuple[list[DeploymentPlan], dict[int, PRLink]]:
    """The deployments of a repository and, per PR number, the one that shipped it."""
    plans = (_from_merges(prs, settings) if settings.deploy_source == "merge"
             else _from_provider(prs, provided))
    plans.sort(key=lambda d: (d.deployed_at, d.external_id))
    links = _link(prs, plans, settings)
    for pr_number, link in links.items():
        next(p for p in plans if p.external_id == link.external_id).pr_numbers.add(pr_number)
    _mark_failures(prs, plans, links, settings)
    return plans, links


def _link(
    prs: Sequence[PRFact], plans: list[DeploymentPlan], settings: ProductivitySettings,
) -> dict[int, PRLink]:
    links: dict[int, PRLink] = {}
    for pr in sorted(prs, key=lambda p: p.number):
        if pr.state != "merged" or not pr.merged_at:
            continue
        on_production = branch_matches(pr.target_branch, settings.production_branches)
        on_integration = branch_matches(pr.target_branch, settings.integration_branches)
        if not (on_production or on_integration):
            continue
        direct = next((p for p in plans if pr.number in p.pr_numbers), None)
        if direct is not None:
            links[pr.number] = PRLink(direct.external_id, direct.deployed_at, "direct")
            continue
        wanted = {_short(s) for s in (pr.merge_commit_sha, pr.head_sha) if s}
        carried = [p for p in plans if wanted & p.shas]
        if carried:
            first = min(carried, key=lambda p: p.deployed_at)
            links[pr.number] = PRLink(first.external_id, first.deployed_at, "sha")
            continue
        after = next((p for p in plans if p.deployed_at >= pr.merged_at), None)
        if after is not None:
            links[pr.number] = PRLink(after.external_id, after.deployed_at, "time")
    return links


def _mark_failures(
    prs: Sequence[PRFact], plans: list[DeploymentPlan], links: dict[int, PRLink],
    settings: ProductivitySettings,
) -> None:
    window = timedelta(days=settings.failure_window_days)
    by_id = {p.external_id: p for p in plans}
    fixes = sorted(
        (p for p in prs if p.kind in ("revert", "hotfix") and p.number in links),
        key=lambda p: (links[p.number].deployed_at, p.number))
    for fix in fixes:
        fixed_by = by_id[links[fix.number].external_id]
        target: DeploymentPlan | None = None
        named = links.get(fix.reverts_pr_number) if fix.reverts_pr_number else None
        if named is not None:
            target = by_id[named.external_id]
        elif fix.reverts_pr_number is None:
            earlier = [p for p in plans if p.deployed_at < fixed_by.deployed_at]
            target = earlier[-1] if earlier else None
        if target is None or target is fixed_by:
            continue
        if not timedelta(0) < fixed_by.deployed_at - target.deployed_at <= window:
            continue
        if not target.is_failure or (target.recovered_at and fixed_by.deployed_at < target.recovered_at):
            target.failed_by_pr_number = fix.number
            target.recovered_at = fixed_by.deployed_at
        target.is_failure = True
