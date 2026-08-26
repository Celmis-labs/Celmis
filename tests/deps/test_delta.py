"""What changed since the last audit — the question every run refused to answer.

Each audit was a complete picture of now, and a stack of independent snapshots
technically contains "what appeared since Friday" while giving it to nobody.
That gap has a name in the regulation the evidence pack exists for:
post-market monitoring means continuity, not a series of unrelated states.

The tests below are mostly about what counts as the SAME finding twice, which
is the only interesting decision here. Get it wrong in one direction and a
patch bump reports every advisory as new; get it wrong in the other and a
genuinely new vulnerability hides behind a package that was already listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.deps.delta import compute_delta


@dataclass
class _Row:
    repo_slug: str
    package: str
    ecosystem: str = "PyPI"
    current_version: str = "1.0.0"
    severity: str = "high"
    vulns: list = field(default_factory=list)


def _v(ident: str, **kw):
    return {"id": ident, "severity": kw.get("severity", "high"),
            "summary": kw.get("summary", ""), "fixed_in": kw.get("fixed_in")}


# ─── the comparison ──────────────────────────────────────────────────


def test_a_new_advisory_is_reported_as_appeared():
    before = [_Row("acme/api", "requests", vulns=[_v("GHSA-old")])]
    now = [_Row("acme/api", "requests",
                vulns=[_v("GHSA-old"), _v("GHSA-new")])]

    d = compute_delta(now, before, previous_run_id="run-1")
    assert [f["id"] for f in d.appeared] == ["GHSA-new"]
    assert d.resolved == []
    assert d.unchanged == 1


def test_a_disappearing_advisory_is_reported_as_resolved():
    before = [_Row("acme/api", "requests", vulns=[_v("GHSA-a"), _v("GHSA-b")])]
    now = [_Row("acme/api", "requests", vulns=[_v("GHSA-a")])]

    d = compute_delta(now, before, previous_run_id="run-1")
    assert [f["id"] for f in d.resolved] == ["GHSA-b"]
    assert d.appeared == []


def test_a_patch_bump_is_not_a_new_finding():
    """Version is deliberately absent from the identity. A dependency moved
    from 2.20.0 to 2.20.1 while still carrying the same advisory has not
    produced a new problem, and reporting it as one is how a weekly digest
    teaches people to ignore it."""
    before = [_Row("acme/api", "requests", current_version="2.20.0",
                   vulns=[_v("GHSA-a")])]
    now = [_Row("acme/api", "requests", current_version="2.20.1",
                vulns=[_v("GHSA-a")])]

    d = compute_delta(now, before, previous_run_id="run-1")
    assert d.appeared == [] and d.resolved == []
    assert d.unchanged == 1


def test_the_same_advisory_in_two_repos_is_two_findings():
    """One repository being fixed while another is not is exactly the movement
    worth reporting."""
    before = [_Row("acme/api", "requests", vulns=[_v("GHSA-a")]),
              _Row("acme/web", "requests", vulns=[_v("GHSA-a")])]
    now = [_Row("acme/web", "requests", vulns=[_v("GHSA-a")])]

    d = compute_delta(now, before, previous_run_id="run-1")
    assert [f["repo"] for f in d.resolved] == ["acme/api"]
    assert d.unchanged == 1


def test_one_advisory_reported_twice_in_a_repo_is_one_finding():
    """The same advisory can arrive from two subprojects of one repository,
    and to a reader it is one thing."""
    now = [_Row("acme/api", "requests", vulns=[_v("GHSA-a")]),
           _Row("acme/api", "requests", vulns=[_v("GHSA-a")])]

    d = compute_delta(now, [], previous_run_id="run-1")
    assert len(d.appeared) == 1


def test_a_finding_with_no_identifier_is_skipped():
    """Nothing can be compared across runs by an empty id, and inventing a key
    would make it appear and resolve on alternate days."""
    now = [_Row("acme/api", "x", vulns=[{"severity": "high"}])]
    d = compute_delta(now, [], previous_run_id="run-1")
    assert d.appeared == []


# ─── the first run is not "no change" ────────────────────────────────


def test_the_first_run_says_so_rather_than_reporting_everything_as_new():
    """Otherwise the one day with no baseline is the day the reader is
    drowned — and after that they skim."""
    now = [_Row("acme/api", "requests", vulns=[_v("GHSA-a")])]
    d = compute_delta(now, [], previous_run_id=None)

    assert d.is_first_run
    assert d.appeared == []
    assert "First audit" in d.headline()


def test_quiet_and_unstarted_do_not_read_the_same():
    """One means the monitoring has not begun; the other that it is working
    and there is nothing to say."""
    rows = [_Row("acme/api", "requests", vulns=[_v("GHSA-a")])]
    first = compute_delta(rows, [], previous_run_id=None)
    quiet = compute_delta(rows, rows, previous_run_id="run-1")

    assert first.headline() != quiet.headline()
    assert "No change" in quiet.headline()
    assert quiet.is_first_run is False


# ─── the digest ──────────────────────────────────────────────────────


def test_the_headline_counts_both_directions():
    before = [_Row("acme/api", "a", vulns=[_v("GHSA-gone")])]
    now = [_Row("acme/api", "b", vulns=[_v("GHSA-new")])]

    line = compute_delta(now, before, previous_run_id="run-1").headline()
    assert "1 new" in line and "1 resolved" in line


def test_the_worst_appears_first():
    """"3 new" means nothing until you know whether one of them is critical,
    and a digest is skimmed."""
    now = [
        _Row("acme/api", "low-pkg", vulns=[_v("GHSA-l", severity="low")]),
        _Row("acme/api", "crit-pkg", vulns=[_v("GHSA-c", severity="critical")]),
        _Row("acme/api", "med-pkg", vulns=[_v("GHSA-m", severity="medium")]),
    ]
    d = compute_delta(now, [_Row("acme/api", "other")], previous_run_id="run-1")
    assert [f["id"] for f in d.appeared] == ["GHSA-c", "GHSA-m", "GHSA-l"]


def test_the_payload_carries_counts_the_ui_can_render_without_counting():
    d = compute_delta([_Row("acme/api", "x", vulns=[_v("V-1")])], [],
                      previous_run_id="run-0")
    payload = d.as_dict()
    assert payload["counts"] == {
        "appeared": 1, "resolved": 0, "out_of_scope": 0, "unchanged": 0,
    }
    assert payload["first_run"] is False
    assert payload["previous_run_id"] == "run-0"
