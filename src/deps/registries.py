"""Registry + vulnerability lookups for the dependency audit.

Latest versions come from the canonical public registries; vulnerabilities
come from OSV.dev (Google's open vulnerability DB — one API covering npm,
PyPI, Go, crates.io and more, including the `fixed` version per advisory).

All calls are best-effort with short timeouts: a registry hiccup degrades one
package's row to "latest unknown", never fails the whole audit.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from src.deps.severity import (
    normalize_severity,
    severity_from_vector,
    worst_severity,
)
from src.http import build_client

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)  # querybatch of 100 can be slow
_OSV_BATCH = 100


# ─── Latest versions ─────────────────────────────────────────────────


def fetch_latest(client: httpx.Client, ecosystem: str, package: str) -> str | None:
    try:
        if ecosystem == "npm":
            r = client.get(f"https://registry.npmjs.org/{package}/latest")
            return str(r.json().get("version")) if r.status_code == 200 else None
        if ecosystem == "PyPI":
            r = client.get(f"https://pypi.org/pypi/{package}/json")
            return str(r.json()["info"]["version"]) if r.status_code == 200 else None
        if ecosystem == "Go":
            r = client.get(f"https://proxy.golang.org/{package.lower()}/@latest")
            v = r.json().get("Version", "") if r.status_code == 200 else ""
            return v.lstrip("v") or None
        if ecosystem == "crates.io":
            r = client.get(f"https://crates.io/api/v1/crates/{package}",
                           headers={"User-Agent": "celmis-dep-audit"})
            if r.status_code == 200:
                crate = r.json().get("crate") or {}
                return str(crate.get("max_stable_version") or crate.get("max_version") or "") or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("latest_lookup_failed eco=%s pkg=%s err=%s", ecosystem, package, exc)
    return None


# ─── OSV vulnerabilities ─────────────────────────────────────────────


def fetch_vulns_batch(
    client: httpx.Client, deps: list[tuple[str, str, str]],
    *, deadline: float | None = None,
) -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], int]:
    """deps: [(ecosystem, package, version)] → ({(eco, pkg, ver): [vuln, …]},
    failed_batches).

    Keyed by the FULL triple: two repos can pin different versions of the
    same package, and a vuln fixed in one version must not paint the other.
    failed_batches lets the caller distinguish "no vulns" from "OSV down".

    Two-phase: querybatch returns only ids, then each unique id is fetched
    once for severity/summary/fixed data (ids repeat across packages rarely
    enough that per-id caching inside the run is sufficient).
    """
    out: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    id_cache: dict[str, dict[str, Any] | None] = {}
    failed_batches = 0

    for i in range(0, len(deps), _OSV_BATCH):
        # THE WALL CLOCK, checked between batches.
        #
        # This function is one POST per hundred packages plus a GET per
        # advisory, each bounded at 30s and NOTHING bounding the sequence. Its
        # only caller gives it 120 seconds and abandons the thread when they
        # are up, so a lockfile large enough to need five batches could not
        # finish inside its own timebox however healthy OSV was — and the
        # review reported the scan as blind rather than as partial. Blind and
        # partial are different answers: a partial scan found real advisories,
        # and dropping them because the tail did not fit is losing the work
        # that had already been paid for.
        #
        # Between batches and not mid-batch: a querybatch answer must be
        # paired positionally with the chunk that asked for it, so abandoning
        # one halfway is how a package gets credited with another package's
        # vulnerabilities.
        #
        # The unreached packages count as failed batches — the signal that
        # already means "this scan is incomplete", so no caller needs a second
        # notion of partial and none can read the shortfall as "no vulns".
        if deadline is not None and time.monotonic() >= deadline:
            remaining = len(deps) - i
            failed_batches += (remaining + _OSV_BATCH - 1) // _OSV_BATCH
            logger.warning(
                "osv_deadline_reached scanned=%d of=%d unscanned_batches=%d",
                i, len(deps), (remaining + _OSV_BATCH - 1) // _OSV_BATCH)
            break
        chunk = deps[i:i + _OSV_BATCH]
        queries = [
            {"package": {"ecosystem": eco, "name": pkg}, "version": ver}
            for eco, pkg, ver in chunk
        ]
        try:
            r = client.post("https://api.osv.dev/v1/querybatch", json={"queries": queries})
            if r.status_code != 200:
                logger.warning("osv_batch_http_%s", r.status_code)
                failed_batches += 1
                continue
            results = r.json().get("results", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("osv_batch_failed err=%s", exc)
            failed_batches += 1
            continue
        if len(results) != len(chunk):
            # Misaligned answer: pairing it positionally would credit one
            # package with another package's vulns. Count it as a failed
            # batch so the caller can tell this apart from "no vulns".
            logger.warning("osv_batch_length_mismatch sent=%d got=%d",
                           len(chunk), len(results))
            failed_batches += 1
            continue
        for (eco, pkg, ver), res in zip(chunk, results, strict=True):
            ids = [v.get("id") for v in (res or {}).get("vulns", []) if v.get("id")]
            if not ids:
                continue
            vulns = []
            for vid in ids:
                if len(vulns) < MAX_ADVISORY_DETAILS:
                    detail = _fetch_vuln_detail(client, vid, id_cache)
                    if detail:
                        vulns.append(_summarise(detail, eco, pkg, ver))
                        continue
                # Past the detail budget, or the detail fetch failed: the
                # advisory is still REAL and still applies to this version —
                # OSV said so in the batch answer. Carry it with what is known
                # for certain rather than dropping it.
                vulns.append(_stub(vid))
            if vulns:
                out[(eco, pkg, ver)] = vulns
    return out, failed_batches


#: How many advisories per package get their full record fetched. Each one is
#: its own HTTP GET to /v1/vulns/{id}, so this is a request budget, not a
#: display limit — and it used to be spelled `ids[:10]` inline, where it
#: behaved as both.
#:
#: What that cost, measured on a real audit: axios 0.21.1 has 24 advisories in
#: OSV, all genuinely affecting that version. Ten were reported. The cut was by
#: lexicographic id — every dropped id sorted after `GHSA-fvcv…` — so it fell
#: where the alphabet happened to fall, not where severity did, and it took
#: SEVEN HIGH advisories with it, including CVE-2025-27152 (SSRF with
#: credential leakage) and CVE-2023-45857 (the XSRF token leak). Nothing
#: recorded that anything had been dropped, and the shipped CycloneDX SBOM
#: carried the same ten. A customer reading that inventory under-counted axios
#: by fourteen advisories and had no way to know.
#:
#: The number is raised, but raising it is not the fix — any number has a tail.
#: The fix is below: an advisory past the budget is still emitted, with its id,
#: its OSV link and an honest `detail_unavailable` marker. Nothing is silently
#: dropped, ever. Detail degrades; coverage does not.
MAX_ADVISORY_DETAILS = 50


def _stub(vuln_id: str) -> dict[str, Any]:
    """An advisory OSV confirmed for this version, whose full record was not
    fetched. Severity is unknown and says so — it is not graded "medium",
    because a guessed severity on an unread advisory is worse than an admitted
    gap. The caller's aggregate treats `unknown` as unknown."""
    return {
        "id": vuln_id,
        "cve": vuln_id if vuln_id.startswith("CVE-") else None,
        "severity": "unknown",
        "summary": "",
        "fixed_in": None,
        "url": f"https://osv.dev/vulnerability/{vuln_id}",
        "source": "osv",
        "aliases": [],
        "detail_unavailable": True,
    }


