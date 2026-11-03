"""Breaking-change detector (Stage 15).

Runs after the main review pipeline, in three steps and with no model call:

  1. `contract_change.detect` pairs the old and the new definition of every
     public function, method, class and HTTP route of the pull request (the
     files at its base and head commits when the provider can read them, the
     diff otherwise) and keeps what a caller could no longer do: a removal, a
     removed / renamed / newly required parameter, a changed route method or
     path. A body-only change is not a contract change and is never reported.
  2. `groups.impact` looks the consumers of each up in the OTHER repositories
     of the pull request's workspace: HTTP calls that reach the route, and
     imports of the package that publishes the symbol with the calls of its
     names. Nothing is matched on a bare function name.
  3. A finding per change. Its severity follows the STRONG consumers only; a
     change whose consumers are all weak is reported as information and never
     escalates, notifies, or fails the `celmis/breaking-change` status.

Every consumer named in a finding or a notification comes from
`tenancy.within_workspace`: under multi_tenant another tenant's repositories
are neither read nor named.
"""

from __future__ import annotations

import logging
import time

from src.groups import impact as impact_mod
from src.review.agents.base import AgentContext, AgentRunResult
from src.review.contract_change import ContractChange, detect
from src.review.models import Finding, FindingSeverity, HunkSide, TemplateText

logger = logging.getLogger(__name__)

#: `Finding.rule_id` prefixes the gate reads: a contract change with strong
#: consumers is `bc.strong.*`, anything else `bc.weak.*` / `bc.none.*`.
STRONG_RULE = "bc.strong."
MAX_LISTED = 10


def severity_for(strong: int) -> FindingSeverity:
    return (
        FindingSeverity.CRITICAL if strong >= 5
        else FindingSeverity.ERROR if strong >= 2
        else FindingSeverity.WARNING
    )


def _consumers_of(change: ContractChange, context: AgentContext, owner_slug: str,
                  view: impact_mod.WorkspaceView) -> impact_mod.Impact:
    if change.is_route:
        assert change.route is not None
        return impact_mod.consumers_of_route(
            workspace_id=context.workspace_id, owner_slug=owner_slug,
            method=change.route[0], path=change.route[1], view=view)
    return impact_mod.consumers_of_symbols(
        workspace_id=context.workspace_id, owner_slug=owner_slug,
        file=change.file, names=change.lookup_names(), view=view)


def incomplete_reason(coverage: dict[str, int] | None) -> str:
    """One sentence on what the check could not consult, or "" when it saw
    everything it needed. Only meaningful when a contract change was found."""
    if not coverage or not coverage.get("changes"):
        return ""
    parts = []
    if coverage.get("unchecked"):
        n = coverage["unchecked"]
        parts.append(f"{n} repositor{'y' if n == 1 else 'ies'} not checked")
    if coverage.get("owner_missing"):
        parts.append("this repository has no recorded facts")
    if coverage.get("failures"):
        parts.append(f"{coverage['failures']} lookup(s) failed")
    if coverage.get("partial"):
        parts.append("some files were compared from the diff alone")
    return "; ".join(parts)


def _line_for(change: ContractChange, pr) -> tuple[str, int]:  # noqa: ANN001
    """A (file, line) the provider can anchor: the changed definition when it
    sits inside a hunk of the new side, else the file's first changed line."""
    for h in getattr(pr, "anchor_hunks", None) or pr.hunks:
        if h.file_path != change.file:
            continue
        if h.new_start <= change.line < h.new_start + max(h.new_count, 1):
            return change.file, change.line
    for h in getattr(pr, "anchor_hunks", None) or pr.hunks:
        if h.file_path == change.file or h.old_file_path == change.file:
            return h.file_path, max(h.new_start, 1)
    return (pr.changed_files[0] if pr.changed_files else change.file), 1


def _consumer_line(c: impact_mod.Consumer) -> str:
    mark = "" if c.strong else " (weak)"
    link = "http" if c.kind == "http" else "import"
    return f"  - `{c.repo_slug}` · `{c.file}:{c.line}` · {link}{mark} — {c.detail}"


