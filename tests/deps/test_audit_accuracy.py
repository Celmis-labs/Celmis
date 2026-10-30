"""Accuracy regressions of the dependency audit, one group per observed bug.

Every fixture is generic (`acme-api`, `shop-web`); the cases are the real ones
seen in a production run, renamed.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.deps import native, osv_scanner
from src.deps.applicability import (
    AFFECTED,
    NOT_AFFECTED,
    evaluate_osv,
    fixes_say_fixed,
    lowest_fix_above,
    semver_range_contains,
)
from src.deps.auditor import RowIndex, _apply_resolved, _mark_no_manifests
from src.deps.native import CmdResult, audit_repo, is_gap, parse_npm_audit, parse_pip_audit
from src.deps.registries import fetch_vulns_batch, osv_fixed_version
from src.deps.report import build_prompt
from src.deps.scanner import DeclaredDep, scan_repo


def _w(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── 1. a range floor is not the installed version ────────────────────


def test_a_lock_file_replaces_the_floor_of_a_python_range(tmp_path) -> None:
    _w(tmp_path / "requirements.txt", "django>=4.2\npillow>=10\nuvicorn==0.34.0\n")
    _w(tmp_path / "uv.lock", '''
version = 1
[[package]]
name = "django"
version = "5.2.7"
[[package]]
name = "acme-api"
version = "0.1.0"
source = { virtual = "." }
''')
    deps = {d.package: d for d in scan_repo(tmp_path)}
    assert (deps["django"].version, deps["django"].version_source) == ("5.2.7", "lock")
    # No lock entry: the floor stays a floor and says so.
    assert (deps["pillow"].version, deps["pillow"].version_source) == ("10", "range")
    # Exact pins are left as they are.
    assert (deps["uvicorn"].version, deps["uvicorn"].version_source) == ("0.34.0", "pin")


def test_npm_range_uses_the_lock_and_a_workspace_root_lock_covers_members(tmp_path) -> None:
    _w(tmp_path / "package.json", json.dumps({
        "name": "shop-web", "workspaces": ["apps/*"], "dependencies": {"left-pad": "1.3.0"}}))
    _w(tmp_path / "package-lock.json", json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {}, "node_modules/postcss": {"version": "8.5.6"},
                     "node_modules/left-pad": {"version": "1.3.0"}}}))
    _w(tmp_path / "apps/site/package.json", json.dumps({
        "name": "site", "dependencies": {"postcss": "8", "chalk": "^5"}}))
    deps = {d.package: d for d in scan_repo(tmp_path)}
    assert (deps["postcss"].version, deps["postcss"].version_source) == ("8.5.6", "lock")
    assert deps["chalk"].version_source == "range", "not in any lock: still only a range"
    assert deps["left-pad"].version_source == "pin"


def test_a_resolved_version_replaces_a_python_floor() -> None:
    deps = [DeclaredDep("PyPI", "django", "4.2", ">=4.2", False, "svc/requirements.txt",
                        "range"),
            DeclaredDep("PyPI", "uvicorn", "0.34.0", "==0.34.0", False,
                        "svc/requirements.txt", "pin")]
    out = _apply_resolved(deps, {("svc", "django"): "5.2.7"})
    assert (out[0].version, out[0].version_source) == ("5.2.7", "resolved")
    assert out[1] == deps[1]


# ── 2. Python tree resolved by uv, osv-scanner does not resolve ──────

UV_TREE = "anyio==4.15.1\nfastapi==0.141.1\nidna==3.20\nstarlette==1.6.0\n"


def _uv_runner(calls, *, uv_ok=True, audit_vulns=None):
    def runner(cmd, cwd, timeout):
        calls.append(cmd)
        if cmd[0] == "uv":
            if not uv_ok:
                return CmdResult(True, 1, "", "Downloading x\nerror: No solution found when "
                                 "resolving dependencies:\n  Because acme-lib==9 has no wheels"
                                 " for Python 3.14\n")
            return CmdResult(True, 0, UV_TREE, "")
        if cmd[0] == "pip-audit":
            fed = Path(cmd[-1]).read_text()
            deps = [{"name": ln.split("==")[0], "version": ln.split("==")[1],
                     "vulns": (audit_vulns or {}).get(ln.split("==")[0], [])}
                    for ln in fed.split()]
            return CmdResult(True, 1, json.dumps({"dependencies": deps}), "")
        return CmdResult(False, -1, "", "", f"{cmd[0]} is not installed in this image",
                         native.TOOL_MISSING)
    return runner


def test_python_tree_comes_from_uv_for_the_dockerfile_python(tmp_path) -> None:
    _w(tmp_path / "Dockerfile", "FROM python:3.14-slim AS base\nCOPY . .\n")
    _w(tmp_path / "requirements.txt", "fastapi==0.141.1\nstarlette==1.6.0\n")
    calls: list[list[str]] = []
    declared = [DeclaredDep("PyPI", "fastapi", "0.141.1", "==0.141.1", False,
                            "requirements.txt")]
    result = audit_repo(tmp_path, declared=declared, runner=_uv_runner(calls, audit_vulns={
        "idna": [{"id": "PYSEC-X", "fix_versions": ["3.99"], "aliases": []}]}))
    uv_cmd = next(c for c in calls if c[0] == "uv")
    assert uv_cmd[:3] == ["uv", "pip", "compile"]
    assert uv_cmd[uv_cmd.index("--python-version") + 1] == "3.14"
    assert "--no-build" in uv_cmd, "metadata only: no repo build backend runs"
    # The audit saw the FULL pinned tree, with the versions uv resolved.
    assert result.resolved[("", "anyio")] == "4.15.1"
    assert result.resolved[("", "idna")] == "3.20"
    check = next(c for c in result.checks if c.ecosystem == "PyPI")
    assert check.status == "checked" and "resolved on" in check.reason
    assert "no lock file" in check.reason and "Python 3.14" in check.reason
    idna = next(f for f in result.findings if f.package == "idna")
    assert idna.version == "3.20" and idna.transitive is True


def test_python_target_order(tmp_path) -> None:
    sub = tmp_path / "svc"
    _w(sub / "requirements.txt", "x==1\n")
    _w(tmp_path / "pyproject.toml", '[project]\nname="a"\nrequires-python=">=3.11"\n')
    assert native.python_target(tmp_path, sub)[0] == "3.11"
    _w(tmp_path / ".python-version", "3.13.1\n")
    assert native.python_target(tmp_path, sub) == ("3.13", ".python-version")
    _w(tmp_path / "Dockerfile.api", "FROM --platform=$BUILDPLATFORM python:3.12-slim\n")
    assert native.python_target(tmp_path, sub)[0] == "3.12"
    _w(sub / "Dockerfile", "FROM python:3.14\n")
    assert native.python_target(tmp_path, sub)[0] == "3.14", "the nearest Dockerfile wins"


def test_a_failed_resolution_keeps_the_full_stderr_tail(tmp_path) -> None:
    _w(tmp_path / "requirements.txt", "acme-lib==9\n")
    result = audit_repo(tmp_path, declared=[], runner=_uv_runner([], uv_ok=False))
    check = next(c for c in result.checks if c.ecosystem == "PyPI")
    assert check.status in ("partial", "failed")
    assert "No solution found" in check.reason and "no wheels for Python" in check.reason
    assert check.status != "checked"


def test_osv_scanner_is_told_not_to_resolve_manifests() -> None:
    seen: list[list[str]] = []

    def runner(cmd, cwd, timeout):
        seen.append(cmd)
        return CmdResult(True, 0, json.dumps({"results": None}), "")

    osv_scanner.audit_repo(Path("."), runner=runner)
    assert "--no-resolve" in seen[0]


def _osv_payload(source_path: str, name: str, version: str) -> dict:
    return {"results": [{"source": {"path": source_path, "type": "lockfile"},
                         "packages": [{"package": {"name": name, "version": version,
                                                   "ecosystem": "PyPI"},
                                       "vulnerabilities": [{"id": "GHSA-aaaa-bbbb-cccc",
                                                            "aliases": []}]}]}]}


def test_manifest_derived_python_packages_are_dropped_when_uv_resolved_them() -> None:
    payload = _osv_payload("/repo/requirements.txt", "anyio", "4.9.0")
    kept = osv_scanner.parse_osv_scanner(payload)
    assert [f.version for f in kept] == ["4.9.0"]
    skipped = osv_scanner.parse_osv_scanner(payload, skip_manifest_ecosystems={"PyPI"})
    assert skipped == []
    # A real lock file is still read.
    lock = _osv_payload("/repo/poetry.lock", "anyio", "4.15.1")
    assert osv_scanner.parse_osv_scanner(lock, skip_manifest_ecosystems={"PyPI"})


# ── 3. one row per package AND version ───────────────────────────────


def test_rows_are_kept_per_version_not_merged_across_versions() -> None:
    index = RowIndex("run-1")
    old = index.row("acme-api", "PyPI", "idna", "3.10", is_dev=False, transitive=True)
    new = index.row("acme-api", "PyPI", "idna", "3.18", is_dev=False, transitive=True)
    assert old is not new, "an advisory for 3.10 must not land on the 3.18 row"
    assert new["current_version"] == "3.18"
    again = index.row("acme-api", "PyPI", "idna", "3.18", is_dev=True, transitive=False)
    assert again is new and new["is_dev"] is False, "declared-dev merge keeps the AND rule"
    # A report that states no version attaches only when it is unambiguous.
    only = RowIndex("run-2")
    r = only.row("acme-api", "npm", "left-pad", "1.3.0", is_dev=False, transitive=False)
    assert only.row("acme-api", "npm", "left-pad", "", is_dev=False, transitive=True) is r
    only.row("acme-api", "npm", "left-pad", "1.2.0", is_dev=False, transitive=True)
    ambiguous = only.row("acme-api", "npm", "left-pad", "", is_dev=False, transitive=True)
    assert ambiguous is not r, "two versions: an unversioned report gets its own row"


# ── 4. applicability and the right "fixed in" ────────────────────────


def _record(pkg: str, eco: str, *ranges: list[dict], rtype: str = "SEMVER") -> dict:
    return {"id": "GHSA-test-0000-0001", "affected": [{
        "package": {"name": pkg, "ecosystem": eco},
        "ranges": [{"type": rtype, "events": ev} for ev in ranges]}]}


MULTI = _record(
    "glob-lite", "npm",
    [{"introduced": "0"}, {"fixed": "2.3.2"}],
    [{"introduced": "3.0.0"}, {"fixed": "3.1.8"}],
    [{"introduced": "4.0.0"}, {"fixed": "4.0.5"}])


def test_fixed_in_comes_from_the_range_that_contains_the_installed_version() -> None:
    assert osv_fixed_version(MULTI, "npm", "glob-lite", "4.0.3") == "4.0.5"
    assert osv_fixed_version(MULTI, "npm", "glob-lite", "3.0.9") == "3.1.8"
    assert osv_fixed_version(MULTI, "npm", "glob-lite", "1.0.0") == "2.3.2"


def test_a_version_above_every_fix_gets_no_downgrade_and_no_advisory() -> None:
    # picomatch-like: 4.0.3 was "fixed in 2.3.2"; undici-like 8.x "fixed in 7.30.1".
    two = _record("net-lite", "npm", [{"introduced": "0"}, {"fixed": "6.9.0"}],
                  [{"introduced": "7.0.0"}, {"fixed": "7.30.1"}])
    assert evaluate_osv(two, "npm", "net-lite", "8.2.0") == (NOT_AFFECTED, None)
    assert osv_fixed_version(two, "npm", "net-lite", "8.2.0") is None
    assert evaluate_osv(two, "npm", "net-lite", "7.1.0") == (AFFECTED, "7.30.1")


def test_range_event_kinds_are_respected() -> None:
    last = _record("fast-lite", "crates.io", [{"introduced": "1.0.0"}, {"last_affected": "1.4.0"}])
    assert evaluate_osv(last, "crates.io", "fast-lite", "1.4.0") == (AFFECTED, None)
    assert evaluate_osv(last, "crates.io", "fast-lite", "1.4.1")[0] == NOT_AFFECTED
    limited = _record("lim-lite", "npm", [{"introduced": "0"}, {"limit": "2.0.0"}])
    assert evaluate_osv(limited, "npm", "lim-lite", "2.0.0")[0] == NOT_AFFECTED
    pep = _record("pil-lite", "PyPI", [{"introduced": "0"}, {"fixed": "10.3.0"}],
                  rtype="ECOSYSTEM")
    assert evaluate_osv(pep, "PyPI", "pil-lite", "10.10.0")[0] == NOT_AFFECTED
    assert evaluate_osv(pep, "PyPI", "pil-lite", "10.3.0rc1")[0] == AFFECTED, "PEP 440 order"
    listed = {"id": "X", "affected": [{"package": {"name": "ver-lite", "ecosystem": "PyPI"},
                                        "versions": ["1.0", "1.1"]}]}
    assert evaluate_osv(listed, "PyPI", "ver-lite", "1.1")[0] == AFFECTED
    assert evaluate_osv(listed, "PyPI", "ver-lite", "1.2")[0] == NOT_AFFECTED
    git_only = _record("g-lite", "npm", [{"introduced": "abc"}, {"fixed": "def"}], rtype="GIT")
    assert evaluate_osv(git_only, "npm", "g-lite", "1.0.0")[0] == "unknown", "kept, not dropped"


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class _OsvClient:
    """querybatch says every id applies; the record's own ranges disagree."""

    def post(self, url, json=None):
        return _Resp({"results": [{"vulns": [{"id": "GHSA-old-0000-0001"}]}
                                  for _ in json["queries"]]})

    def get(self, url):
        rec = _record("idna", "PyPI", [{"introduced": "0"}, {"fixed": "3.15"}],
                      rtype="ECOSYSTEM")
        rec["id"] = "GHSA-old-0000-0001"
        return _Resp(rec)


