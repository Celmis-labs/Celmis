"""osv-scanner — one auditor for every ecosystem OSV knows.

The native auditors (:mod:`src.deps.native`) are the best answer where they
exist, and they exist for four languages. Everything else — Java (Maven,
Gradle), .NET (NuGet), Dart (Pub), Elixir (Hex), Haskell, R (CRAN), Swift,
C/C++ (Conan), and the Alpine/Debian packages a container is built from —
had no auditor at all, which in this audit's vocabulary means `not_checked`:
an honest unknown, but still an unknown.

`osv-scanner` (a single static Go binary from Google) closes that gap in one
pass: it walks the checkout, extracts every manifest and lock file it
recognises, and matches the resolved packages against OSV. What comes back
are plain OSV records — the same shape :mod:`src.deps.registries` already
parses — so findings converge on `native.NativeFinding` and dedupe against
pip-audit / npm audit / the OSV API by advisory id, CVE and alias.

Two facts about the binary decide the whole design:

* **Exit 1 means "found vulnerabilities", not "failed".** 127 is a real
  error and 128 is "no package sources found"; only those two are failures.
  Reading exit 1 as failure silently drops every finding.
* **It may simply not be installed.** Then the audit must lose this engine
  and nothing else, so every failure path returns an empty result plus a
  machine-readable reason code instead of raising.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from src.deps.native import (
    TOOL_MISSING,
    TOOL_OS_ERROR,
    TOOL_TIMEOUT,
    EcosystemCheck,
    NativeFinding,
    NativeResult,
    _cve,
    _loads,
    run_tool,
)
from src.deps.registries import osv_fixed_version, osv_severity
from src.deps.severity import SEV_ORDER, severity_from_vector

logger = logging.getLogger(__name__)

BINARY = "osv-scanner"
SOURCE = "osv-scanner"

# One invocation covers the WHOLE repo, unlike the native auditors that get a
# timeout each per subproject — and Maven/Gradle manifests make it fetch
# remote POMs to resolve transitives, which is the slow part.
DEFAULT_TIMEOUT = 600

# Machine-readable "why there is no result". The first three are whatever
# `run_tool` stamped, bound here rather than respelled so the two cannot drift.
REASON_MISSING = TOOL_MISSING
REASON_TIMEOUT = TOOL_TIMEOUT
REASON_OS_ERROR = TOOL_OS_ERROR
REASON_FAILED = "failed"
REASON_BAD_JSON = "bad_json"
REASON_NO_SOURCES = "no_sources"

_NO_SOURCES = "osv-scanner recognised no manifest or lock file here"

# The binary ran and did its job in both cases; the rest are real failures.
_EXIT_OK = 0
_EXIT_VULNS_FOUND = 1
_EXIT_NO_SOURCES = 128

# v2 command line. Every flag is here for a reason:
#   --allow-no-lockfiles  a repo with nothing to scan exits 128 and prints no
#                         JSON otherwise; "nothing to scan" is a result
#   --all-packages        clean packages are omitted otherwise, and then we
#                         cannot tell "scanned and clean" from "never looked"
#                         — the one distinction this audit is built around
#   --all-vulns           the default hides advisories osv-scanner judges
#                         unimportant; under-reporting is the output this
#                         audit must never produce
#   --verbosity error     the progress log is per-file and would bury the
#                         actual error message on stderr
_V2_ARGV = ["scan", "source", "--format", "json", "--recursive",
            "--allow-no-lockfiles", "--all-packages", "--all-vulns",
            "--verbosity", "error"]
# v1 had no `scan` subcommand and knows none of the flags above. Kept as a
# fallback so a machine with an older binary degrades to fewer features
# instead of to nothing.
_V1_ARGV = ["--format", "json", "--recursive"]

# Lock files name their "not shipped to production" bucket differently.
_DEV_GROUPS = {"dev", "devdependencies", "development", "require-dev",
               "test", "testing"}


def canonical_ecosystem(raw: str) -> str:
    """OSV's ecosystem string → the name the audit groups rows by.

    osv-scanner speaks OSV's vocabulary, which is already the one used here
    ("npm", "PyPI", "Go", "crates.io", "Packagist", "RubyGems"), so the only
    real work is dropping the release suffix OSV puts on distro advisories —
    "Debian:12", "Alpine:v3.18", "Red Hat:rhel_aus:8.4::appstream" — which
    would otherwise scatter one package across a row per release. Anything
    unrecognised keeps its own name: an ecosystem nobody here has heard of
    must still reach the report rather than be dropped.
    """
    return str(raw or "").split(":", 1)[0].strip()


def scanned_ecosystems(payload: object) -> dict[str, int]:
    """canonical ecosystem → how many packages osv-scanner actually resolved.

    This is the honest half of the report: a lock file it read and found
    clean has to count as `checked`, otherwise the audit re-checks it through
    the OSV fallback and still calls a scanned ecosystem an unknown.
    """
    out: dict[str, int] = {}
    for _source, entry in _iter_packages(payload):
        eco = canonical_ecosystem((entry.get("package") or {}).get("ecosystem"))
        if eco:
            out[eco] = out.get(eco, 0) + 1
    return out


def parse_osv_scanner(
    payload: object,
    *,
    repo_path: Path | str | None = None,
    direct: dict[str, set[str]] | None = None,
    dev: dict[str, set[str]] | None = None,
) -> list[NativeFinding]:
    """`osv-scanner --format json` output → findings.

    `direct` / `dev` are the manifest scan's package names per ecosystem.
    osv-scanner reports the resolved tree without saying what was declared,
    so without them every transitive package would claim to be direct — and
    for the ecosystems the manifest scanner does not read at all (Maven,
    NuGet, …) the honest answer is "unknown", i.e. not marked transitive.
    """
    out: list[NativeFinding] = []
    for source, entry in _iter_packages(payload):
        pkg = entry.get("package") or {}
        raw_eco = str(pkg.get("ecosystem") or "")
        eco = canonical_ecosystem(raw_eco)
        name = str(pkg.get("name") or "")
        if eco == "PyPI":
            # pip-audit and the manifest scanner both lowercase PyPI names;
            # one package under two spellings would become two rows.
            name = name.lower()
        if not name:
            continue

        direct_names = {n.lower() for n in (direct or {}).get(eco, set())}
        dev_names = {n.lower() for n in (dev or {}).get(eco, set())}
        groups = {str(g).lower() for g in (entry.get("dependency_groups") or [])}
        is_dev = bool(groups & _DEV_GROUPS) or name.lower() in dev_names
        transitive = bool(direct_names) and name.lower() not in direct_names
        subproject = _subproject(source.get("path"), repo_path)
        # osv-scanner collapses aliased advisories into `groups` and states the
        # highest CVSS score per group — the only severity signal available for
        # a record that carries neither a severity word nor a v3 vector.
        group_max: dict[str, str] = {}
        for group in entry.get("groups") or []:
            if not isinstance(group, dict):
                continue
            for vuln_id in group.get("ids") or []:
                group_max[str(vuln_id)] = str(group.get("max_severity") or "")

        produced: list[NativeFinding] = []
        for record in entry.get("vulnerabilities") or []:
            if not isinstance(record, dict):
                continue
            vuln_id = str(record.get("id") or "")
            aliases = tuple(str(a) for a in (record.get("aliases") or []) if a)
            produced.append(NativeFinding(
                ecosystem=eco,
                package=name,
                version=str(pkg.get("version") or ""),
                severity=_grade(record, group_max.get(vuln_id, "")),
                vuln_id=vuln_id,
                cve=_cve(aliases, vuln_id, record.get("related")),
                # The raw ecosystem, not the canonical one: OSV's `affected`
                # entries carry the release suffix too.
                fixed_in=osv_fixed_version(record, raw_eco, name,
                                           str(pkg.get("version") or "")),
                summary=str(record.get("summary") or record.get("details") or ""),
                url=f"https://osv.dev/vulnerability/{vuln_id}" if vuln_id else "",
                is_dev=is_dev,
                transitive=transitive,
                source=SOURCE,
                subproject=subproject,
                # Aliases are what let this finding dedupe against a source
                # that knows the same advisory under a different primary id
                # (GHSA-… here, PYSEC-… from pip-audit) with no shared CVE.
                aliases=aliases,
                ecosystem_raw=raw_eco if raw_eco != eco else "",
            ))
        # Worst first: a package with thirty advisories is summarised by its
        # first few everywhere downstream, and those should be the ones that
        # matter.
        produced.sort(key=lambda f: -SEV_ORDER.get(f.severity, 0))
        out.extend(produced)
    return out


def audit_repo(
    repo_path: Path,
    *,
    declared: list | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    runner=run_tool,
    on_progress=None,
) -> NativeResult:
    """Scan a clone with osv-scanner. Never raises: an unavailable engine
    comes back as an empty result carrying a reason code.

    `runner` is injectable so the orchestration is testable without the
    binary; `on_progress(tool, subproject)` beats once before the (single,
    possibly minutes-long) invocation, and a caller that raises from it stops
    the scan — the same cooperative-cancellation contract as `native`.
    """
    direct_by_eco: dict[str, set[str]] = {}
    dev_by_eco: dict[str, set[str]] = {}
    for dep in declared or []:
        direct_by_eco.setdefault(dep.ecosystem, set()).add(dep.package.lower())
        if dep.is_dev:
            dev_by_eco.setdefault(dep.ecosystem, set()).add(dep.package.lower())

    if on_progress is not None:
        on_progress(BINARY, "")
    result = runner([BINARY, *_V2_ARGV, str(repo_path)], repo_path, timeout)
    payload = _loads(result.stdout) if result.ok else None
    if result.ok and payload is None and result.code not in (_EXIT_OK, _EXIT_VULNS_FOUND):
        # A v1 binary rejects `scan source` (and the newer flags) with a usage
        # error before printing any JSON — retry the form it does understand.
        result = runner([BINARY, *_V1_ARGV, str(repo_path)], repo_path, timeout)
        payload = _loads(result.stdout) if result.ok else None

    if not result.ok:
        return _degraded(result.reason_code or REASON_FAILED, result.error)
    if payload is None:
        if result.code == _EXIT_NO_SOURCES:
            return _degraded(REASON_NO_SOURCES, _NO_SOURCES)
        code = (REASON_BAD_JSON if result.code in (_EXIT_OK, _EXIT_VULNS_FOUND)
                else REASON_FAILED)
        return _degraded(code, _stderr_tail(result.stderr)
                         or f"osv-scanner exited {result.code} without JSON")

    findings = parse_osv_scanner(payload, repo_path=repo_path,
                                 direct=direct_by_eco, dev=dev_by_eco)
    scanned = scanned_ecosystems(payload)
    if not scanned:
        return _degraded(REASON_NO_SOURCES, _NO_SOURCES)

    hits: dict[str, int] = {}
    for finding in findings:
        hits[finding.ecosystem] = hits.get(finding.ecosystem, 0) + 1
    # One check per ecosystem, no subproject: osv-scanner walks the whole tree
    # in a single pass, so per-directory accounting would be a fiction.
    checks = [EcosystemCheck(eco, SOURCE, "", "checked", "", hits.get(eco, 0))
              for eco in sorted(scanned)]
    logger.info("osv_scanner_done repo=%s ecosystems=%s findings=%d",
                repo_path, sorted(scanned), len(findings))
    return NativeResult(findings=findings, checks=checks)


# ─── Internals ───────────────────────────────────────────────────────


def _iter_packages(payload: object):
    """(source, package-entry) pairs. `results` is null — not [] — when
    osv-scanner found nothing to scan."""
    if not isinstance(payload, dict):
        return
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        source = result.get("source")
        source = source if isinstance(source, dict) else {}
        for entry in result.get("packages") or []:
            if isinstance(entry, dict) and isinstance(entry.get("package"), dict):
                yield source, entry


def _grade(record: dict, group_max: str) -> str:
    """Severity ladder, identical to the OSV API path so the same advisory
    from both sources cannot disagree.

    `group_max` is a bare score ("7.5"), which is exactly why it goes through
    the CVSS helper instead of `float()`: the same field holds vector strings
    elsewhere, and `float()` on one of those is how every CVSS-only advisory
    once collapsed to "medium".
    """
    return osv_severity(record, default="") or severity_from_vector(
        group_max, default="medium")


def _subproject(path: object, repo_path: Path | str | None) -> str:
    """Repo-relative directory of the manifest — the unit the UI groups by.
    osv-scanner reports an absolute path to the file itself."""
    text = str(path or "")
    if not text:
        return ""
    if repo_path is not None:
        with contextlib.suppress(ValueError, OSError):
            text = Path(text).resolve().relative_to(
                Path(repo_path).resolve()).as_posix()
    parent = Path(text).parent.as_posix()
    return "" if parent in (".", "/") else parent


def _degraded(code: str, reason: str) -> NativeResult:
    """No findings and a reason — never an exception. One missing engine must
    not take the audit down with it."""
    status = "failed" if code in (REASON_BAD_JSON, REASON_FAILED) else "not_checked"
    logger.warning("osv_scanner_unavailable code=%s reason=%s", code, reason)
    return NativeResult(
        findings=[],
        # "all" rather than a real ecosystem: what was lost is every ecosystem
        # only this engine covers, and the report has to say so.
        checks=[EcosystemCheck("all", SOURCE, "", status, str(reason)[:300], 0, code)],
    )


def _stderr_tail(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


__all__ = [
    "BINARY",
    "DEFAULT_TIMEOUT",
    "REASON_BAD_JSON",
    "REASON_FAILED",
    "REASON_MISSING",
    "REASON_NO_SOURCES",
    "REASON_OS_ERROR",
    "REASON_TIMEOUT",
    "SOURCE",
    "audit_repo",
    "canonical_ecosystem",
    "parse_osv_scanner",
    "scanned_ecosystems",
]
