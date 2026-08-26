"""Native ecosystem auditors — the primary vulnerability source.

Every ecosystem ships a tool that already knows how to resolve the *full*
dependency tree and match it against its own advisory database: `pip-audit`,
`npm audit`, `pnpm audit`, `yarn npm audit`, `govulncheck`, `cargo audit`,
`composer audit`, `bundler-audit`. When one of them is installed in the
container it beats anything we can reconstruct from a manifest, because it
sees transitive dependencies — where most real CVEs actually live.

Three rules this module exists to enforce:

1. **A non-zero exit code means "findings", not "failure".** Every one of
   these tools exits 1 when it finds something. Treating that as an error is
   how an audit reports a clean bill of health for a vulnerable repo.
2. **"Tool absent" and "tool found nothing" are different answers.** The
   first is `not_checked` with a reason, and it must reach the UI — a silent
   zero is worse than no number at all.
3. **Parsing is pure.** Every `parse_*` function takes already-captured
   output, so the tests pin real tool output without executing anything.

All parsers converge on `NativeFinding`, which carries the two facts the
manifest pipeline could never supply: `transitive` and `source`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from src.deps.severity import normalize_severity, severity_from_vector

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300          # per tool invocation, seconds
MAX_SUBPROJECTS_PER_ECOSYSTEM = 8

# `pip-audit .` audits a *project*, which means pip-audit builds it — i.e. the
# cloned repo's build backend executes. That is more than reading files, so it
# gets its own switch: turning it off keeps requirements.txt auditing (which
# only resolves metadata) and falls back to poetry.lock + OSV for the rest.
PROJECT_BUILD_AUDIT = os.getenv("DEPS_PIP_AUDIT_PROJECT", "1").lower() not in (
    "0", "false", "no")

# `pip-audit -r` is not the safe half of that switch. It resolves by running
# `pip install --dry-run`, which evaluates a source distribution's build
# metadata — verified in production, where Pillow 10.1.0 failed to BUILD and
# took the whole file's audit with it. On a server auditing a repo the operator
# chose that is a considered risk. In somebody's CI, where the input is an
# outside contributor's pull request, it is arbitrary code execution on the
# runner. This forces the pinned-only path that never invokes pip at all —
# fewer transitives, no execution.
PIP_AUDIT_NO_EXEC = os.getenv("DEPS_PIP_AUDIT_NO_EXEC", "0").lower() in (
    "1", "true", "yes")

_SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", ".venv",
              "venv", "__pycache__", ".next", "target", "site-packages"}

_GHSA_RE = re.compile(r"(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})", re.I)
_CVE_RE = re.compile(r"(CVE-\d{4}-\d{4,})", re.I)

# Why a tool did not run, machine-readable. "Install the binary" and "the run
# broke" are different actions and nobody should match on English to tell them
# apart — these end up in the audit summary as `reason_code`.
TOOL_MISSING = "binary_missing"
TOOL_TIMEOUT = "timeout"
TOOL_OS_ERROR = "os_error"


# ─── Common shape ────────────────────────────────────────────────────


@dataclass(frozen=True)
class NativeFinding:
    """One advisory against one resolved package, from one tool."""

    ecosystem: str            # npm | PyPI | Go | crates.io | Packagist | RubyGems
    package: str
    version: str              # "" when the tool doesn't state the installed version
    severity: str             # none|low|medium|high|critical
    vuln_id: str              # GHSA-… / PYSEC-… / GO-… / RUSTSEC-… / CVE-…
    cve: str | None
    fixed_in: str | None
    summary: str
    url: str
    is_dev: bool
    transitive: bool
    source: str               # pip-audit | npm-audit | osv-scanner | …
    subproject: str = ""      # repo-relative dir of the manifest ("" = root)
    # Every other id the advisory answers to. Two tools routinely pick
    # different primary ids for one problem (PYSEC-… vs GHSA-…) and do not
    # always share a CVE; the alias list is what still collapses them into one.
    aliases: tuple[str, ...] = ()
    # OSV's ecosystem string when it carries more than the grouping name —
    # "Debian:12" against a row grouped as "Debian".
    ecosystem_raw: str = ""

    def to_vuln(self) -> dict:
        """The dict shape stored in `dep_findings.vulns` (JSONB) — a superset
        of the OSV shape, so the existing UI keeps working and the new fields
        (`source`, `transitive`) are simply extra."""
        vuln = {
            "id": self.vuln_id,
            "cve": self.cve,
            "severity": self.severity,
            "summary": self.summary[:300],
            "fixed_in": self.fixed_in,
            "url": self.url,
            "source": self.source,
            "transitive": self.transitive,
            "is_dev": self.is_dev,
            "subproject": self.subproject,
        }
        if self.aliases:
            vuln["aliases"] = list(self.aliases)
        if self.ecosystem_raw:
            vuln["ecosystem_raw"] = self.ecosystem_raw
        return vuln


@dataclass
class EcosystemCheck:
    """What we can honestly say about one (ecosystem, subproject) pair."""

    ecosystem: str
    tool: str
    subproject: str
    status: str               # checked | not_checked | failed
    reason: str = ""
    findings: int = 0
    # Machine-readable counterpart of `reason` — "install the binary" and "the
    # scan itself broke" are different actions, and nobody should have to
    # match on English to tell them apart.
    reason_code: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class NativeResult:
    findings: list[NativeFinding]
    checks: list[EcosystemCheck]

    def covered(self) -> set[str]:
        """Ecosystems a native tool actually audited — the auditor skips its
        own OSV pass for these to avoid double-reporting the same CVE."""
        return {c.ecosystem for c in self.checks if c.status == "checked"}


# ─── Process plumbing ────────────────────────────────────────────────


@dataclass
class CmdResult:
    ok: bool                  # the tool ran to completion (any exit code)
    code: int
    stdout: str
    stderr: str
    error: str = ""           # why it did NOT run (missing / timeout / oserror)
    reason_code: str = ""      # the same, machine-readable


def _tool_env() -> dict[str, str]:
    """Keep the auditors quiet, offline-ish and non-interactive."""
    env = dict(os.environ)
    env.update({
        "CI": "1",
        "NO_COLOR": "1",
        "npm_config_fund": "false",
        "npm_config_audit_level": "info",
        "npm_config_update_notifier": "false",
        "npm_config_yes": "true",
        "COMPOSER_NO_INTERACTION": "1",
        "GOFLAGS": "-mod=mod",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    return env


def run_tool(cmd: list[str], cwd: Path, timeout: int = DEFAULT_TIMEOUT) -> CmdResult:
    """Run an auditor. A non-zero exit is a *result*, not an error — only a
    missing binary, a timeout or an OS-level failure count as "didn't run"."""
    binary = shutil.which(cmd[0])
    if binary is None:
        return CmdResult(False, -1, "", "",
                         f"{cmd[0]} is not installed in this image", TOOL_MISSING)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [binary, *cmd[1:]], cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, env=_tool_env(), check=False,
        )
    except subprocess.TimeoutExpired:
        return CmdResult(False, -1, "", "",
                         f"{cmd[0]} timed out after {timeout}s", TOOL_TIMEOUT)
    except OSError as exc:
        return CmdResult(False, -1, "", "",
                         f"{cmd[0]} could not run: {exc}", TOOL_OS_ERROR)
    return CmdResult(True, proc.returncode, proc.stdout or "", proc.stderr or "")


def detect_tools() -> dict[str, str | None]:
    """source label → absolute path, or None when the tool is not in the image."""
    return {
        "pip-audit": shutil.which("pip-audit"),
        "npm-audit": shutil.which("npm"),
        "pnpm-audit": shutil.which("pnpm"),
        "yarn-audit": shutil.which("yarn"),
        "govulncheck": shutil.which("govulncheck"),
        "cargo-audit": shutil.which("cargo-audit") or shutil.which("cargo"),
        "composer-audit": shutil.which("composer"),
        "bundler-audit": shutil.which("bundle-audit") or shutil.which("bundler-audit"),
        # Not ecosystem-specific: the universal engine (src/deps/osv_scanner.py)
        # that covers Java, .NET, Dart, Elixir, Swift, Conan and the distro
        # packages none of the tools above know about.
        "osv-scanner": shutil.which("osv-scanner"),
    }


def _loads(text: str) -> object:
    """Tolerant JSON load — several tools print a banner before the payload."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