def test_osv_api_advisory_for_an_older_copy_is_dropped_and_counted() -> None:
    dropped: list[dict] = []
    out, failed = fetch_vulns_batch(_OsvClient(), [("PyPI", "idna", "3.18")], dropped=dropped)
    assert out == {} and failed == 0
    assert dropped[0]["package"] == "idna" and "outside" in dropped[0]["reason"]
    kept, _ = fetch_vulns_batch(_OsvClient(), [("PyPI", "idna", "3.10")])
    assert kept[("PyPI", "idna", "3.10")][0]["fixed_in"] == "3.15"


def test_pip_audit_advisory_fixed_below_the_installed_version_is_dropped() -> None:
    payload = {"dependencies": [
        {"name": "idna", "version": "3.18",
         "vulns": [{"id": "PYSEC-OLD", "fix_versions": ["3.15"], "aliases": []}]},
        {"name": "logger-lite", "version": "2.14.1",
         "vulns": [{"id": "PYSEC-MULTI", "fix_versions": ["2.3.2", "2.12.2", "2.15.0"],
                    "aliases": []}]}]}
    dropped: list[dict] = []
    findings = parse_pip_audit(payload, dropped=dropped)
    assert [f.package for f in findings] == ["logger-lite"]
    assert findings[0].fixed_in == "2.15.0", "lowest fix ABOVE the installed version"
    assert dropped[0]["id"] == "PYSEC-OLD" and dropped[0]["source"] == "pip-audit"
    assert fixes_say_fixed("PyPI", "2.13", ["2.12.2", "2.15.0"]) is False
    assert lowest_fix_above("PyPI", "9.0", ["1.0", "2.0"]) is None


