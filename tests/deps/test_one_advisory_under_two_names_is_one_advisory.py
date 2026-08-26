"""The same advisory does not appear as both resolved and new.

MEASURED. A repository at commit 9c25b4d9 was audited twice with nothing
changed between the runs. 74 advisories before, 74 after. The delta claimed
**14 resolved — five of them critical — and 14 appeared.** They were the same
fourteen:

    GO-2026-5005       ↔  GHSA-jppx-rxg9-jmrx     (both CVE-2026-39833)
    GO-2023-1571       ↔  GHSA-vvpx-j8f3-3w6h     (both CVE-2022-41723)
    RUSTSEC-2023-0072  ↔  GHSA-xphf-cx8h-7q9g

`merge_vuln` is first-writer-wins on `id`, so which source arrives first
decides the primary name — the two runs saw `{osv 44, osv-scanner 30}` and
`{osv 74, osv-scanner 0}`. `_flatten` then keyed on `id or cve` and never
looked at `aliases`, even though every one of these records carries its
counterpart there and thirteen of fourteen share an identical `cve`.

"Five criticals resolved" when nobody touched the repository is the same lie
the out-of-scope bucket was written to kill, moved from the repository axis to
the identifier axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.deps.delta import _canonical_id, compute_delta


@dataclass
class Row:
    repo_slug: str = "acme/worker"
    package: str = "golang.org/x/net"
    ecosystem: str = "Go"
    current_version: str = "0.0.0-2021"
    severity: str = "high"
    vulns: list[dict[str, Any]] = field(default_factory=list)


def test_the_cve_wins_because_every_source_agrees_on_it():
    a = {"id": "GO-2026-5005", "aliases": ["CVE-2026-39833", "GHSA-jppx-rxg9-jmrx"]}
    b = {"id": "GHSA-jppx-rxg9-jmrx", "aliases": ["CVE-2026-39833", "GO-2026-5005"]}

    assert _canonical_id(a) == _canonical_id(b) == "CVE-2026-39833"


def test_a_cve_in_aliases_counts_even_without_the_field():
    """OSV records carry the CVE as an alias rather than in `cve`."""
    assert _canonical_id({"id": "GO-1", "aliases": ["CVE-2020-1"]}) == "CVE-2020-1"


def test_without_a_cve_the_choice_is_arbitrary_but_stable():
    """RUSTSEC and GHSA pairs often have no CVE at all. Which one wins does
    not matter; that BOTH runs pick the same one does."""
    a = {"id": "RUSTSEC-2023-0072", "aliases": ["GHSA-xphf-cx8h-7q9g"]}
    b = {"id": "GHSA-xphf-cx8h-7q9g", "aliases": ["RUSTSEC-2023-0072"]}

    assert _canonical_id(a) == _canonical_id(b)


def test_an_advisory_with_no_aliases_keeps_its_own_id():
    assert _canonical_id({"id": "GHSA-lonely", "aliases": []}) == "GHSA-lonely"


def test_an_unidentifiable_advisory_is_skipped():
    assert _canonical_id({"id": "", "aliases": []}) == ""


# ─── the delta itself ────────────────────────────────────────────────


def test_an_unchanged_repo_reports_no_change_across_a_source_switch():
    """The exact production shape: same commit, same advisories, different
    source ordering between the two runs."""
    before = [Row(vulns=[
        {"id": "GO-2026-5005", "aliases": ["CVE-2026-39833"], "severity": "critical"},
        {"id": "GO-2023-1571", "aliases": ["CVE-2022-41723"], "severity": "high"},
    ])]
    after = [Row(vulns=[
        {"id": "GHSA-jppx-rxg9-jmrx", "aliases": ["CVE-2026-39833"], "severity": "critical"},
        {"id": "GHSA-vvpx-j8f3-3w6h", "aliases": ["CVE-2022-41723"], "severity": "high"},
    ])]

    d = compute_delta(after, before, previous_run_id="r1",
                      current_repos={"acme/worker"})

    assert d.appeared == []
    assert d.resolved == []
    assert d.unchanged == 2
    assert d.headline() == "No change since the previous audit."


def test_a_genuinely_new_advisory_is_still_reported():
    """The check must not become blind by becoming lenient."""
    before = [Row(vulns=[{"id": "GO-1", "aliases": ["CVE-2020-1"]}])]
    after = [Row(vulns=[
        {"id": "GO-1", "aliases": ["CVE-2020-1"]},
        {"id": "GHSA-new", "aliases": ["CVE-2026-9"]},
    ])]

    d = compute_delta(after, before, previous_run_id="r1",
                      current_repos={"acme/worker"})

    assert [f["id"] for f in d.appeared] == ["CVE-2026-9"]
    assert d.resolved == []


def test_a_genuinely_fixed_advisory_is_still_reported():
    before = [Row(vulns=[
        {"id": "GO-1", "aliases": ["CVE-2020-1"]},
        {"id": "GO-2", "aliases": ["CVE-2020-2"]},
    ])]
    after = [Row(vulns=[{"id": "GHSA-x", "aliases": ["CVE-2020-1"]}])]

    d = compute_delta(after, before, previous_run_id="r1",
                      current_repos={"acme/worker"})

    assert [f["id"] for f in d.resolved] == ["CVE-2020-2"]
    assert d.appeared == []