def _json_stream(text: str) -> list[dict]:
    """Concatenated / newline-delimited JSON objects (govulncheck, yarn v1)."""
    out: list[dict] = []
    decoder = json.JSONDecoder()
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except ValueError:
            nl = text.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        idx = end
    return out


def _ghsa(*candidates: object) -> str | None:
    """GHSA ids are canonically lowercase after the prefix — normalise so the
    same advisory from two tools dedupes instead of appearing twice."""
    for c in candidates:
        m = _GHSA_RE.search(str(c or ""))
        if m:
            return "GHSA-" + m.group(1)[len("GHSA-"):].lower()
    return None


def _cve(*candidates: object) -> str | None:
    for c in candidates:
        if isinstance(c, (list, tuple)):
            for item in c:
                found = _cve(item)
                if found:
                    return found
            continue
        m = _CVE_RE.search(str(c or ""))
        if m:
            return m.group(1).upper()
    return None


def _first_version(spec: object) -> str | None:
    """Lowest concrete version inside a fix expression: ">=4.17.19" → 4.17.19,
    ["1.0", "2.1"] → 1.0."""
    if isinstance(spec, (list, tuple)):
        versions = [v for v in (_first_version(s) for s in spec) if v]
        return sorted(versions, key=_verkey)[0] if versions else None
    m = re.search(r"(\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?)", str(spec or ""))
    return m.group(1) if m else None


def _verkey(v: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.\-+]", str(v).lstrip("v")):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, 0))
    while len(parts) < 4:
        parts.append((0, 0))
    return tuple(parts[:4])


# ─── pip-audit ───────────────────────────────────────────────────────


def parse_pip_audit(
    payload: object,
    *,
    direct: set[str] | None = None,
    dev: set[str] | None = None,
    force_dev: bool = False,
    subproject: str = "",
    source: str = "pip-audit",
) -> list[NativeFinding]:
    """`pip-audit -f json` — both the current `{"dependencies": [...]}` shape
    and the pre-2.0 bare list.

    pip-audit resolves the whole tree but never says which packages were
    *declared*; `direct` (the manifest's names) is what separates a direct hit
    from a transitive one.
    """
    if isinstance(payload, dict):
        deps = payload.get("dependencies") or []
    elif isinstance(payload, list):
        deps = payload
    else:
        return []
    direct_l = {n.lower() for n in (direct or set())}
    dev_l = {n.lower() for n in (dev or set())}

    out: list[NativeFinding] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "").lower()
        version = str(dep.get("version") or "")
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            vid = str(vuln.get("id") or "")
            aliases = vuln.get("aliases") or []
            description = str(vuln.get("description") or vuln.get("summary") or "")
            out.append(NativeFinding(
                ecosystem="PyPI",
                package=name,
                version=version,
                # pip-audit carries no severity of its own; the advisory id is
                # the honest answer and `medium` the honest default.
                severity=normalize_severity(vuln.get("severity"), default="medium"),
                vuln_id=vid,
                cve=_cve(vid, aliases),
                fixed_in=_first_version(vuln.get("fix_versions")),
                summary=description,
                url=f"https://osv.dev/vulnerability/{vid}" if vid else "",
                # `force_dev` is how a requirements-dev.txt run labels its whole
                # result set — pip-audit itself has no notion of dev extras.
                is_dev=force_dev or name in dev_l,
                transitive=bool(direct_l) and name not in direct_l,
                source=source,
                subproject=subproject,
            ))
    return out


