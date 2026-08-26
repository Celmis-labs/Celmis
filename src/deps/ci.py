"""Dependency audit as a standalone command — no Celmis deployment needed.

Everything Celmis does well needs a server: the cross-repo graph, ownership,
history, Q&A. The dependency audit does not. It reads manifests and lock files
off disk, shells out to the ecosystem's own auditor, and asks OSV.dev — no
Postgres, no Qdrant, no LiteLLM, no workspace, and not one model call. So it
can run in somebody's CI on a pull request before they have heard of the
hosted product, which is a far cheaper first touch than "stand up a
multi-tenant stack".

Deliberately NOT `src.cli`: that module is Ukrainian, registers forty-odd
commands whose help text would render on `--help`, and almost none of them can
run without the API. This is its own entry point with two commands.

Deliberately NOT the structural rules either. There are nine of them and
ruff/eslint cover the same ground better; shipping them here would be volume
rather than value.

What is held back is held back because it genuinely cannot follow into a
runner, not to force an upgrade.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Ordered worst-first, so a threshold is a prefix of this list.
SEVERITIES = ("critical", "high", "medium", "low", "none")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass
class CiFinding:
    ecosystem: str
    package: str
    version: str
    severity: str
    vuln_id: str
    fixed_in: str | None
    summary: str
    url: str
    subproject: str = ""
    source: str = ""


@dataclass
class CiResult:
    """What one directory audit found, plus what it could not look at."""

    findings: list[CiFinding] = field(default_factory=list)
    #: (ecosystem, tool, subproject, status, reason) — the coverage gaps. An
    #: ecosystem nobody audited reports zero vulnerabilities exactly like a
    #: clean one, so this travels with the findings, never separately.
    checks: list[dict[str, Any]] = field(default_factory=list)
    packages_declared: int = 0

    def worst(self) -> str:
        return min((f.severity for f in self.findings),
                   key=lambda s: _RANK.get(s, 99), default="none")

    def count_at_or_above(self, threshold: str) -> int:
        limit = _RANK.get(threshold, 99)
        return sum(1 for f in self.findings if _RANK.get(f.severity, 99) <= limit)

    def not_checked(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if c.get("status") != "checked"]


def audit_path(directory: Path, *, timeout: int = 300) -> CiResult:
    """Audit one checkout. Never raises; an unavailable tool becomes a gap.

    Composes the same pure pieces the server orchestration uses — the manifest
    scan, the native auditors, and osv-scanner — without the clone, the
    workspace or the database rows that `run_audit` exists to write.
    """
    from src.deps import native, osv_scanner
    from src.deps.scanner import scan_repo

    result = CiResult()
    try:
        declared = scan_repo(directory)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ci_manifest_scan_failed err=%s", exc)
        declared = []
    result.packages_declared = len(declared)

    native_res = native.audit_repo(directory, declared=declared, timeout=timeout)
    _absorb(result, native_res, default_source="native")

    # osv-scanner covers the ecosystems no native tool claimed, so the same CVE
    # is not reported twice under two names.
    covered = native_res.covered()
    osv_res = osv_scanner.audit_repo(directory, declared=declared, timeout=timeout)
    _absorb(result, osv_res, default_source="osv-scanner", skip_ecosystems=covered)
    return result


def _key(ecosystem: str, package: str, version: str, vuln_id: str) -> tuple:
    """One advisory against one package version, however many tools saw it.

    Two auditors reporting the same CVE is normal — the server merges them via
    `merge_vuln`; without the same step here the comment listed `ecdsa`
    PYSEC-2026-1325 twice and the fail-on count double-counted it.
    """
    return (ecosystem.lower(), package.lower(), version, vuln_id)


def _absorb(result: CiResult, res, *, default_source: str,
            skip_ecosystems: set[str] | None = None) -> None:
    skip = skip_ecosystems or set()
    seen = {_key(f.ecosystem, f.package, f.version, f.vuln_id)
            for f in result.findings}
    for f in res.findings:
        if f.ecosystem in skip:
            continue
        k = _key(f.ecosystem, f.package, f.version, f.vuln_id)
        if k in seen:
            continue
        seen.add(k)
        result.findings.append(CiFinding(
            ecosystem=f.ecosystem, package=f.package, version=f.version,
            severity=f.severity, vuln_id=f.vuln_id, fixed_in=f.fixed_in,
            summary=f.summary, url=f.url,
            subproject=getattr(f, "subproject", "") or "",
            source=getattr(f, "source", "") or default_source,
        ))
    for c in res.checks:
        result.checks.append(c.as_dict() if hasattr(c, "as_dict") else dict(c))


# ─── Output ──────────────────────────────────────────────────────────


def to_markdown(result: CiResult, *, target: str) -> str:
    """A pull-request comment. Coverage gaps come BEFORE the findings, because
    a zero from an ecosystem nobody audited looks exactly like a clean one."""
    vulnerable = sorted(
        (f for f in result.findings if f.severity != "none"),
        key=lambda f: (_RANK.get(f.severity, 99), f.package),
    )
    counts = {s: sum(1 for f in vulnerable if f.severity == s) for s in SEVERITIES}
    lines = [
        "## Celmis — dependency audit",
        "",
        f"`{target}` · {result.packages_declared} declared packages · "
        + (" · ".join(f"**{counts[s]}** {s}" for s in SEVERITIES
                      if counts.get(s)) or "no known vulnerabilities"),
        "",
    ]
    gaps = result.not_checked()
    if gaps:
        lines += [
            f"**Not fully checked ({len(gaps)}).** Treat a zero for these as "
            "*unknown*, not as *safe*.",
            "",
        ]
        for c in gaps[:20]:
            where = c.get("subproject") or "."
            reason = " ".join(str(c.get("reason") or "").split())[:200]
            lines.append(
                f"- `{where}` — {c.get('ecosystem', '?')} via "
                f"{c.get('tool', '?')}: {reason or c.get('status', '?')}"
            )
        lines.append("")
    if not vulnerable:
        lines += ["No known vulnerabilities in what was audited.", ""]
        return "\n".join(lines)

    lines += ["| Severity | Package | Installed | Fixed in | Advisory |",
              "| --- | --- | --- | --- | --- |"]
    for f in vulnerable[:100]:
        fixed = f.fixed_in or "_no fix yet_"
        ident = f"[{f.vuln_id}]({f.url})" if f.url else f.vuln_id
        lines.append(
            f"| {f.severity} | `{f.package}` | {f.version or '?'} | {fixed} | {ident} |"
        )
    if len(vulnerable) > 100:
        lines.append("")
        lines.append(f"_…and {len(vulnerable) - 100} more._")
    lines.append("")
    return "\n".join(lines)


def to_json(result: CiResult, *, target: str) -> str:
    from dataclasses import asdict
    return json.dumps({
        "target": target,
        "packages_declared": result.packages_declared,
        "findings": [asdict(f) for f in result.findings if f.severity != "none"],
        "not_checked": result.not_checked(),
    }, indent=2)


# ─── Entry point ─────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """`python -m src.deps.ci <path> [--fail-on high] [--format md|json]`.

    argparse rather than typer: this must start inside a CI image that
    installs the audit extra and nothing else.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="celmis-audit",
        description="Audit a checkout's dependencies for known vulnerabilities.",
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="directory to audit (default: the current one)")
    parser.add_argument("--fail-on", default="high", choices=[*SEVERITIES, "never"],
                        help="exit non-zero when a finding at or above this "
                             "severity exists (default: high)")
    parser.add_argument("--format", default="md", choices=["md", "json"])
    parser.add_argument("--output", default="", help="write to this file too")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    directory = Path(args.path).resolve()
    if not directory.is_dir():
        print(f"not a directory: {directory}", file=sys.stderr)
        return 2

    result = audit_path(directory, timeout=args.timeout)
    text = (to_markdown(result, target=directory.name) if args.format == "md"
            else to_json(result, target=directory.name))
    print(text)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")

    if args.fail_on == "never":
        return 0
    hits = result.count_at_or_above(args.fail_on)
    if hits:
        print(f"\n{hits} finding(s) at or above '{args.fail_on}'.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
