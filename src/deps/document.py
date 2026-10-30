"""The audit as a document someone can send to a colleague.

The dependencies page holds everything a reader needs across four collapsible
panels in a browser session. A person who has to hand the result to a security
reviewer, a client or a ticket has no way to take it with them — and the first
exports did not help: one flat list of bullets per repository, 58 pages when
printed, the answer buried inside.

`build_report` assembles the run into a short list of blocks (`src.docs.blocks`)
and the three exporters render that one list: markdown (`build_markdown`),
Word (`src.docs.docx_blocks`) and PDF (`src.docs.pdf_blocks`).

Order is deliberate. A reader gets the verdict, then the action, then the
caveats, then the evidence:

  1. summary           — headline numbers, the AI analysis when there is one
  2. what to do now    — fixable vulnerabilities, grouped across repositories
  3. coverage          — what was checked, how, and what nobody checked. BEFORE
                         the findings: an unchecked ecosystem reports zero
                         vulnerabilities exactly like a clean one, and a reader
                         who meets the findings first has formed an impression
                         by the time the caveat arrives
  4. findings          — vulnerable packages per repository, worst repository first
  5. appendices        — outdated-but-not-vulnerable, range and chain notes,
                         supply-chain hygiene
"""

from __future__ import annotations

from typing import Any

from src.docs.blocks import (
    Block,
    Bullets,
    Heading,
    PageBreak,
    Para,
    Stat,
    Stats,
    Table,
    Title,
    parse_markdown,
    to_markdown_text,
)

#: Every exported artifact says where it came from. A report that lands in a
#: ticket without attribution gets treated as somebody's hand-written opinion.
PRODUCT = "Celmis"

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
_GAP_RANK = {"major": 0, "minor": 1, "patch": 2}

#: Past this the appendix stops being readable and becomes a data dump. The
#: count of what was dropped is printed rather than silently truncated.
MAX_CLEAN_ROWS = 250
MAX_ACTION_ROWS = 25
MAX_NOTE_ROWS = 20

_STATUS_LABEL = {
    "checked": "Audited by its native tool",
    "covered_by_osv": "Lock file read, audited through OSV.dev",
    "no_manifests": "Nothing to check",
    "partial": "Direct dependencies only (no transitive tree)",
    "failed": "Tool failed",
    "not_checked": "Not checked",
}
_PROVENANCE_LABEL = {
    "pin": "Pinned in the manifest",
    "lock": "Read from a lock file",
    "resolved": "Resolved by the package tool",
    "range": "Only a range — floor shown, installed version unknown",
    "reported": "Reported by the auditor",
}
_SRC_SHORT = {"pin": "pin", "lock": "lock", "resolved": "resolved",
              "range": "range", "reported": "reported"}


def _src(r: Any) -> str:
    return _SRC_SHORT.get(getattr(r, "version_source", None) or "", "")


def _fmt_int(value: Any) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value or 0)


def _severity_line(by_severity: dict[str, int]) -> str:
    parts = [f"{by_severity.get(k, 0)} {k}"
             for k in ("critical", "high", "medium", "low")
             if by_severity.get(k)]
    return " · ".join(parts) if parts else "none"


def _sev(r: Any) -> str:
    return str(getattr(r, "severity", "none") or "none")


def _sev_cell(r: Any) -> str:
    s = _sev(r)
    return s.upper() if s != "none" else ""


def _fixed_in(vulns: list[dict] | None) -> str:
    """Where the fix is — the only part of an advisory that is actionable."""
    if not vulns:
        return ""
    fixes: list[str] = []
    for v in vulns:
        f = v.get("fixed_in")
        if f and str(f) not in fixes:
            fixes.append(str(f))
    return ", ".join(fixes[:2]) + (f" +{len(fixes) - 2}" if len(fixes) > 2 else "") \
        if fixes else "no fix yet"