# ─── npm audit (npm 7+ report v2) ────────────────────────────────────


def parse_npm_audit(
    payload: object,
    *,
    installed: dict[str, str] | None = None,
    prod_names: set[str] | None = None,
    subproject: str = "",
    source: str = "npm-audit",
) -> list[NativeFinding]:
    """`npm audit --json`.

    `prod_names` is the vulnerable-package set from the second, `--omit=dev`
    run: a package that is vulnerable in the full tree but absent from the
    production tree is a dev-only problem, and saying so is the difference
    between "ship-blocking" and "tidy up later".

    npm's v2 report states the *vulnerable range*, never the installed
    version, so `installed` (from package-lock.json) fills that in.
    """
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("advisories"), dict):
        return parse_npm_v1_audit(payload, subproject=subproject, source=source)
    vulns = payload.get("vulnerabilities")
    if not isinstance(vulns, dict):
        return []

    installed = {k.lower(): v for k, v in (installed or {}).items()}
    out: list[NativeFinding] = []
    for name, entry in vulns.items():
        if not isinstance(entry, dict):
            continue
        pkg = str(entry.get("name") or name)
        severity = normalize_severity(entry.get("severity"), default="medium")
        transitive = not bool(entry.get("isDirect", False))
        is_dev = prod_names is not None and pkg not in prod_names
        fix = entry.get("fixAvailable")
        # `fixAvailable` may name a *different* package (the parent that has
        # to move); only a fix for this package is a version to jump to.
        fixed_in = (str(fix.get("version")) if isinstance(fix, dict)
                    and str(fix.get("name") or pkg) == pkg and fix.get("version")
                    else None)
        version = installed.get(pkg.lower(), "")

        advisories = [v for v in (entry.get("via") or []) if isinstance(v, dict)]
        via_names = [str(v) for v in (entry.get("via") or []) if isinstance(v, str)]
        if advisories:
            for adv in advisories:
                url = str(adv.get("url") or "")
                gid = _ghsa(url, adv.get("id"))
                out.append(NativeFinding(
                    ecosystem="npm",
                    package=pkg,
                    version=version,
                    severity=normalize_severity(adv.get("severity"), default=severity),
                    vuln_id=gid or f"npm-{adv.get('source') or adv.get('id') or pkg}",
                    cve=_cve(adv.get("cve"), adv.get("cves"), adv.get("title")),
                    # `range` is the VULNERABLE range, never a fix — inferring
                    # a target version from it would be a confident lie.
                    fixed_in=fixed_in,
                    summary=str(adv.get("title") or ""),
                    url=url,
                    is_dev=is_dev,
                    transitive=transitive,
                    source=source,
                    subproject=subproject,
                ))
        else:
            # Vulnerable only *through* another package — npm reports the
            # chain, not an advisory of its own.
            out.append(NativeFinding(
                ecosystem="npm",
                package=pkg,
                version=version,
                severity=severity,
                vuln_id=f"npm-chain-{pkg}",
                cve=None,
                fixed_in=fixed_in,
                summary=("vulnerable via " + ", ".join(via_names)) if via_names else
                        f"vulnerable range {entry.get('range') or '?'}",
                url="",
                is_dev=is_dev,
                transitive=transitive,
                source=source,
                subproject=subproject,
            ))
    return out


def parse_npm_v1_audit(
    payload: object, *, subproject: str = "", source: str = "npm-audit",
) -> list[NativeFinding]:
    """The `{"advisories": {...}}` report — npm 6, pnpm audit and
    `yarn npm audit` all speak it.

    Here transitivity is explicit: a finding path of "foo>lodash" means lodash
    was dragged in by foo.
    """
    if not isinstance(payload, dict):
        return []
    advisories = payload.get("advisories")
    if not isinstance(advisories, dict):
        return []

    out: list[NativeFinding] = []
    for key, adv in advisories.items():
        if not isinstance(adv, dict):
            continue
        name = str(adv.get("module_name") or "")
        url = str(adv.get("url") or "")
        gid = _ghsa(adv.get("github_advisory_id"), url)
        vid = gid or str(adv.get("id") or key)
        severity = normalize_severity(adv.get("severity"), default="medium")
        fixed_in = _first_version(adv.get("patched_versions"))
        cve = _cve(adv.get("cves"), adv.get("cve"), url)
        summary = str(adv.get("title") or adv.get("overview") or "")

        findings = [f for f in (adv.get("findings") or []) if isinstance(f, dict)]
        if not findings:
            findings = [{}]
        for finding in findings:
            paths = [str(p) for p in (finding.get("paths") or [])]
            out.append(NativeFinding(
                ecosystem="npm",
                package=name,
                version=str(finding.get("version") or ""),
                severity=severity,
                vuln_id=vid,
                cve=cve,
                fixed_in=fixed_in,
                summary=summary,
                url=url,
                is_dev=bool(finding.get("dev")),
                transitive=any(">" in p for p in paths),
                source=source,
                subproject=subproject,
            ))
    return out


