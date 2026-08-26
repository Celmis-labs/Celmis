"""The dependency audit as something you can run without deploying Celmis.

Everything Celmis does well needs a server — the cross-repo graph, ownership,
history, Q&A. The dependency audit does not: it reads manifests off disk,
shells out to each ecosystem's own auditor, and asks OSV.dev. That makes it the
one capability that can run in somebody's CI before they have heard of the
product, which is a far cheaper first touch than "stand up a multi-tenant
stack".

These tests hold the properties that make that true, because they are easy to
lose by accident: the module stays free of server imports, the output tells the
truth about coverage gaps, and the CI image never lets pip execute a
contributor's code.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.deps.ci import (
    SEVERITIES,
    CiFinding,
    CiResult,
    _absorb,
    to_json,
    to_markdown,
)

ROOT = Path(__file__).resolve().parents[2]


def _f(**kw):
    base = dict(ecosystem="PyPI", package="django", version="4.2.0",
                severity="high", vuln_id="GHSA-x", fixed_in="4.2.28",
                summary="s", url="https://osv.dev/x")
    base.update(kw)
    return CiFinding(**base)


# ─── it must not drag the server in ──────────────────────────────────


def test_the_module_imports_nothing_that_needs_a_deployment():
    """A single `from src.db...` at module scope turns a 30 MB CI image into a
    Postgres client that cannot start without a DATABASE_URL."""
    tree = ast.parse((ROOT / "src" / "deps" / "ci.py").read_text())
    banned = ("src.db", "src.api", "sqlalchemy", "qdrant", "litellm",
              "src.credentials", "src.config")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            mod = node.module or ""
            if any(mod.startswith(b) for b in banned):
                offenders.append(f"line {node.lineno}: from {mod}")
        elif isinstance(node, ast.Import) and node.col_offset == 0:
            for alias in node.names:
                if any(alias.name.startswith(b) for b in banned):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, f"server-only imports at module scope: {offenders}"


def test_it_does_not_reach_for_a_model():
    source = (ROOT / "src" / "deps" / "ci.py").read_text()
    for word in ("generate(", "litellm", "gemini", "resolve_profile"):
        assert word not in source, f"{word} — this path must cost nothing"


# ─── the same advisory twice is one finding ──────────────────────────


def test_two_tools_reporting_one_advisory_are_merged():
    """Native auditors and osv-scanner routinely both see a CVE. Without this
    the comment listed `ecdsa` PYSEC-2026-1325 twice and the fail-on count
    double-counted it — observed on a real repository."""
    from types import SimpleNamespace

    result = CiResult()
    dup = SimpleNamespace(
        ecosystem="PyPI", package="ecdsa", version="0.19.2", severity="medium",
        vuln_id="PYSEC-2026-1325", fixed_in=None, summary="s", url="u",
        subproject="", source="pip-audit")
    _absorb(result, SimpleNamespace(findings=[dup, dup], checks=[]),
            default_source="native")
    _absorb(result, SimpleNamespace(findings=[dup], checks=[]),
            default_source="osv-scanner")
    assert len(result.findings) == 1


def test_the_same_package_at_two_versions_is_two_findings():
    """Merging on name alone would hide a vulnerable copy in a subproject."""
    from types import SimpleNamespace

    result = CiResult()
    mk = lambda v: SimpleNamespace(  # noqa: E731
        ecosystem="npm", package="lodash", version=v, severity="high",
        vuln_id="GHSA-y", fixed_in="4.17.21", summary="", url="",
        subproject="", source="npm-audit")
    _absorb(result, SimpleNamespace(findings=[mk("4.17.11"), mk("3.10.1")],
                                    checks=[]), default_source="native")
    assert len(result.findings) == 2


# ─── the output tells the truth ──────────────────────────────────────


def test_coverage_gaps_come_before_the_findings():
    """A zero from an ecosystem nobody audited looks exactly like a clean one.
    A reader who meets the table first has already formed an impression."""
    result = CiResult(
        findings=[_f()],
        checks=[{"ecosystem": "npm", "tool": "npm-audit", "subproject": "web",
                 "status": "not_checked", "reason": "no lock file"}])
    md = to_markdown(result, target="acme")
    assert md.index("Not fully checked") < md.index("| Severity |")
    assert "no lock file" in md and "web" in md


def test_a_clean_audit_says_what_it_actually_means():
    md = to_markdown(CiResult(packages_declared=12), target="acme")
    assert "No known vulnerabilities in what was audited" in md
    assert "| Severity |" not in md


def test_a_finding_with_no_fix_is_not_shown_as_fixable():
    md = to_markdown(CiResult(findings=[_f(fixed_in=None)]), target="acme")
    assert "_no fix yet_" in md


def test_the_table_is_capped_and_says_so():
    rows = [_f(package=f"p{i}", vuln_id=f"GHSA-{i}") for i in range(140)]
    md = to_markdown(CiResult(findings=rows), target="acme")
    assert "and 40 more" in md


def test_json_output_carries_the_gaps_too():
    import json

    result = CiResult(
        findings=[_f()],
        checks=[{"ecosystem": "Go", "tool": "govulncheck", "subproject": "",
                 "status": "not_checked", "reason": "binary missing"}])
    payload = json.loads(to_json(result, target="acme"))
    assert payload["not_checked"], "a machine consumer must see the gaps"
    assert payload["findings"][0]["package"] == "django"


# ─── the exit code ───────────────────────────────────────────────────


def test_the_threshold_counts_severity_at_or_above():
    result = CiResult(findings=[_f(severity="medium"), _f(severity="critical",
                                                          package="x")])
    assert result.count_at_or_above("critical") == 1
    assert result.count_at_or_above("medium") == 2
    assert result.count_at_or_above("low") == 2


def test_severities_are_ordered_worst_first():
    """The threshold is a prefix of this list, so the order is load-bearing."""
    assert SEVERITIES[0] == "critical"
    assert SEVERITIES[-1] == "none"


# ─── the CI image must not execute a contributor's code ──────────────


def test_the_image_disables_pip_execution():
    """`pip-audit -r` resolves by running `pip install --dry-run`, which
    evaluates a source distribution's build metadata. On a server auditing a
    repository the operator chose that is a considered risk; on a runner
    processing an outside contributor's pull request it is arbitrary code
    execution."""
    dockerfile = (ROOT / "Dockerfile.ci").read_text()
    assert "DEPS_PIP_AUDIT_NO_EXEC=1" in dockerfile
    assert "DEPS_PIP_AUDIT_PROJECT=0" in dockerfile


def test_the_no_exec_switch_is_wired_to_the_pinned_only_path():
    source = (ROOT / "src" / "deps" / "native.py").read_text()
    assert "PIP_AUDIT_NO_EXEC" in source
    idx = source.find("if PIP_AUDIT_NO_EXEC:")
    assert idx > 0, "the switch is declared but never read"
    block = source[idx:idx + 900]
    assert "_pip_audit_pinned_only(" in block
    # And it must report the reduced coverage rather than pass it off as full.
    assert '"partial"' in block


def test_the_image_carries_the_parsers_that_would_silently_return_zero():
    """locks.py parses pnpm and yarn-berry lock files inside
    `except Exception: return []`. Without PyYAML those ecosystems report zero
    packages — the exact silent zero this audit exists to prevent."""
    dockerfile = (ROOT / "Dockerfile.ci").read_text()
    assert "PyYAML" in dockerfile
    assert "httpx" in dockerfile


def test_the_image_ships_the_auditors_not_just_the_python():
    dockerfile = (ROOT / "Dockerfile.ci").read_text()
    for tool in ("osv-scanner", "pip-audit", "nodejs"):
        assert tool in dockerfile, f"{tool} missing — its ecosystem goes unaudited"
    assert "sha256sum -c" in dockerfile, "the downloaded binary is unverified"


def test_the_action_applies_the_threshold_after_reporting():
    """A non-zero exit that loses the report is a worse product than a failing
    build with an explanation."""
    action = (ROOT / ".github" / "actions" / "dependency-audit"
              / "action.yml").read_text()
    report = action.find("GITHUB_STEP_SUMMARY")
    gate = action.find("Apply the threshold")
    assert 0 < report < gate
    assert "fail-on" in action