def test_osv_scanner_drops_inapplicable_records() -> None:
    rec = _record("idna", "PyPI", [{"introduced": "0"}, {"fixed": "3.15"}], rtype="ECOSYSTEM")
    rec["id"] = "GHSA-old-0000-0001"
    payload = {"results": [{"source": {"path": "/r/uv.lock"}, "packages": [
        {"package": {"name": "idna", "version": "3.18", "ecosystem": "PyPI"},
         "vulnerabilities": [rec]}]}]}
    dropped: list[dict] = []
    assert osv_scanner.parse_osv_scanner(payload, dropped=dropped) == []
    assert dropped and dropped[0]["source"] == "osv-scanner"


def test_semver_vulnerable_range_matching() -> None:
    assert semver_range_contains(">=1.0.0 <1.2.3", "1.2.2") is True
    assert semver_range_contains(">=1.0.0 <1.2.3", "1.2.3") is False
    assert semver_range_contains("<1.0.0 || >=2.0.0 <2.1.0", "2.0.5") is True
    assert semver_range_contains("^1.2.0", "1.2.5") is None, "unparsed -> keep the advisory"


# ── 5. npm audit: chains and bogus fixes ─────────────────────────────

NPM_REPORT = {"auditReportVersion": 2, "vulnerabilities": {
    "ui-cli": {  # fixAvailable is a DOWNGRADE of this very package
        "name": "ui-cli", "severity": "high", "isDirect": True,
        "via": [{"source": 1, "name": "ui-cli", "title": "t",
                 "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                 "severity": "high", "range": ">=4.0.0"}],
        "fixAvailable": {"name": "ui-cli", "version": "1.0.0", "isSemVerMajor": True}},
    "shell-lite": {
        "name": "shell-lite", "severity": "critical", "isDirect": False,
        "via": [{"source": 2, "name": "shell-lite", "title": "t",
                 "url": "https://github.com/advisories/GHSA-dddd-eeee-ffff",
                 "severity": "critical", "range": "<1.11.0"}],
        "fixAvailable": {"name": "shell-lite", "version": "1.11.0", "isSemVerMajor": False}},
    "runner-lite": {  # chain entry: vulnerable only through shell-lite
        "name": "runner-lite", "severity": "critical", "isDirect": True,
        "via": ["shell-lite"],
        "fixAvailable": {"name": "runner-lite", "version": "1.0.0", "isSemVerMajor": True}},
    "other-pkg": {  # fixAvailable names a DIFFERENT package
        "name": "other-pkg", "severity": "low", "isDirect": False,
        "via": [{"source": 3, "name": "other-pkg", "title": "t",
                 "url": "https://github.com/advisories/GHSA-gggg-hhhh-iiii",
                 "severity": "low", "range": "<5.0.0"}],
        "fixAvailable": {"name": "parent-pkg", "version": "9.9.9"}},
    "old-pkg": {  # installed version is outside the advisory's range
        "name": "old-pkg", "severity": "low", "isDirect": False,
        "via": [{"source": 4, "name": "old-pkg", "title": "t",
                 "url": "https://github.com/advisories/GHSA-jjjj-kkkk-llll",
                 "severity": "low", "range": "<2.0.0"}],
        "fixAvailable": True},
}}
INSTALLED = {"ui-cli": "4.21.0", "shell-lite": "1.9.0", "runner-lite": "7.0.0",
             "other-pkg": "4.0.0", "old-pkg": "3.0.0"}


def test_npm_fix_is_never_a_downgrade_or_another_package() -> None:
    chains: list[dict] = []
    dropped: list[dict] = []
    findings = {f.package: f for f in parse_npm_audit(
        NPM_REPORT, installed=INSTALLED, chains=chains, dropped=dropped)}
    assert findings["ui-cli"].fixed_in is None, "1.0.0 is below the installed 4.21.0"
    assert findings["other-pkg"].fixed_in is None, "9.9.9 belongs to the parent"
    assert findings["shell-lite"].fixed_in == "1.11.0"
    assert "old-pkg" not in findings and dropped[0]["package"] == "old-pkg"


def test_npm_chain_entries_are_notes_not_vulnerabilities() -> None:
    chains: list[dict] = []
    findings = parse_npm_audit(NPM_REPORT, installed=INSTALLED, chains=chains)
    assert "runner-lite" not in {f.package for f in findings}
    assert not any(f.vuln_id.startswith("npm-chain-") for f in findings)
    (note,) = chains
    assert note["package"] == "runner-lite" and note["pulls_in"] == ["shell-lite"]
    assert note["fix_via"] == {"shell-lite": "1.11.0"}
    assert "fix via shell-lite >= 1.11.0" in note["note"]


# ── 6. coverage ──────────────────────────────────────────────────────


def _missing(cmd, cwd, timeout):
    return CmdResult(False, -1, "", "", f"{cmd[0]} is not installed in this image",
                     native.TOOL_MISSING)


def test_workspace_members_and_empty_packages_are_not_gaps(tmp_path) -> None:
    _w(tmp_path / "package.json", json.dumps({"name": "root", "workspaces": ["apps/*"]}))
    _w(tmp_path / "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {"": {}}}))
    _w(tmp_path / "apps/site/package.json", json.dumps({"dependencies": {"chalk": "^5"}}))
    _w(tmp_path / "tools/empty/package.json", json.dumps({"name": "empty"}))

    def runner(cmd, cwd, timeout):
        if cmd[0] == "npm":
            return CmdResult(True, 0, json.dumps(
                {"auditReportVersion": 2, "vulnerabilities": {}}), "")
        return _missing(cmd, cwd, timeout)

    result = audit_repo(tmp_path, declared=[], runner=runner)
    npm = {c.subproject: c for c in result.checks if c.ecosystem == "npm"}
    assert not is_gap(npm["apps/site"].status)
    assert "workspace root" in npm["apps/site"].reason
    assert "tools/empty" not in npm, "no dependencies: nothing to check, nothing to report"


def test_a_member_covered_by_a_sibling_lock_is_not_a_gap(tmp_path) -> None:
    _w(tmp_path / "web/package.json", json.dumps({"dependencies": {"chalk": "5.0.0"}}))
    _w(tmp_path / "web/package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {
        "": {}, "node_modules/chalk": {"version": "5.0.0"}}}))
    _w(tmp_path / "docs/package.json", json.dumps({"dependencies": {"chalk": "5.0.0"}}))
    result = audit_repo(tmp_path, declared=[], runner=lambda c, w, t: (
        CmdResult(True, 0, json.dumps({"auditReportVersion": 2, "vulnerabilities": {}}), "")
        if c[0] == "npm" else _missing(c, w, t)))
    docs = next(c for c in result.checks if c.subproject == "docs")
    assert not is_gap(docs.status)