def parse_yarn_classic_audit(
    text: str, *, subproject: str = "", source: str = "yarn-audit",
) -> list[NativeFinding]:
    """`yarn audit --json` (yarn 1) — newline-delimited events. The
    `resolution` block is the richest transitivity/dev signal of any tool:
    both are stated per occurrence."""
    out: list[NativeFinding] = []
    for event in _json_stream(text):
        if event.get("type") != "auditAdvisory":
            continue
        data = event.get("data") or {}
        adv = data.get("advisory") or {}
        resolution = data.get("resolution") or {}
        if not isinstance(adv, dict):
            continue
        url = str(adv.get("url") or "")
        gid = _ghsa(adv.get("github_advisory_id"), url)
        path = str(resolution.get("path") or "")
        findings = [f for f in (adv.get("findings") or []) if isinstance(f, dict)]
        version = str(findings[0].get("version")) if findings else ""
        out.append(NativeFinding(
            ecosystem="npm",
            package=str(adv.get("module_name") or ""),
            version=version,
            severity=normalize_severity(adv.get("severity"), default="medium"),
            vuln_id=gid or str(adv.get("id") or ""),
            cve=_cve(adv.get("cves"), url),
            fixed_in=_first_version(adv.get("patched_versions")),
            summary=str(adv.get("title") or ""),
            url=url,
            is_dev=bool(resolution.get("dev")),
            transitive=">" in path,
            source=source,
            subproject=subproject,
        ))
    return out


# ─── govulncheck ─────────────────────────────────────────────────────


def parse_govulncheck(
    text: str,
    *,
    direct: set[str] | None = None,
    subproject: str = "",
    source: str = "govulncheck",
) -> list[NativeFinding]:
    """`govulncheck -json ./...` — a stream of `{"osv": …}` definitions and
    `{"finding": …}` hits.

    govulncheck emits a finding per trace level (module, package, symbol); we
    keep one row per (advisory, module) and prefer the symbol-level trace,
    because a *called* vulnerable function is the strongest signal there is.
    """
    osv: dict[str, dict] = {}
    hits: dict[tuple[str, str], dict] = {}
    for obj in _json_stream(text):
        entry = obj.get("osv")
        if isinstance(entry, dict) and entry.get("id"):
            osv[str(entry["id"])] = entry
        finding = obj.get("finding")
        if not isinstance(finding, dict):
            continue
        trace = [t for t in (finding.get("trace") or []) if isinstance(t, dict)]
        if not trace:
            continue
        # Frames run from the vulnerable symbol outwards, so frame 0 is the
        # affected module itself.
        frame = trace[0]
        module = str(frame.get("module") or "")
        if not module:
            continue
        key = (str(finding.get("osv") or ""), module)
        previous = hits.get(key)
        called = any(t.get("function") for t in trace)
        if previous is None or (called and not previous.get("_called")):
            hits[key] = {**finding, "_module": module,
                         "_version": str(frame.get("version") or "").lstrip("v"),
                         "_called": called}

    direct_l = {n.lower() for n in (direct or set())}
    out: list[NativeFinding] = []
    for (vid, module), finding in hits.items():
        entry = osv.get(vid) or {}
        aliases = entry.get("aliases") or []
        severity = normalize_severity(
            (entry.get("database_specific") or {}).get("severity"), default="medium")
        out.append(NativeFinding(
            ecosystem="Go",
            package=module,
            version=str(finding.get("_version") or ""),
            severity=severity,
            vuln_id=vid,
            cve=_cve(vid, aliases),
            fixed_in=str(finding.get("fixed_version") or "").lstrip("v") or None,
            summary=str(entry.get("summary") or entry.get("details") or "")
                    + ("" if finding.get("_called") else " (imported, not called)"),
            url=f"https://pkg.go.dev/vuln/{vid}" if vid else "",
            is_dev=False,
            transitive=bool(direct_l) and module.lower() not in direct_l,
            source=source,
            subproject=subproject,
        ))
    return out


# ─── cargo audit ─────────────────────────────────────────────────────


def parse_cargo_audit(
    payload: object,
    *,
    direct: set[str] | None = None,
    subproject: str = "",
    source: str = "cargo-audit",
) -> list[NativeFinding]:
    """`cargo audit --json` — RustSec advisories against Cargo.lock.

    RustSec states CVSS as a *vector*, so the severity here is computed, not
    guessed (see `severity.cvss_base_score`).
    """
    if not isinstance(payload, dict):
        return []
    vulns = ((payload.get("vulnerabilities") or {}).get("list")
             if isinstance(payload.get("vulnerabilities"), dict) else None) or []
    direct_l = {n.lower() for n in (direct or set())}

    out: list[NativeFinding] = []
    for item in vulns:
        if not isinstance(item, dict):
            continue
        adv = item.get("advisory") or {}
        pkg = item.get("package") or {}
        name = str(pkg.get("name") or adv.get("package") or "")
        cvss = adv.get("cvss")
        severity = (severity_from_vector(str(cvss), default="medium")
                    if cvss else normalize_severity(adv.get("severity"), default="medium"))
        patched = (item.get("versions") or {}).get("patched") if isinstance(
            item.get("versions"), dict) else None
        out.append(NativeFinding(
            ecosystem="crates.io",
            package=name,
            version=str(pkg.get("version") or ""),
            severity=severity,
            vuln_id=str(adv.get("id") or ""),
            cve=_cve(adv.get("aliases"), adv.get("id")),
            fixed_in=_first_version(patched),
            summary=str(adv.get("title") or ""),
            url=str(adv.get("url") or ""),
            is_dev=False,
            transitive=bool(direct_l) and name.lower() not in direct_l,
            source=source,
            subproject=subproject,
        ))
    return out


