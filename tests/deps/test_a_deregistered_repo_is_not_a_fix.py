"""Removing a repository from the audit is not the same as fixing it.

THE DEFECT. `compute_delta` put everything that vanished between two runs into
one bucket called `resolved`, and `headline()` rendered it as "N resolved".

A real run reported **"493 resolved"**. The true number of vulnerabilities
anybody had fixed was **zero**: all 493 belonged to nine repositories that had
simply been de-registered from the workspace. The dataclass comment knew the
ambiguity was there ("fixed, removed, or the package is gone"); the sentence
shown to a human did not.

For a post-market-monitoring artefact that is not a rounding error — it is the
opposite of the truth, in the direction that flatters.

WHY THE FIX NEEDED A NEW INPUT. Scope cannot be inferred from findings. A
repository that is still audited and now perfectly clean produces no findings
at all, so "which repos appear in this run's results" would file its genuine
fixes under "no longer audited" — the same lie with the sign flipped. That
inference was written, and an existing test caught it. The auditor now records
the slugs it scanned, and a caller that cannot supply them gets the old
undifferentiated answer rather than a confident wrong split.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.deps.delta import compute_delta


@dataclass
class Row:
    repo_slug: str
    package: str
    ecosystem: str = "PyPI"
    current_version: str = "1.0.0"
    severity: str = "high"
    vulns: list[dict[str, Any]] = field(default_factory=list)


def finding(repo: str, pkg: str, vid: str) -> Row:
    return Row(repo_slug=repo, package=pkg,
               vulns=[{"id": vid, "severity": "high", "summary": "x"}])


PREV = [finding("acme/api", "requests", "CVE-1"),
        finding("acme/gone", "urllib3", "CVE-2"),
        finding("acme/gone", "jinja2", "CVE-3")]


def test_a_repo_that_left_the_scan_is_not_reported_as_fixed():
    """`acme/api` was really fixed; `acme/gone` was de-registered."""
    d = compute_delta([], PREV, previous_run_id="r1",
                      current_repos={"acme/api"})

    assert [f["repo"] for f in d.resolved] == ["acme/api"]
    assert sorted(f["repo"] for f in d.out_of_scope) == ["acme/gone", "acme/gone"]


def test_the_headline_says_which_is_which():
    d = compute_delta([], PREV, previous_run_id="r1",
                      current_repos={"acme/api"})

    line = d.headline()
    assert "1 resolved" in line
    assert "2 no longer audited" in line
    assert "1 repositories left the scan" in line


def test_a_repo_cleaned_completely_still_counts_as_fixed():
    """The failure mode of the first attempt at this fix: a repository with
    zero findings left is IN scope and its fixes are real."""
    d = compute_delta([], PREV, previous_run_id="r1",
                      current_repos={"acme/api", "acme/gone"})

    assert len(d.resolved) == 3
    assert d.out_of_scope == []
    assert "no longer audited" not in d.headline()


def test_without_a_known_scope_nothing_is_split():
    """Guessing is worse than not splitting, so a caller that cannot say gets
    the old answer — undifferentiated, but not wrong."""
    d = compute_delta([], PREV, previous_run_id="r1")

    assert len(d.resolved) == 3
    assert d.out_of_scope == []


def test_the_counts_carry_the_new_bucket():
    payload = compute_delta([], PREV, previous_run_id="r1",
                            current_repos={"acme/api"}).as_dict()

    assert payload["counts"]["resolved"] == 1
    assert payload["counts"]["out_of_scope"] == 2


def test_a_quiet_run_still_reads_as_quiet():
    same = [finding("acme/api", "requests", "CVE-1")]
    d = compute_delta(same, same, previous_run_id="r1",
                      current_repos={"acme/api"})

    assert d.headline() == "No change since the previous audit."


def test_the_auditor_records_the_slugs_the_delta_needs():
    """The split is only as good as its input, and that input is written on
    the other side of the system."""
    import ast
    import inspect

    from src.deps import auditor

    tree = ast.parse(inspect.getsource(auditor))
    literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "repos_scanned_slugs" in literals