def test_go_without_govulncheck_is_covered_by_osv_not_unchecked(tmp_path) -> None:
    _w(tmp_path / "go.mod", "module example.com/acme\n\ngo 1.24\n")
    result = audit_repo(tmp_path, declared=[], runner=_missing)
    (go,) = [c for c in result.checks if c.ecosystem == "Go"]
    assert go.status == "covered_by_osv" and not is_gap(go.status)
    assert "no call-reachability" in go.reason


def test_pnpm_lock_without_the_cli_is_covered_by_osv(tmp_path) -> None:
    _w(tmp_path / "package.json", json.dumps({"dependencies": {"chalk": "5.0.0"}}))
    _w(tmp_path / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    result = audit_repo(tmp_path, declared=[], runner=_missing)
    (npm,) = [c for c in result.checks if c.ecosystem == "npm"]
    assert npm.status == "covered_by_osv" and not is_gap(npm.status)
    assert "pnpm-lock.yaml" in npm.reason
    assert "npm" not in result.covered(), "OSV lock fallback must stay active"


def test_a_repo_without_manifests_is_not_a_failed_scan() -> None:
    checks = [{"repo": "cfg-export", "ecosystem": "all", "tool": "osv-scanner",
               "subproject": "", "status": "not_checked",
               "reason": "osv-scanner is not installed", "reason_code": "binary_missing"}]
    _mark_no_manifests("cfg-export", [], [], checks, 0)
    assert [c["status"] for c in checks] == ["no_manifests"]
    assert not is_gap(checks[0]["status"])
    # A repo that does have a manifest keeps its real findings about tools.
    keep = [{"repo": "acme-api", "ecosystem": "all", "tool": "osv-scanner",
             "status": "not_checked", "reason_code": "binary_missing"}]
    _mark_no_manifests("acme-api", [object()], [], keep, 0)
    assert keep[0]["status"] == "not_checked"


def test_osv_no_sources_is_no_manifests_not_a_gap() -> None:
    result = osv_scanner.audit_repo(Path("."), runner=lambda *_: CmdResult(
        True, 128, "", "no sources"))
    assert result.checks[0].status == "no_manifests"
    assert not is_gap(result.checks[0].status)


def test_gradle_version_catalog_is_parsed_into_maven_coordinates(tmp_path) -> None:
    _w(tmp_path / "gradle/libs.versions.toml", '''
[versions]
okio = "3.9.0"
core = { strictly = "[1.2, 1.4)" }
[libraries]
okio = { module = "com.acme.io:okio", version.ref = "okio" }
core = { group = "com.acme", name = "core", version.ref = "core" }
inline = "com.acme:inline:2.0.1"
rich = { module = "com.acme:rich", version = { require = "1.5.0" } }
noversion = { module = "com.acme:bom" }
''')
    _w(tmp_path / "app/build.gradle.kts", "dependencies { implementation(libs.okio) }\n")
    deps = {d.package: d for d in scan_repo(tmp_path)}
    assert (deps["com.acme.io:okio"].ecosystem, deps["com.acme.io:okio"].version,
            deps["com.acme.io:okio"].version_source) == ("Maven", "3.9.0", "pin")
    assert deps["com.acme:inline"].version == "2.0.1"
    assert deps["com.acme:core"].version_source == "range", "strictly range = a range"
    assert deps["com.acme:rich"].version_source == "range", "rich versions are ranges"
    assert "com.acme:bom" not in deps
    # With a gradle.lockfile the catalog is not second-guessed.
    _w(tmp_path / "gradle.lockfile", "com.acme.io:okio:3.9.0=compileClasspath\n")
    assert not [d for d in scan_repo(tmp_path) if d.ecosystem == "Maven"]


# ── Executive summary facts ──────────────────────────────────────────


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_the_facts_carry_provenance_and_coverage_categories() -> None:
    rows = [_Row(repo_slug="acme-api", ecosystem="PyPI", package="django",
                 current_version="range >=4.2", latest_version="5.2", outdated="unknown",
                 severity="none", vulns=[], version_source="range"),
            _Row(repo_slug="acme-api", ecosystem="PyPI", package="idna",
                 current_version="3.20", latest_version="3.20", outdated="none",
                 severity="high", version_source="resolved",
                 vulns=[{"id": "X", "severity": "high", "fixed_in": "3.21", "source": "pip-audit"}])]
    summary = {"coverage": {"checked": 3, "covered_by_osv": 1, "no_manifests": 1},
               "dropped_inapplicable": {"total": 4, "by_source": {"osv": 4}},
               "range_hygiene": {"total": 2}, "npm_chains": {"total": 5}}
    prompt = build_prompt(rows, summary)
    assert "[range]" in prompt and "[resolved]" in prompt
    assert "covered_by_osv" in prompt and "no_manifests" in prompt
    assert "4 advisories were NOT counted" in prompt
    assert "never describe a range floor as the installed version" in prompt
    assert "false positive" in prompt and "Do not speculate" in prompt