def _advisories(vulns: list[dict] | None, limit: int = 3) -> str:
    ids = [str(v.get("cve") or v.get("id") or "?") for v in (vulns or [])]
    out = ", ".join(ids[:limit])
    return out + (f" +{len(ids) - limit}" if len(ids) > limit else "")


def _vuln_bits(vulns: list[dict], limit: int = 4) -> str:
    """CVE ids with where the fix is — kept for callers that want one string."""
    out = []
    for v in (vulns or [])[:limit]:
        ident = v.get("cve") or v.get("id") or "?"
        fixed = v.get("fixed_in")
        out.append(f"{ident} ({'fixed in ' + fixed if fixed else 'no fix yet'})")
    extra = len(vulns or []) - limit
    if extra > 0:
        out.append(f"+{extra} more")
    return "; ".join(out)


def _current(r: Any) -> str:
    cur = str(getattr(r, "current_version", "") or "?")
    return f"≥ {cur}" if getattr(r, "version_source", "") == "range" else cur


def _pkg(r: Any) -> str:
    return f"{r.package}" + (" (dev)" if getattr(r, "is_dev", False) else "")


def _short(text: str, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:(") + "…"


def _where(item: dict) -> str:
    where = str(item.get("repo", "?"))
    if item.get("subproject"):
        where = f"{where}/{item['subproject']}"
    return where


def _demote(blocks: list[Block], to: int) -> list[Block]:
    """Shift headings so the shallowest one sits at level `to` (max 4)."""
    levels = [b.level for b in blocks if isinstance(b, Heading)]
    if not levels or min(levels) >= to:
        return blocks
    shift = to - min(levels)
    return [Heading(min(b.level + shift, 4), b.text) if isinstance(b, Heading) else b
            for b in blocks]


# ─── sections ────────────────────────────────────────────────────────

def _summary(summary: dict[str, Any]) -> list[Block]:
    by_sev = summary.get("by_severity") or {}
    groups = summary.get("groups") or {}
    vulnerable = summary.get("vulnerable") or 0
    urgent = (by_sev.get("critical") or 0) + (by_sev.get("high") or 0)
    update_now = groups.get("update_now") or 0
    out: list[Block] = [
        Heading(2, "Summary"),
        Stats([
            Stat("repositories scanned",
                 f"{_fmt_int(summary.get('repos_scanned'))} of {_fmt_int(summary.get('repos_total'))}"),
            Stat("packages", _fmt_int(summary.get("packages"))),
            Stat("vulnerable", _fmt_int(vulnerable), "bad" if vulnerable else "good"),
            Stat("critical + high", _fmt_int(urgent), "bad" if urgent else "good"),
            Stat("outdated", _fmt_int(summary.get("outdated")), "warn"),
            Stat("update now", _fmt_int(update_now), "bad" if update_now else "good"),
        ]),
        Para(f"**Vulnerable by severity:** {_severity_line(by_sev)}. "
             f"**Packages:** {_fmt_int(summary.get('packages'))} "
             f"({_fmt_int(summary.get('unique_packages'))} unique, "
             f"{_fmt_int(summary.get('transitive'))} transitive)."),
    ]
    if groups:
        out.append(Para(
            f"**Recommended:** {_fmt_int(groups.get('update_now'))} update now, "
            f"{_fmt_int(groups.get('update_safe'))} safe to batch, "
            f"{_fmt_int(groups.get('plan_major'))} major upgrades to plan."))
    skipped = summary.get("repos_skipped") or []
    if skipped:
        out.append(Para(f"**Repositories skipped:** {len(skipped)} — "
                        + ", ".join(str(s) for s in skipped[:10])
                        + (f" and {len(skipped) - 10} more" if len(skipped) > 10 else "")))
    if summary.get("vuln_check_errors"):
        out.append(Para(
            f"OSV.dev could not be reached for {summary['vuln_check_errors']} batch(es): "
            "a zero below may mean unknown, not safe.", "callout"))
    return out


def _executive(summary: dict[str, Any]) -> list[Block]:
    report = (summary.get("ai_report") or "").strip()
    if report:
        engine = summary.get("ai_report_engine") or "?"
        return [
            Heading(2, "Executive summary"),
            Para(f"Written by {PRODUCT} using {engine} over the deterministic audit "
                 "facts below. The facts are measured; only the analysis is generated.",
                 "note"),
            *_demote(parse_markdown(report), 3),
        ]
    if summary.get("ai_report_error"):
        return [Heading(2, "Executive summary"),
                Para(f"Not generated: {summary['ai_report_error']}", "note")]
    return []


def _actions(findings: list[Any], summary: dict[str, Any]) -> list[Block]:
    out: list[Block] = [Heading(2, "What to do now")]
    vuln = [r for r in findings if _sev(r) != "none"]
    if not vuln:
        out.append(Para("No known vulnerabilities were found in the packages that "
                        "were audited. See Coverage for what that does and does not cover."))
    # One line per package version, however many repositories carry it: the fix
    # is the same bump, and listing it once per repository is how the old
    # export reached 58 pages.
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for r in vuln:
        grouped.setdefault((r.ecosystem, r.package, str(r.current_version)), []).append(r)

    def sort_key(item: tuple[tuple[str, str, str], list[Any]]) -> tuple[int, int, str]:
        rows = item[1]
        return (min(_SEV_RANK.get(_sev(r), 5) for r in rows), -len(rows), item[0][1])

    fixable: list[tuple[tuple[str, str, str], list[Any], str]] = []
    unfixable: list[tuple[tuple[str, str, str], list[Any], str]] = []
    for key, rows in sorted(grouped.items(), key=sort_key):
        fixes = _fixed_in([v for r in rows for v in (r.vulns or [])])
        (unfixable if fixes in ("", "no fix yet") else fixable).append((key, rows, fixes))

    def table(items: list, headers: list[str], with_fix: bool) -> Table:
        rows_out = []
        for (_eco, pkg, cur), rows, fixes in items[:MAX_ACTION_ROWS]:
            worst = min(rows, key=lambda r: _SEV_RANK.get(_sev(r), 5))
            repos = sorted({r.repo_slug for r in rows})
            where = repos[0] if len(repos) == 1 else f"{len(repos)} repositories"
            row = [pkg, cur]
            if with_fix:
                row.append(fixes)
            row += [_sev_cell(worst), _advisories([v for r in rows for v in (r.vulns or [])], 2),
                    where]
            rows_out.append(row)
        widths = [3.0, 1.6, 1.6, 1.4, 3.4, 2.4] if with_fix else [3.2, 1.6, 1.4, 3.8, 2.4]
        return Table(headers, rows_out, widths, mono=(1, 2) if with_fix else (1,))

    if fixable:
        out += [
            Heading(3, f"Fix available ({len(fixable)})"),
            Para("The fixed version is the lowest release that clears every listed "
                 "advisory for the package.", "note"),
            table(fixable, ["Package", "Current", "Fixed in", "Severity", "Advisories",
                            "Where"], True),
        ]
        if len(fixable) > MAX_ACTION_ROWS:
            out.append(Para(f"{len(fixable) - MAX_ACTION_ROWS} more with a fix are listed "
                            "per repository below.", "note"))
    if unfixable:
        out += [
            Heading(3, f"No fix released yet ({len(unfixable)})"),
            Para("Mitigate, replace or accept explicitly — upgrading will not help yet.",
                 "note"),
            table(unfixable, ["Package", "Current", "Severity", "Advisories", "Where"], False),
        ]
        if len(unfixable) > MAX_ACTION_ROWS:
            out.append(Para(f"{len(unfixable) - MAX_ACTION_ROWS} more without a fix are "
                            "listed per repository below.", "note"))

    notes: list[str] = []
    groups = summary.get("groups") or {}
    if groups.get("plan_major"):
        notes.append(f"{_fmt_int(groups['plan_major'])} major-version upgrades to plan "
                     "(not vulnerable, but the gap grows).")
    if (summary.get("range_hygiene") or {}).get("total"):
        notes.append(f"{_fmt_int(summary['range_hygiene']['total'])} version ranges allow "
                     "a vulnerable release (Appendix B).")
    if (summary.get("npm_chains") or {}).get("total"):
        notes.append(f"{_fmt_int(summary['npm_chains']['total'])} npm package(s) pull in a "
                     "vulnerable dependency; fix it through an override or a newer parent "
                     "(Appendix B).")
    if notes:
        out += [Heading(3, "Also worth planning"), Bullets(notes)]
    return out


def _coverage(summary: dict[str, Any]) -> list[Block]:
    out: list[Block] = [Heading(2, "Coverage & sources")]
    sources = summary.get("sources") or {}
    if sources:
        out.append(Para("**Findings by auditor:** "
                        + ", ".join(f"{k} — {v}" for k, v in sorted(sources.items()))))
    coverage = summary.get("coverage") or {}
    if coverage:
        out.append(Table(
            ["Ecosystem checks", "Count"],
            [[_STATUS_LABEL.get(k, k), _fmt_int(v)]
             for k, v in sorted(coverage.items(), key=lambda kv: -kv[1])],
            [5.0, 1.0]))
    provenance = summary.get("version_provenance") or {}
    if provenance:
        total = sum(provenance.values()) or 1
        out.append(Table(
            ["Where the installed version came from", "Rows", "Share"],
            [[_PROVENANCE_LABEL.get(k, k), _fmt_int(v), f"{v * 100 // total} %"]
             for k, v in sorted(provenance.items(), key=lambda kv: -kv[1])],
            [5.0, 1.0, 1.0]))
        if provenance.get("range"):
            out.append(Para(
                "Rows that carry only a range show its floor (marked ≥) and no vulnerability "
                "count; an advisory that hits only the floor is listed as a range note in "
                "Appendix B instead.", "note"))
    dropped = summary.get("dropped_inapplicable") or {}
    if dropped.get("total"):
        by_source = ", ".join(f"{k} — {v}" for k, v in sorted((dropped.get("by_source") or {}).items()))
        out.append(Para(
            f"**{_fmt_int(dropped['total'])} advisories were not counted** because the "
            "installed version is outside their affected range or at or above every fix"
            + (f" ({by_source})." if by_source else ".")))
    truncated = summary.get("truncated") or {}
    if truncated:
        names = [str(t.get("repo", t)) if isinstance(t, dict) else str(t) for t in truncated]
        out.append(Para(
            f"**{len(names)} repositories were read only in part** (size caps): "
            + ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
            + ".", "callout"))
    not_checked = summary.get("not_checked") or []
    if not_checked:
        partial = sum(1 for i in not_checked if i.get("status") == "partial")
        rest = len(not_checked) - partial
        headline = f"**Not fully checked ({len(not_checked)}).**"
        if partial and rest:
            headline += (f" {partial} saw the direct dependencies but not the transitive "
                         "tree; the rest fell back to lock files and OSV.dev, or were "
                         "skipped entirely.")
        elif partial:
            # No "rest" to describe. Saying there is one invents a second
            # category of gap that this run does not have.
            headline += " These saw the direct dependencies but not the transitive tree."
        else:
            headline += " These fell back to lock files and OSV.dev, or were skipped entirely."
        out += [
            Para(headline + " Treat a zero here as *unknown*, not as *safe*.", "callout"),
            Table(
                ["Repository", "Ecosystem", "Tool", "Reason"],
                [[_where(i), str(i.get("ecosystem", "?")), str(i.get("tool", "?")),
                  _short(str(i.get("reason") or ""))] for i in not_checked],
                [3.2, 1.2, 1.4, 4.2]),
        ]
    else:
        out.append(Para("Every detected ecosystem was audited by its native tool."))
    return out


def _repo_findings(findings: list[Any]) -> tuple[list[Block], list[Any]]:
    """Vulnerable packages per repository, worst repository first."""
    out: list[Block] = [PageBreak(), Heading(2, "Findings by repository")]
    by_repo: dict[str, list[Any]] = {}
    for f in findings:
        by_repo.setdefault(f.repo_slug, []).append(f)
    # Runs audited before version provenance existed carry none: an all-empty
    # column would only look like missing data, so it is left out.
    has_src = any(_src(f) for f in findings)

    def weight(slug: str) -> tuple[int, int, int, str]:
        rows = [r for r in by_repo[slug] if _sev(r) != "none"]
        return (-sum(_sev(r) == "critical" for r in rows),
                -sum(_sev(r) == "high" for r in rows), -len(rows), slug)

    clean: list[str] = []
    for repo in sorted(by_repo, key=weight):
        rows = by_repo[repo]
        vulnerable = sorted(
            (r for r in rows if _sev(r) != "none"),
            key=lambda r: (_SEV_RANK.get(_sev(r), 5), r.package))
        outdated = [r for r in rows if _sev(r) == "none" and r.outdated != "none"]
        if not vulnerable:
            clean.append(repo)
            continue
        table_rows = [[_pkg(r), _current(r), _src(r), _fixed_in(r.vulns),
                       r.latest_version or "—", _sev_cell(r), _advisories(r.vulns)]
                      for r in vulnerable]
        if has_src:
            table = Table(
                ["Package", "Current", "Src", "Fixed in", "Latest", "Severity", "Advisories"],
                table_rows, [2.6, 1.3, 0.9, 1.5, 1.3, 1.2, 2.6], mono=(1, 3, 4))
        else:
            table = Table(
                ["Package", "Current", "Fixed in", "Latest", "Severity", "Advisories"],
                [row[:2] + row[3:] for row in table_rows],
                [2.6, 1.3, 1.5, 1.3, 1.2, 2.6], mono=(1, 2, 3))
        out += [
            Heading(3, repo),
            Para(f"{len(rows)} packages · {len(vulnerable)} vulnerable · "
                 f"{len(outdated)} outdated without a known vulnerability", "note"),
            table,
        ]
    if not by_repo:
        out.append(Para("No packages were recorded for this run."))
    if clean:
        out += [Heading(3, "No known vulnerabilities"),
                Para(", ".join(clean))]
    return out, [r for r in findings if _sev(r) == "none" and r.outdated != "none"]


def _appendix_outdated(outdated: list[Any]) -> list[Block]:
    if not outdated:
        return []
    ordered = sorted(outdated, key=lambda r: (_GAP_RANK.get(r.outdated, 3), r.repo_slug,
                                              r.package))
    shown = ordered[:MAX_CLEAN_ROWS]
    gaps: dict[str, int] = {}
    for r in outdated:
        gaps[r.outdated] = gaps.get(r.outdated, 0) + 1
    out: list[Block] = [
        Heading(2, "Appendix A — Outdated packages without a known vulnerability"),
        Para(f"{len(outdated)} packages: "
             + ", ".join(f"{n} {k}" for k, n in sorted(gaps.items(),
                                                       key=lambda kv: _GAP_RANK.get(kv[0], 3)))
             + ". Major gaps first.", "note"),
        Table(["Repository", "Package", "Current", "Latest", "Gap"],
              [[r.repo_slug, _pkg(r), _current(r), r.latest_version or "—", r.outdated]
               for r in shown],
              [3.0, 3.0, 1.4, 1.4, 1.0], mono=(2, 3)),
    ]
    dropped = len(outdated) - len(shown)
    if dropped:
        out.append(Para(
            f"{dropped} further outdated packages with no known vulnerability were omitted "
            "to keep this document readable — they are all listed in the app.", "note"))
    return out


def _appendix_notes(summary: dict[str, Any]) -> list[Block]:
    out: list[Block] = []
    ranges = summary.get("range_hygiene") or {}
    chains = summary.get("npm_chains") or {}
    if ranges.get("total") or chains.get("total"):
        out.append(Heading(2, "Appendix B — Range and dependency-chain notes"))
        out.append(Para("Kept apart from the vulnerability counts: they say a manifest "
                        "*allows* a vulnerable release, or that a package pulls one in.",
                        "note"))
    if ranges.get("total"):
        items = ranges.get("items") or []
        out.append(Heading(3, f"Ranges that allow a vulnerable release ({ranges['total']})"))
        out.append(Table(
            ["Repository", "Package", "Range", "Advisories", "Worst"],
            [[str(i.get("repo", "?")), str(i.get("package", "?")), str(i.get("spec", "")),
              str(i.get("advisories", "")), str(i.get("worst") or "").upper()]
             for i in items[:MAX_NOTE_ROWS]],
            [3.0, 2.6, 1.8, 1.0, 1.1], mono=(2,)))
        if ranges["total"] > MAX_NOTE_ROWS:
            out.append(Para(f"{ranges['total'] - MAX_NOTE_ROWS} more in the app.", "note"))
    if chains.get("total"):
        items = chains.get("items") or []
        out.append(Heading(3, f"npm packages that pull in a vulnerable one ({chains['total']})"))
        out.append(Table(
            ["Repository", "Package", "Pulls in", "Fix via", "Severity"],
            [[str(i.get("repo", "?")), str(i.get("package", "?")),
              ", ".join(i.get("pulls_in") or []),
              ", ".join(f"{k} ≥ {v}" for k, v in (i.get("fix_via") or {}).items()) or "—",
              str(i.get("severity") or "").upper()]
             for i in items[:MAX_NOTE_ROWS]],
            [2.6, 2.2, 2.2, 2.0, 1.1]))
        if chains["total"] > MAX_NOTE_ROWS:
            out.append(Para(f"{chains['total'] - MAX_NOTE_ROWS} more in the app.", "note"))
    return out


def _appendix_hygiene(summary: dict[str, Any]) -> list[Block]:
    items = (summary.get("hygiene") or {}).get("items") or []
    if not items:
        return []
    shown = items[:60]
    out: list[Block] = [
        Heading(2, "Appendix C — Supply-chain hygiene"),
        Table(["Kind", "Repository", "Detail"],
              [[str(i.get("kind", "?")), str(i.get("repo", "?")),
                _short(str(i.get("detail", "")), 200)] for i in shown],
              [1.6, 2.4, 5.0]),
    ]
    if len(items) > len(shown):
        out.append(Para(f"{len(items) - len(shown)} more in the app.", "note"))
    return out


# ─── entry points ────────────────────────────────────────────────────

def build_report(
    run: Any,
    findings: list[Any],
    *,
    generated_at: str,
    scope: str = "",
    workspace: str = "",
) -> list[Block]:
    """The run as blocks — the one source the md, docx and pdf exports render."""
    summary: dict[str, Any] = dict(getattr(run, "summary", None) or {})
    meta = ([("Workspace", workspace)] if workspace else []) + [
        ("Generated", f"{generated_at} by {PRODUCT}"),
        ("Run", f"`{getattr(run, 'id', '')}`"),
    ]
    if scope:
        meta.append(("Scope", scope))
    blocks: list[Block] = [
        Title(f"{PRODUCT} — Dependency Audit", meta),
        *_summary(summary),
        *_executive(summary),
        *_actions(findings, summary),
        *_coverage(summary),
    ]
    repo_blocks, outdated = _repo_findings(findings)
    blocks += repo_blocks
    blocks += _appendix_outdated(outdated)
    blocks += _appendix_notes(summary)
    blocks += _appendix_hygiene(summary)
    return blocks


def build_markdown(
    run: Any,
    findings: list[Any],
    *,
    generated_at: str,
    scope: str = "",
    workspace: str = "",
) -> str:
    """The same report as markdown."""
    return to_markdown_text(build_report(
        run, findings, generated_at=generated_at, scope=scope, workspace=workspace))


def footer_label(generated_at: str) -> str:
    return f"{PRODUCT} — Dependency Audit  ·  {generated_at}"


__all__ = ["PRODUCT", "build_markdown", "build_report", "footer_label"]
