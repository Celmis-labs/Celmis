"""Breaking-change detector (Stage 15).

Runs after the main review pipeline. Two-stage:

  1. Cheap parse of the diff for **signature changes** on public
     functions/exports/routes — regex on `def NAME(...)`, `function NAME`,
     `export function NAME`, `@app.get("/…")`, etc. Zero LLM cost.
  2. For each suspected changed public symbol, call the legacy graph
     tool `find_callers` across every OTHER indexed repo. If N > 0, we
     emit a Finding tagged agent="breaking_change" whose severity scales
     with consumer count.

The agent also NOTIFIES via the Grafana-style channel routing so
consumers can react before the change lands.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from src.review.agents.base import AgentContext, AgentRunResult
from src.review.models import Finding, FindingSeverity, HunkSide

logger = logging.getLogger(__name__)


# Regexes for "signature-like line" — high recall, low precision. We
# tolerate FPs because the impact score (consumer count) filters them.
_PATTERNS = (
    # Python
    re.compile(r"^\+\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", re.M),
    re.compile(r"^\-\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", re.M),
    # JS/TS
    re.compile(r"^\+\s*(?:export\s+)?function\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.M),
    re.compile(r"^\+\s*export\s+(?:const|let|async\s+function)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.M),
    # HTTP routers
    re.compile(r"^\+.*@(?:app|router)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]", re.M),
    re.compile(r"^\+.*(?:app|router)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]", re.M),
)


@dataclass
class ChangedSymbol:
    kind: str    # 'function' | 'route'
    name: str
    detail: str = ""


def _extract_symbols(diff_text: str) -> list[ChangedSymbol]:
    if not diff_text:
        return []
    symbols: list[ChangedSymbol] = []
    seen: set[str] = set()
    for pat in _PATTERNS:
        for m in pat.finditer(diff_text):
            g = m.groups()
            if len(g) == 1:
                name = g[0]
                if name in seen:
                    continue
                symbols.append(ChangedSymbol(kind="function", name=name))
                seen.add(name)
            elif len(g) == 2:
                method, path = g
                key = f"{method.upper()} {path}"
                if key in seen:
                    continue
                symbols.append(ChangedSymbol(kind="route", name=path, detail=method.upper()))
                seen.add(key)
    return symbols[:40]


def run_breaking_change(context: AgentContext) -> AgentRunResult:
    import time
    t0 = time.time()
    pr = context.pull_request
    diff = pr.raw_diff or ""
    symbols = _extract_symbols(diff)
    if not symbols:
        return AgentRunResult(agent="breaking_change", elapsed_seconds=0.0)

    # THE PROVIDER PATH IS NOT THE SLUG THE OTHER REPOS ARE LISTED UNDER.
    # This passed `pr.repo` ("acme/api") to be compared against
    # `list_repos().slug` ("github_acme-api"), so the comparison never matched
    # and THE PULL REQUEST'S OWN REPOSITORY WAS NEVER EXCLUDED.
    #
    # It was harmless only because the search underneath it was broken too:
    # `find_callers` was being handed a bare symbol name where it matches on
    # `b.id`, so it returned nothing for everything. Fixing that armed this —
    # a helper called five times inside its own file would be reported as
    # "5 consumers across other repositories" and escalate to CRITICAL,
    # with a notification, on a pull request that broke nothing.
    consumers_by_symbol = _find_consumers_across_repos(pr.local_slug, symbols)
    findings: list[Finding] = []
    for sym in symbols:
        consumers = consumers_by_symbol.get(sym.name, [])
        if not consumers:
            continue
        n = len(consumers)
        severity = (
            FindingSeverity.CRITICAL if n >= 5
            else FindingSeverity.ERROR if n >= 2
            else FindingSeverity.WARNING
        )
        first_file = pr.changed_files[0] if pr.changed_files else ""
        consumer_lines = "\n".join(
            f"  - `{c['repo_slug']}` · `{c.get('file','?')}:{c.get('line','?')}` — {c.get('caller','?')}"
            for c in consumers[:10]
        )
        findings.append(Finding(
            file_path=first_file, line=1, side=HunkSide.RIGHT,
            severity=severity,
            title=f"Breaking change risk: {sym.name} — {n} consumer(s)",
            body=(
                f"Signature change touches `{sym.kind}` **{sym.name}** "
                f"({sym.detail}). {n} consumer(s) detected across other "
                f"repositories:\n\n{consumer_lines}"
            ),
            agent="breaking_change", rule_id=f"bc.{sym.name}", confidence=0.75,
        ))

    # Fire notifications only if we actually surfaced anything.
    if findings:
        try:
            from src.notifications import notify
            notify(
                workspace_id=context.workspace_id,
                event="breaking_change", repo_slug=pr.repo,
                title=f"Breaking change risk on PR #{pr.number} — {pr.title}",
                body_md="\n\n".join(f"**{f.title}**\n{f.body}" for f in findings[:5]),
                severity="error" if any(f.severity == FindingSeverity.CRITICAL for f in findings) else "warn",
                link_url=pr.url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bc_notify_failed err=%s", exc)

    return AgentRunResult(
        agent="breaking_change", findings=findings,
        elapsed_seconds=time.time() - t0,
    )


def _find_consumers_across_repos(
    current_repo: str, symbols: list[ChangedSymbol],
) -> dict[str, list[dict]]:
    from src.mcp_server import tools as legacy
    consumers: dict[str, list[dict]] = {}
    try:
        repos = legacy.list_repos()
    except Exception:  # noqa: BLE001
        return consumers
    other = [r for r in repos if r.slug != current_repo]
    if len(other) == len(repos):
        # Nothing was excluded, which means the caller handed an address this
        # listing is not keyed on. Better to find no consumers than to report
        # the repository's own as somebody else's.
        logger.warning("breaking_change_own_repo_not_excluded repo=%r", current_repo)
    for sym in symbols:
        found: list[dict] = []
        for r in other:
            try:
                # find_callers → {..., "callers": [dict]}; caller dicts
                # carry name/file/start_line keys.
                res = legacy.find_callers(symbol_id=sym.name, repo_slug=r.slug)
                callers = (res or {}).get("callers", []) if isinstance(res, dict) else []
            except Exception:  # noqa: BLE001
                continue
            for c in callers:
                found.append({
                    "repo_slug": r.slug,
                    "caller": c.get("name"),
                    "file": c.get("file", ""),
                    "line": c.get("start_line", 0),
                })
        if found:
            consumers[sym.name] = found
    return consumers


__all__ = ["run_breaking_change"]