# ─── composer audit ──────────────────────────────────────────────────


def parse_composer_audit(
    payload: object,
    *,
    installed: dict[str, str] | None = None,
    direct: set[str] | None = None,
    subproject: str = "",
    source: str = "composer-audit",
) -> list[NativeFinding]:
    """`composer audit --format=json`. Abandoned packages are reported too —
    unmaintained code is a slow-motion vulnerability, but it is not a CVE, so
    it lands as `low` with an explicit summary rather than inflating counts."""
    if not isinstance(payload, dict):
        return []
    installed = {k.lower(): v for k, v in (installed or {}).items()}
    direct_l = {n.lower() for n in (direct or set())}
    out: list[NativeFinding] = []

    for name, items in (payload.get("advisories") or {}).items():
        entries = items if isinstance(items, list) else [items]
        for adv in entries:
            if not isinstance(adv, dict):
                continue
            pkg = str(adv.get("packageName") or name)
            sources = adv.get("sources") or []
            remote = next((str(s.get("remoteId")) for s in sources
                           if isinstance(s, dict) and s.get("remoteId")), "")
            out.append(NativeFinding(
                ecosystem="Packagist",
                package=pkg,
                version=installed.get(pkg.lower(), ""),
                severity=normalize_severity(adv.get("severity"), default="medium"),
                vuln_id=str(adv.get("cve") or remote or adv.get("advisoryId") or ""),
                cve=_cve(adv.get("cve")),
                fixed_in=None,      # composer states affected ranges, not fixes
                summary=str(adv.get("title") or ""),
                url=str(adv.get("link") or ""),
                is_dev=False,
                transitive=bool(direct_l) and pkg.lower() not in direct_l,
                source=source,
                subproject=subproject,
            ))
    for name in (payload.get("abandoned") or {}):
        replacement = (payload.get("abandoned") or {}).get(name)
        out.append(NativeFinding(
            ecosystem="Packagist", package=str(name),
            version=installed.get(str(name).lower(), ""),
            severity="low", vuln_id=f"abandoned:{name}", cve=None, fixed_in=None,
            summary=("package is abandoned" +
                     (f"; use {replacement} instead" if replacement else "")),
            url="", is_dev=False,
            transitive=bool(direct_l) and str(name).lower() not in direct_l,
            source=source, subproject=subproject,
        ))
    return out


# ─── bundler-audit ───────────────────────────────────────────────────


def parse_bundler_audit(
    text: str,
    *,
    direct: set[str] | None = None,
    subproject: str = "",
    source: str = "bundler-audit",
) -> list[NativeFinding]:
    """`bundle audit check --update` has no JSON mode — it prints
    `Key: value` blocks separated by blank lines. Parsed here rather than
    skipped, because Ruby shops are exactly the ones with old Rails CVEs."""
    direct_l = {n.lower() for n in (direct or set())}
    out: list[NativeFinding] = []
    block: dict[str, str] = {}

    def _flush() -> None:
        if not block.get("name"):
            block.clear()
            return
        name = block["name"]
        ghsa = block.get("ghsa") or ""
        if ghsa and not ghsa.upper().startswith("GHSA"):
            ghsa = f"GHSA-{ghsa}"
        cve = block.get("cve") or ""
        if cve and not cve.upper().startswith("CVE"):
            cve = f"CVE-{cve}"
        osvdb = block.get("osvdb") or ""
        vid = ghsa or cve or (f"OSVDB-{osvdb}" if osvdb else "")
        cve = cve or None
        out.append(NativeFinding(
            ecosystem="RubyGems",
            package=name,
            version=block.get("version", ""),
            severity=normalize_severity(block.get("criticality"), default="medium"),
            vuln_id=vid or f"bundler-audit:{name}",
            cve=cve,
            fixed_in=_first_version(block.get("solution", "")),
            summary=block.get("title", ""),
            url=block.get("url", ""),
            is_dev=False,
            transitive=bool(direct_l) and name.lower() not in direct_l,
            source=source,
            subproject=subproject,
        ))
        block.clear()

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            _flush()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        k = key.strip().lower()
        if k == "name" and block.get("name"):
            _flush()
        if k in ("name", "version", "cve", "ghsa", "osvdb", "criticality",
                 "url", "title", "solution"):
            block[k] = value.strip()
    _flush()
    return out


# ─── go.mod direct modules ───────────────────────────────────────────

_GO_REQUIRE = re.compile(r"^\s*(?P<mod>[\w./~-]+)\s+v(?P<ver>\S+)")


def go_direct_modules(text: str) -> set[str]:
    """Modules a go.mod requires *directly* — `// indirect` is go's own
    transitivity marker and the only place that information exists."""
    out: set[str] = set()
    in_block = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line.startswith(")"):
            in_block = False
            continue
        candidate = line
        if line.startswith("require "):
            candidate = line[len("require "):].strip()
        elif not in_block:
            continue
        indirect = "// indirect" in candidate
        candidate = candidate.split("//", 1)[0].strip()
        m = _GO_REQUIRE.match(candidate)
        if m and not indirect:
            out.add(m.group("mod").lower())
    return out


# ─── Orchestration ───────────────────────────────────────────────────


