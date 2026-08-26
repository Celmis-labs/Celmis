"""Every advisory OSV confirms for a version reaches the report.

THE DEFECT. `_osv_batch` fetched the full record for each advisory — one HTTP
GET per id — and bounded that work with `for vid in ids[:10]`. The cap read as
a request budget and behaved as a display limit: advisories past the tenth were
not fetched AND not reported, and nothing recorded that anything was missing.

Measured on a real audit: axios 0.21.1 has 24 advisories in OSV, every one
genuinely affecting that version. Ten were reported. The cut fell by
lexicographic id — every dropped id sorted after `GHSA-fvcv…` — so it landed
where the alphabet happened to land, not where severity did, and it took SEVEN
HIGH advisories with it, including CVE-2025-27152 (SSRF with credential
leakage). The shipped CycloneDX SBOM carried the same ten, so a customer
reading that inventory under-counted axios by fourteen and had no way to know.

THE FIX IS NOT A BIGGER NUMBER. Any budget has a tail. An advisory past the
budget is emitted anyway, with its id, its OSV link and `detail_unavailable`.
Detail degrades; coverage does not.
"""

from __future__ import annotations

from src.deps.registries import MAX_ADVISORY_DETAILS, _stub
from src.deps.severity import worst_severity


def test_a_stub_carries_the_identity_that_matters():
    """Enough for a reader to go and look it up themselves."""
    v = _stub("GHSA-3g43-hqhq-hqhq")

    assert v["id"] == "GHSA-3g43-hqhq-hqhq"
    assert v["url"] == "https://osv.dev/vulnerability/GHSA-3g43-hqhq-hqhq"
    assert v["detail_unavailable"] is True


def test_a_stub_admits_it_does_not_know_the_severity():
    """A guessed severity on an unread advisory is worse than an admitted gap.
    `_summarise` grades what it read; `_stub` read nothing."""
    assert _stub("GHSA-xxxx-xxxx-xxxx")["severity"] == "unknown"


def test_a_cve_id_still_reads_as_a_cve():
    assert _stub("CVE-2025-27152")["cve"] == "CVE-2025-27152"
    assert _stub("GHSA-3g43-hqhq-hqhq")["cve"] is None


def test_a_stub_never_inflates_a_packages_severity():
    """It must not turn a medium package critical by being unreadable."""
    real = {"severity": "medium"}

    assert worst_severity([real, _stub("GHSA-a")]) == "medium"


def test_a_stub_never_deflates_a_packages_severity():
    real = {"severity": "critical"}

    assert worst_severity([_stub("GHSA-a"), real]) == "critical"


def test_the_budget_is_large_enough_for_the_package_that_exposed_this():
    """axios 0.21.1 carries 24. A budget under that reintroduces the defect on
    the exact case that found it."""
    assert MAX_ADVISORY_DETAILS >= 24


def test_the_budget_is_a_named_constant_not_a_literal():
    """It was `ids[:10]` inline, where nothing could see it, document it or
    test it."""
    import inspect

    from src.deps import registries

    src = inspect.getsource(registries)
    body = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "ids[:10]" not in body
    assert "MAX_ADVISORY_DETAILS" in body


def test_a_package_whose_advisories_were_all_unreadable_is_not_clean():
    """`worst_severity` grades an unreadable advisory as "none", which is the
    correct answer for the grader and the wrong answer for the report: the
    package has advisories, nobody read them. The auditor carries a guard that
    turns this exact shape into "unknown" — the same distinction the drift
    histogram already makes, where "unknown" is deliberately not "up to date"."""
    vlist = [_stub("GHSA-a"), _stub("GHSA-b")]

    assert worst_severity(vlist) == "none"
    assert all(v.get("detail_unavailable") for v in vlist)


def test_a_partly_read_package_is_graded_on_what_was_read():
    """The guard must not fire when anything was actually read."""
    vlist = [{"severity": "high"}, _stub("GHSA-b")]

    assert worst_severity(vlist) == "high"
    assert not all(v.get("detail_unavailable") for v in vlist)