def _fetch_vuln_detail(
    client: httpx.Client, vuln_id: str, cache: dict,
) -> dict[str, Any] | None:
    if vuln_id in cache:
        return cache[vuln_id]
    try:
        r = client.get(f"https://api.osv.dev/v1/vulns/{vuln_id}")
        cache[vuln_id] = r.json() if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        cache[vuln_id] = None
    return cache[vuln_id]


def _summarise(detail: dict[str, Any], ecosystem: str, package: str,
               version: str = "") -> dict[str, Any]:
    aliases = detail.get("aliases") or []
    cve = next((a for a in aliases if a.startswith("CVE-")), None)
    return {
        "id": detail.get("id"),
        "cve": cve,
        "severity": osv_severity(detail),
        "summary": (detail.get("summary") or detail.get("details") or "")[:300],
        "fixed_in": osv_fixed_version(detail, ecosystem, package, version),
        "url": f"https://osv.dev/vulnerability/{detail.get('id')}",
        # Provenance: OSV is the fallback path. Native auditors stamp their own
        # tool name here, and the UI shows it so a human can weigh the finding.
        "source": "osv",
        # Kept so this advisory still dedupes against a tool that knows it
        # under a different primary id and shares no CVE.
        "aliases": [str(a) for a in aliases],
    }