def _manifest_dirs(repo_path: Path, filename: str) -> list[Path]:
    dirs: list[tuple[int, Path]] = []
    for path in repo_path.rglob(filename):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        dirs.append((len(rel.parts), path.parent))
    dirs.sort(key=lambda t: (t[0], str(t[1])))
    seen: set[Path] = set()
    out: list[Path] = []
    for _, d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out[:MAX_SUBPROJECTS_PER_ECOSYSTEM]


def _rel(repo_path: Path, directory: Path) -> str:
    rel = str(directory.relative_to(repo_path))
    return "" if rel == "." else rel


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


_DEV_REQ_HINT = re.compile(r"(dev|test|lint|doc|ci)", re.I)


def _pip_audit_targets(directory: Path) -> list[tuple[list[str], bool]]:
    """What to point pip-audit at, and whether the result is dev-only.

    Requirement files are audited one at a time on purpose: pip-audit merges
    everything it is given into a single resolution, so a shared run would lose
    the only signal that distinguishes `requirements-dev.txt` from production.

    A bare `pip-audit` audits the *interpreter's* environment — which inside a
    container is our own venv, not the repo. For a pyproject-only project the
    correct target is the project path (`pip-audit .`).
    """
    reqs = sorted(p for p in directory.glob("requirements*.txt") if p.is_file())
    if reqs:
        return [(["-r", p.name], bool(_DEV_REQ_HINT.search(p.stem))) for p in reqs[:4]]
    if PROJECT_BUILD_AUDIT and (directory / "pyproject.toml").is_file():
        return [(["."], False)]
    return []


#: `name==version`, the only shape `pip-audit --no-deps` accepts. Extras and
#: environment markers are dropped — the advisory lookup keys on name+version
#: and neither changes which CVEs apply.
_PINNED_LINE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def _pip_audit_pinned_only(
    directory: Path,
    target: list[str],
    runner,
    timeout: int,
    original_error: str,
) -> tuple[dict | None, str]:
    """Second attempt: audit only the exactly-pinned requirements, no pip.

    `--no-deps --disable-pip` skips dependency resolution entirely, so a
    requirement that cannot be built for the running interpreter stops being
    able to blank the whole file. It refuses the run if ANY line is not pinned
    to an exact version, so the pinned lines are extracted into a temp file
    first and the count of what was dropped is reported rather than swallowed.

    Returns (payload, degradation_note). A note is only present on success —
    it is what the coverage panel prints, and it must say what is missing.
    """
    if not target or target[0] != "-r":
        return None, ""     # `pip-audit .` builds a project; there is nothing to pin
    source = directory / target[1]
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, ""

    pinned, skipped = [], 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _PINNED_LINE.match(stripped)
        if match:
            pinned.append(f"{match.group(1)}=={match.group(2)}")
        else:
            skipped += 1
    if not pinned:
        return None, ""

    with tempfile.TemporaryDirectory(prefix="pip-audit-pinned-") as tmp:
        pinned_file = Path(tmp) / "requirements.txt"
        pinned_file.write_text("\n".join(pinned) + "\n", encoding="utf-8")
        result = runner(
            ["pip-audit", "-s", "osv", "-f", "json", "--progress-spinner", "off",
             "--no-deps", "--disable-pip", "-r", str(pinned_file)],
            directory, timeout,
        )
        if not result.ok:
            return None, ""
        payload = _loads(result.stdout)
    if payload is None:
        return None, ""

    note = (f"dependency resolution failed ({original_error}) — audited "
            f"{len(pinned)} pinned requirements directly, without the "
            f"transitive tree")
    if skipped:
        note += f"; {skipped} unpinned line(s) skipped"
    return payload, note


