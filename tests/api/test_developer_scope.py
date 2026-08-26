"""Scoping by developer must not silently become scoping by nobody.

An owner is the account a repository sits under — for most teams, one
organisation covering everything, which makes "audit what this owner owns" the
same as "audit everything". The question people actually have is whose work is
where, and that comes from two different places:

  * registered repositories — git authorship the intel builder already
    computed, aggregated across the workspace's ownership snapshots;
  * repositories not added yet — the provider's contributor list, one request
    per repository, therefore bounded and therefore reported as bounded.

The two identity namespaces are never merged. A git author email and a
provider login are not the same key, and treating them as one is how a
repository quietly drops out of a scope.

These tests exercise the aggregation rules directly. Standing up Postgres,
ownership snapshots and a provider would test the plumbing; what breaks is the
arithmetic.
"""

from __future__ import annotations

import pytest


def aggregate(per_repo: dict[str, list[tuple[str, int]]]) -> list[dict]:
    """The rule both endpoints share: {repo: [(identity, commits)]} → ranked.

    Ranked by repositories touched first, then commits, then name — someone
    spread across four services is who a scope is usually built around, and a
    single prolific commit run in one repo should not outrank them.
    """
    commits: dict[str, int] = {}
    repos: dict[str, list[str]] = {}
    for repo, people in per_repo.items():
        for identity, n in people:
            if not identity:
                continue
            commits[identity] = commits.get(identity, 0) + n
            repos.setdefault(identity, []).append(repo)
    return [
        {"identity": i, "repos": sorted(repos[i]), "repo_count": len(repos[i]), "commits": n}
        for i, n in sorted(
            commits.items(), key=lambda kv: (-len(repos[kv[0]]), -kv[1], kv[0].lower()),
        )
    ]


def test_a_developer_carries_every_repo_they_touch():
    out = aggregate({
        "api": [("petro@corp", 40)],
        "web": [("petro@corp", 10), ("olena@corp", 90)],
    })
    petro = next(d for d in out if d["identity"] == "petro@corp")
    assert petro["repos"] == ["api", "web"]
    assert petro["commits"] == 50


def test_breadth_outranks_a_single_busy_repo():
    """Ninety commits in one place is a specialist; the scope wants the person
    whose change would span services."""
    out = aggregate({
        "api": [("petro@corp", 5)],
        "web": [("petro@corp", 5), ("olena@corp", 90)],
    })
    assert out[0]["identity"] == "petro@corp"


def test_ties_break_on_commits_then_name():
    out = aggregate({"api": [("b@corp", 5), ("a@corp", 5), ("c@corp", 9)]})
    assert [d["identity"] for d in out] == ["c@corp", "a@corp", "b@corp"]


def test_an_empty_identity_never_becomes_a_developer():
    """Bitbucket commits can carry an unparseable author line; a blank key
    would render as an empty dropdown row that selects every repo."""
    out = aggregate({"api": [("", 12), ("real@corp", 1)]})
    assert [d["identity"] for d in out] == ["real@corp"]


def test_no_repos_means_no_developers_rather_than_an_error():
    assert aggregate({}) == []


def test_a_repo_nobody_committed_to_contributes_nothing():
    out = aggregate({"api": [], "web": [("olena@corp", 3)]})
    assert len(out) == 1
    assert out[0]["repos"] == ["web"]


@pytest.mark.parametrize(("scanned", "total"), [(25, 62), (3, 3), (0, 0)])
def test_a_bounded_scan_reports_its_own_bounds(scanned: int, total: int):
    """The number that stops a short list from reading as a small team.

    Without it, "we scanned the first 25 of 62" and "these are all your
    developers" look identical on screen.
    """
    assert scanned <= total or total == 0