def run_breaking_change(context: AgentContext) -> AgentRunResult:
    t0 = time.time()
    pr = context.pull_request
    notes: list[str] = []
    try:
        changes = detect(pr, getattr(context, "file_provider", None), notes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("breaking_change_detect_failed err=%s", exc)
        return AgentRunResult(agent="breaking_change", error=type(exc).__name__,
                              elapsed_seconds=time.time() - t0)
    if not changes:
        return AgentRunResult(agent="breaking_change", elapsed_seconds=time.time() - t0)

    findings: list[Finding] = []
    unknown_note = ""
    failures = 0
    try:  # the facts of the workspace are read ONCE per review
        view = impact_mod.workspace_view(context.workspace_id, pr.local_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("breaking_change_view_failed err=%s", exc)
        view = impact_mod.WorkspaceView(owner_missing=True)
        failures = len(changes)
        changes_to_look_up: list[ContractChange] = []
    else:
        changes_to_look_up = changes
    for change in changes_to_look_up:
        try:
            result = _consumers_of(change, context, pr.local_slug, view)
        except Exception as exc:  # noqa: BLE001
            logger.warning("breaking_change_lookup_failed symbol=%s err=%s", change.symbol, exc)
            failures += 1
            continue
        if result.unknown and not unknown_note:
            unknown_note = (f"\n\n{len(result.unknown)} repositor"
                            f"{'y' if len(result.unknown) == 1 else 'ies'} of the workspace "
                            "were not checked: indexed before cross-repository facts existed "
                            "(reindex to include them).")
        if not result.consumers:
            continue
        strong = result.strong
        n = len(strong)
        severity = severity_for(n) if n else FindingSeverity.INFO
        file_path, line = _line_for(change, pr)
        listed = "\n".join(_consumer_line(c) for c in (strong + [c for c in result.consumers if not c.strong])[:MAX_LISTED])
        kind = "route" if change.is_route else "function"
        title = f"Breaking change risk: {change.symbol} — {n} consumer(s)" if n else \
            f"Possible breaking change: {change.symbol} — {len(result.consumers)} possible consumer(s)"
        body = (
            f"Contract change touches `{kind}` **{change.symbol}** ({change.detail}). "
            + (f"{n} consumer(s) detected across other repositories:" if n else
               "Only weak links were found (a matching path or name without a proven target); "
               "this does not block the merge:")
            + f"\n\n{listed}{unknown_note}"
        )
        findings.append(Finding(
            file_path=file_path, line=line, side=HunkSide.RIGHT,
            severity=severity, title=title, body=body,
            agent="breaking_change",
            rule_id=f"{STRONG_RULE if n else 'bc.weak.'}{change.kind}.{change.symbol}",
            confidence=0.9 if n else 0.5,
            evidence_kind="proven",
            template=TemplateText(
                key="breaking_change.risk", title=title, body=body,
                args={"name": change.symbol, "n": n, "kind": kind,
                      "detail": change.detail, "consumers": listed,
                      "unknown": len(result.unknown) if result.unknown else 0}),
        ))

    coverage = {"changes": len(changes), "unchecked": len(view.unknown), "failures": failures,
                "owner_missing": int(view.owner_missing), "partial": int(bool(notes))}
    reason = incomplete_reason(coverage)
    if reason:
        file_path, line = _line_for(changes[0], pr)
        body = (f"A contract change was found, but the check could not look everywhere: "
                f"{reason}. Absence of consumers below is not proof there are none. "
                "Reindex the repositories (\"Index all\") so their routes, calls and imports are known."
                + ("\n\n" + "\n".join(notes) if notes else ""))
        findings.append(Finding(
            file_path=file_path, line=line, side=HunkSide.RIGHT,
            severity=FindingSeverity.INFO, title=f"Breaking-change check incomplete: {reason}",
            body=body, agent="breaking_change", rule_id="bc.unchecked", confidence=0.5,
            evidence_kind="proven"))

    strong_findings = [f for f in findings if f.rule_id.startswith(STRONG_RULE)]
    if strong_findings:
        try:
            from src.notifications import notify
            notify(
                workspace_id=context.workspace_id,
                event="breaking_change", repo_slug=pr.repo,
                title=f"Breaking change risk on PR #{pr.number} — {pr.title}",
                body_md="\n\n".join(f"**{f.title}**\n{f.body}" for f in strong_findings[:5]),
                severity="error" if any(f.severity == FindingSeverity.CRITICAL for f in strong_findings) else "warn",
                link_url=pr.url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("bc_notify_failed err=%s", exc)

    return AgentRunResult(agent="breaking_change", findings=findings, coverage=coverage,
                          elapsed_seconds=time.time() - t0)


def has_strong_consumers(findings: list[Finding]) -> bool:
    """Whether any breaking-change finding names a strong consumer: the
    question the `celmis/breaking-change` status answers."""
    return any(f.agent == "breaking_change" and f.rule_id.startswith(STRONG_RULE) for f in findings)


__all__ = ["run_breaking_change", "has_strong_consumers", "severity_for"]