def _npm_installed(directory: Path) -> dict[str, str]:
    """package name → resolved version, from the lock file next to the
    manifest — npm's own report never states it."""
    from src.deps.locks import parse_package_lock

    for name in ("package-lock.json", "npm-shrinkwrap.json"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            entries = parse_package_lock(json.loads(_read(path)), name)
        except Exception:  # noqa: BLE001
            return {}
        return {e.package: e.version for e in entries if e.version}
    return {}


def audit_repo(
    repo_path: Path,
    *,
    declared: list | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    runner=run_tool,
    on_progress=None,
) -> NativeResult:
    """Run every available native auditor over every subproject in a clone.

    `declared` is the manifest scan (`scanner.DeclaredDep`) — it supplies the
    direct/dev names that most tools don't report. `runner` is injectable so
    the orchestration is testable without installing eight toolchains.

    `on_progress(tool, subproject)` fires before every invocation. A single
    `pip-audit -r` can resolve for minutes; without a beat between tools the
    caller's run looks frozen and the UI declares it stuck. It is called
    without a guard on purpose — a caller that raises from it (cooperative
    cancellation) must be able to stop the sweep mid-way.
    """
    findings: list[NativeFinding] = []
    checks: list[EcosystemCheck] = []

    if on_progress is not None:
        _base_runner = runner

        def runner(cmd, cwd, tool_timeout):  # noqa: F811 — deliberate wrapper
            on_progress(cmd[0], _rel(repo_path, cwd))
            return _base_runner(cmd, cwd, tool_timeout)

    direct_by_eco: dict[str, set[str]] = {}
    dev_by_eco: dict[str, set[str]] = {}
    for dep in declared or []:
        direct_by_eco.setdefault(dep.ecosystem, set()).add(dep.package.lower())
        if dep.is_dev:
            dev_by_eco.setdefault(dep.ecosystem, set()).add(dep.package.lower())

    def _add(check: EcosystemCheck, produced: list[NativeFinding]) -> None:
        check.findings = len(produced)
        checks.append(check)
        findings.extend(produced)

    # ── Python ───────────────────────────────────────────────────────
    py_dirs: list[Path] = []
    for filename in ("requirements.txt", "pyproject.toml"):
        for directory in _manifest_dirs(repo_path, filename):
            if directory not in py_dirs:
                py_dirs.append(directory)
    for directory in py_dirs[:MAX_SUBPROJECTS_PER_ECOSYSTEM]:
        sub = _rel(repo_path, directory)
        targets = _pip_audit_targets(directory)
        if not targets:
            checks.append(EcosystemCheck(
                "PyPI", "pip-audit", sub, "not_checked",
                "no requirements*.txt and project-build auditing is off "
                "(DEPS_PIP_AUDIT_PROJECT=0) — falling back to lock files + OSV"))
            continue
        for target, force_dev in targets:
            if PIP_AUDIT_NO_EXEC:
                label = target[-1] if target else "environment"
                payload, note = _pip_audit_pinned_only(
                    directory, target, runner, timeout,
                    "execution disabled (DEPS_PIP_AUDIT_NO_EXEC=1)")
                if payload is None:
                    checks.append(EcosystemCheck(
                        "PyPI", "pip-audit", f"{sub}:{label}" if sub else label,
                        "not_checked",
                        "pip execution is disabled and the requirements are not "
                        "all pinned — falling back to lock files + OSV",
                        reason_code="pip_exec_disabled"))
                    continue
                _add(EcosystemCheck(
                        "PyPI", "pip-audit", f"{sub}:{label}" if sub else label,
                        "partial", note, reason_code="pip_exec_disabled"),
                     parse_pip_audit(payload, direct=direct_by_eco.get("PyPI"),
                                     dev=dev_by_eco.get("PyPI"),
                                     force_dev=force_dev, subproject=sub))
                continue
            # `-s osv` is mandatory: PyPI's own advisory feed is narrower than
            # OSV and would quietly under-report.
            cmd = ["pip-audit", "-s", "osv", "-f", "json",
                   "--progress-spinner", "off", *target]
            label = target[-1] if target else "environment"
            result = runner(cmd, directory, timeout)
            if not result.ok:
                checks.append(EcosystemCheck("PyPI", "pip-audit", sub,
                                             "not_checked", result.error))
                break   # the binary is missing/timing out — retrying per file is pointless
            payload = _loads(result.stdout)
            degraded = ""
            if payload is None:
                # Resolution failed. This is COMMON and not the project's
                # fault: pip-audit resolves by running `pip install --dry-run`,
                # so one requirement with no wheel for the running interpreter
                # (Pillow 10.1.0 on Python 3.13, say) fails the whole file and
                # PyPI silently drops to lock-files-and-OSV. Audit the pinned
                # requirements directly instead — no transitives, but the
                # direct dependencies stop being invisible.
                payload, degraded = _pip_audit_pinned_only(
                    directory, target, runner, timeout,
                    (result.stderr.strip().splitlines() or [""])[-1][:200])
            if payload is None:
                checks.append(EcosystemCheck(
                    "PyPI", "pip-audit", f"{sub}:{label}" if sub else label, "failed",
                    degraded or (result.stderr.strip().splitlines()
                                 or ["no JSON on stdout"])[-1][:200]))
                continue
            _add(EcosystemCheck(
                    "PyPI", "pip-audit", f"{sub}:{label}" if sub else label,
                    # "partial" keeps the row in the coverage panel's
                    # not-fully-checked list, which is the honest place for a
                    # scan that saw the direct dependencies and not the tree.
                    "partial" if degraded else "checked", degraded,
                    reason_code="pip_resolution_failed" if degraded else ""),
                 parse_pip_audit(payload, direct=direct_by_eco.get("PyPI"),
                                 dev=dev_by_eco.get("PyPI"), force_dev=force_dev,
                                 subproject=sub))

    # ── npm family: the lock file decides which tool owns the project ─
    for directory in _manifest_dirs(repo_path, "package.json"):
        sub = _rel(repo_path, directory)
        if (directory / "pnpm-lock.yaml").is_file():
            _add_npm_family(directory, sub, "pnpm", runner, timeout, checks, findings)
        elif (directory / "yarn.lock").is_file():
            _add_npm_family(directory, sub, "yarn", runner, timeout, checks, findings)
        elif (directory / "package-lock.json").is_file() or (
                directory / "npm-shrinkwrap.json").is_file():
            _add_npm_family(directory, sub, "npm", runner, timeout, checks, findings)
        else:
            checks.append(EcosystemCheck(
                "npm", "npm-audit", sub, "not_checked",
                "no lock file (package-lock.json / pnpm-lock.yaml / yarn.lock) — "
                "npm audit cannot resolve the tree"))

    # ── Go ───────────────────────────────────────────────────────────
    for directory in _manifest_dirs(repo_path, "go.mod"):
        sub = _rel(repo_path, directory)
        result = runner(["govulncheck", "-json", "./..."], directory, timeout)
        if not result.ok:
            checks.append(EcosystemCheck("Go", "govulncheck", sub, "not_checked", result.error))
            continue
        _add(EcosystemCheck("Go", "govulncheck", sub, "checked"),
             parse_govulncheck(result.stdout,
                               direct=go_direct_modules(_read(directory / "go.mod")),
                               subproject=sub))

    # ── Rust ─────────────────────────────────────────────────────────
    for directory in _manifest_dirs(repo_path, "Cargo.toml"):
        sub = _rel(repo_path, directory)
        if not (directory / "Cargo.lock").is_file():
            checks.append(EcosystemCheck("crates.io", "cargo-audit", sub, "not_checked",
                                         "no Cargo.lock — cargo audit needs a resolved tree"))
            continue
        result = runner(["cargo-audit", "audit", "--json"], directory, timeout)
        if not result.ok:
            result = runner(["cargo", "audit", "--json"], directory, timeout)
        if not result.ok:
            checks.append(EcosystemCheck("crates.io", "cargo-audit", sub,
                                         "not_checked", result.error))
            continue
        payload = _loads(result.stdout)
        if payload is None:
            checks.append(EcosystemCheck("crates.io", "cargo-audit", sub, "failed",
                                         (result.stderr or "no JSON").strip()[:200]))
            continue
        _add(EcosystemCheck("crates.io", "cargo-audit", sub, "checked"),
             parse_cargo_audit(payload, direct=direct_by_eco.get("crates.io"), subproject=sub))

    # ── PHP ──────────────────────────────────────────────────────────
    for directory in _manifest_dirs(repo_path, "composer.json"):
        sub = _rel(repo_path, directory)
        result = runner(["composer", "audit", "--format=json", "--no-interaction"],
                        directory, timeout)
        if not result.ok:
            checks.append(EcosystemCheck("Packagist", "composer-audit", sub,
                                         "not_checked", result.error))
            continue
        payload = _loads(result.stdout)
        if payload is None:
            checks.append(EcosystemCheck("Packagist", "composer-audit", sub, "failed",
                                         (result.stderr or "no JSON").strip()[:200]))
            continue
        _add(EcosystemCheck("Packagist", "composer-audit", sub, "checked"),
             parse_composer_audit(payload, subproject=sub))

    # ── Ruby ─────────────────────────────────────────────────────────
    for directory in _manifest_dirs(repo_path, "Gemfile"):
        sub = _rel(repo_path, directory)
        result = runner(["bundle-audit", "check", "--update"], directory, timeout)
        if not result.ok:
            result = runner(["bundle", "audit", "check", "--update"], directory, timeout)
        if not result.ok:
            checks.append(EcosystemCheck("RubyGems", "bundler-audit", sub,
                                         "not_checked", result.error))
            continue
        _add(EcosystemCheck("RubyGems", "bundler-audit", sub, "checked"),
             parse_bundler_audit(result.stdout, subproject=sub))

    return NativeResult(findings=findings, checks=checks)


def _add_npm_family(
    directory: Path, sub: str, flavour: str, runner, timeout: int,
    checks: list[EcosystemCheck], findings: list[NativeFinding],
) -> None:
    """npm / pnpm / yarn share an ecosystem and an output dialect but not a
    command line. Only npm can answer the prod-vs-dev question cheaply (a
    second `--omit=dev` pass), so only npm gets one."""
    if flavour == "npm":
        tool, cmd = "npm-audit", ["npm", "audit", "--json"]
    elif flavour == "pnpm":
        tool, cmd = "pnpm-audit", ["pnpm", "audit", "--json"]
    else:
        tool, cmd = "yarn-audit", ["yarn", "npm", "audit", "--all", "--json"]

    result = runner(cmd, directory, timeout)
    if not result.ok and flavour == "yarn":
        # yarn 1 has a different verb and emits NDJSON.
        result = runner(["yarn", "audit", "--json"], directory, timeout)
        if result.ok:
            produced = parse_yarn_classic_audit(result.stdout, subproject=sub)
            checks.append(EcosystemCheck("npm", tool, sub, "checked", "", len(produced)))
            findings.extend(produced)
            return
    if not result.ok:
        checks.append(EcosystemCheck("npm", tool, sub, "not_checked", result.error))
        return

    payload = _loads(result.stdout)
    if payload is None:
        # yarn 1 output is NDJSON, which `_loads` cannot swallow whole.
        produced = parse_yarn_classic_audit(result.stdout, subproject=sub) if flavour == "yarn" else []
        if produced:
            checks.append(EcosystemCheck("npm", tool, sub, "checked", "", len(produced)))
            findings.extend(produced)
            return
        checks.append(EcosystemCheck(
            "npm", tool, sub, "failed",
            (result.stderr or "no JSON on stdout").strip().splitlines()[-1][:200]
            if result.stderr else "no JSON on stdout"))
        return

    prod_names: set[str] | None = None
    if flavour == "npm":
        prod = runner(["npm", "audit", "--omit=dev", "--json"], directory, timeout)
        prod_payload = _loads(prod.stdout) if prod.ok else None
        if isinstance(prod_payload, dict) and isinstance(prod_payload.get("vulnerabilities"), dict):
            prod_names = set(prod_payload["vulnerabilities"])

    produced = parse_npm_audit(
        payload, installed=_npm_installed(directory),
        prod_names=prod_names, subproject=sub, source=tool,
    )
    checks.append(EcosystemCheck("npm", tool, sub, "checked", "", len(produced)))
    findings.extend(produced)


__all__ = [
    "DEFAULT_TIMEOUT",
    "CmdResult",
    "EcosystemCheck",
    "NativeFinding",
    "NativeResult",
    "audit_repo",
    "detect_tools",
    "go_direct_modules",
    "parse_bundler_audit",
    "parse_cargo_audit",
    "parse_composer_audit",
    "parse_govulncheck",
    "parse_npm_audit",
    "parse_npm_v1_audit",
    "parse_pip_audit",
    "parse_yarn_classic_audit",
    "run_tool",
]