# Both helpers below take a raw OSV record, so every source that speaks OSV —
# the API here, and the osv-scanner binary — grades one advisory identically.
# Two sources disagreeing about the same CVE is a bug the UI cannot explain.


def osv_severity(detail: dict[str, Any], *, default: str = "medium") -> str:
    """Normalise OSV severity: prefer database_specific severity, else the
    CVSS vector.

    OSV carries CVSS as a *vector string*, never a bare score — computing the
    base score (severity.cvss_base_score) is the difference between a real
    critical/high split and every CVSS-only advisory reading "medium".
    """
    db = detail.get("database_specific") or {}
    sev = normalize_severity(db.get("severity"), default="")
    if sev:
        return sev
    for entry in detail.get("severity") or []:
        graded = severity_from_vector(str(entry.get("score", "")), default="")
        if graded:
            return graded
    return default if detail.get("id") else "none"


def osv_fixed_version(detail: dict[str, Any], ecosystem: str, package: str,
                      current: str = "") -> str | None:
    """The version to upgrade to, from OSV's affected ranges.

    `current` is what makes the answer usable: an advisory patched on several
    branches lists a `fixed` per branch (log4j-core: 2.3.1, 2.12.2, 2.15.0),
    and the smallest of those is a *downgrade* for anyone on 2.14.1. The
    upgrade target is the lowest fix above the installed version.

    Names and ecosystems are matched loosely on purpose: registries disagree
    about case, and OSV tags distro advisories with the release ("Debian:12")
    while the scan reports the same package under the bare ecosystem.
    """
    base = str(ecosystem or "").split(":", 1)[0].lower()
    name = str(package or "").lower()
    fixes: list[str] = []
    for aff in detail.get("affected") or []:
        pkg = aff.get("package") or {}
        if str(pkg.get("name") or "").lower() != name:
            continue
        if base and str(pkg.get("ecosystem") or "").split(":", 1)[0].lower() != base:
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if "fixed" in ev:
                    fixes.append(str(ev["fixed"]))
    if not fixes:
        return None
    fixes.sort(key=version_key)
    if current:
        above = [f for f in fixes if version_key(f) > version_key(current)]
        if above:
            return above[0]
    return fixes[0]


# ─── Version comparison ──────────────────────────────────────────────


def version_key(v: str) -> tuple:
    """Sortable key for common version strings (numeric parts, then suffix)."""
    v = v.lstrip("v")
    main = re.split(r"[-+]", v, maxsplit=1)[0]
    parts = []
    for p in main.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return (*parts[:4], v)


def outdated_level(current: str, latest: str | None) -> str:
    """unknown | none | patch | minor | major.

    No latest version is "unknown", not "none". They render identically to a
    reader — the table and the exported document both show "none" as up to
    date — and they mean opposite things: one is a package at the newest
    release, the other is a package nobody could look up. `idna 2.10` printed
    as `-> ? (PyPI, none)` while being two majors behind.

    The aggregate histogram already made this distinction and carried a
    comment saying so; the row-level field it was built from did not.
    """
    if not latest:
        return "unknown"
    try:
        cur_key, latest_key = version_key(current), version_key(latest)
        if latest_key[:4] <= cur_key[:4]:
            return "none"
        if latest_key[0] > cur_key[0]:
            return "major"
        if latest_key[1] > cur_key[1]:
            return "minor"
        return "patch"
    except Exception:  # noqa: BLE001
        return "none"


def make_client() -> httpx.Client:
    # Guarded egress (src/http.py). Every registry this client reaches —
    # pypi.org, registry.npmjs.org, crates.io, proxy.golang.org, api.osv.dev —
    # is on the shipped public allowlist, so no host exception is needed;
    # follow_redirects stays True because the raw client always had it.
    return build_client(timeout=_TIMEOUT, follow_redirects=True)


__all__ = [
    "make_client",
    "fetch_latest",
    "fetch_vulns_batch",
    "osv_fixed_version",
    "osv_severity",
    "outdated_level",
    "worst_severity",
    "version_key",
]
