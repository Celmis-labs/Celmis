"""What changed since the last audit.

Every audit run was self-contained: a complete picture of now, and no answer
at all to the question somebody actually asks on a Monday — what appeared
since Friday. The findings were there both times; nothing compared them.

That gap has a name in the regulation the evidence pack is built for.
Post-market monitoring under the Cyber Resilience Act means continuity: not
"here is the state today" but "here is what changed, and here is when we
learned it". A stack of independent snapshots technically contains that
answer and does not give it to anybody.

No new storage. Two runs' findings are already rows keyed by (repo, package,
version, vulnerability id); the delta is a set difference over the identity
below. Deliberately computed rather than recorded, because a stored delta is
a third thing that can disagree with the two it was derived from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _canonical_id(vuln: dict) -> str:
    """One stable identifier for an advisory, across the sources that name it.

    THE SAME ADVISORY ARRIVES UNDER DIFFERENT PRIMARY IDS. `merge_vuln` is
    first-writer-wins on `id`, so which source got there first decides whether
    a record is called GO-2026-5005 or GHSA-jppx-rxg9-jmrx — and both carry
    the other in `aliases` and both carry CVE-2026-39833.

    Keying on the raw `id` therefore reported a repository that nobody had
    touched as having 14 advisories resolved and 14 appeared, five of them
    critical. Same commit, same 74 advisories, different source ordering
    between runs. "5 criticals resolved" when nobody did anything is the same
    lie the out-of-scope bucket was written to kill, moved from the repository
    axis to the identifier axis.

    The CVE wins when there is one, because every source agrees on it. Failing
    that, the lexicographically smallest of the id and its aliases — arbitrary
    but STABLE, which is the whole requirement.
    """
    aliases = [str(a) for a in (vuln.get("aliases") or []) if a]
    cve = str(vuln.get("cve") or "")
    if not cve:
        cve = next((a for a in aliases if a.startswith("CVE-")), "")
    if cve:
        return cve
    ident = str(vuln.get("id") or "")
    candidates = [c for c in [ident, *aliases] if c]
    return min(candidates) if candidates else ""


def _vuln_key(repo: str, package: str, ecosystem: str, vuln_id: str) -> tuple:
    """What makes two vulnerability findings THE SAME finding.

    Version is deliberately absent. A dependency bumped from 2.20.0 to 2.20.1
    while still carrying the same advisory has not produced a new problem, and
    reporting it as one is how a weekly digest teaches people to ignore it.
    Fixing it — the advisory disappearing — is the event worth naming.
    """
    return (repo, ecosystem, package, vuln_id)


@dataclass
class DepDelta:
    """The difference between two audit runs."""

    previous_run_id: str | None
    #: Vulnerabilities present now and absent before.
    appeared: list[dict] = field(default_factory=list)
    #: Present before, absent now, in a repository STILL being audited — so
    #: somebody actually fixed or removed it.
    resolved: list[dict] = field(default_factory=list)
    #: Present before and absent now only because the repository left the
    #: audit. Nobody fixed anything; the question stopped being asked.
    #:
    #: This used to be folded into `resolved`, and the difference is not
    #: cosmetic. A real run reported "493 resolved" when the true number was
    #: ZERO: all 493 belonged to nine repositories that had simply been
    #: de-registered from the workspace. For a post-market-monitoring artefact
    #: that is not a rounding error, it is the opposite of the truth.
    out_of_scope: list[dict] = field(default_factory=list)
    #: Present in both. Carried for the count; the list is the boring one.
    unchanged: int = 0

    @property
    def is_first_run(self) -> bool:
        """No previous run to compare against.

        Distinct from "nothing changed", and the UI must not render them the
        same way: one means the monitoring has not started, the other that it
        is working and quiet.
        """
        return self.previous_run_id is None

    def as_dict(self) -> dict:
        return {
            "previous_run_id": self.previous_run_id,
            "first_run": self.is_first_run,
            "appeared": self.appeared,
            "resolved": self.resolved,
            "out_of_scope": self.out_of_scope,
            "unchanged": self.unchanged,
            "counts": {
                "appeared": len(self.appeared),
                "resolved": len(self.resolved),
                "out_of_scope": len(self.out_of_scope),
                "unchanged": self.unchanged,
            },
        }

    def headline(self) -> str:
        """One sentence, for a digest or a chat card."""
        if self.is_first_run:
            return "First audit — nothing to compare against yet."
        if not self.appeared and not self.resolved and not self.out_of_scope:
            return "No change since the previous audit."
        parts = []
        if self.appeared:
            parts.append(f"{len(self.appeared)} new")
        if self.resolved:
            parts.append(f"{len(self.resolved)} resolved")
        if self.out_of_scope:
            # Named for what happened, never merged into "resolved". A reader
            # who sees "resolved" reasonably concludes somebody did work.
            parts.append(
                f"{len(self.out_of_scope)} no longer audited "
                f"({self._repos_gone()} repositories left the scan)"
            )
        return "Since the previous audit: " + ", ".join(parts) + "."

    def _repos_gone(self) -> int:
        return len({f.get("repo") for f in self.out_of_scope})


def _flatten(rows: list[Any]) -> dict[tuple, dict]:
    """One entry per (repo, ecosystem, package, advisory)."""
    out: dict[tuple, dict] = {}
    for row in rows:
        repo = getattr(row, "repo_slug", "") or ""
        pkg = getattr(row, "package", "") or ""
        eco = getattr(row, "ecosystem", "") or ""
        version = getattr(row, "current_version", "") or ""
        for vuln in (getattr(row, "vulns", None) or []):
            ident = _canonical_id(vuln)
            if not ident:
                continue
            key = _vuln_key(repo, pkg, eco, ident)
            # First writer wins: the same advisory can be reported against two
            # subprojects of one repo, and it is one finding to a reader.
            out.setdefault(key, {
                "id": ident,
                "repo": repo,
                "package": pkg,
                "ecosystem": eco,
                "version": version,
                "severity": vuln.get("severity") or getattr(row, "severity", ""),
                "summary": vuln.get("summary") or "",
                "fixed_version": vuln.get("fixed_in"),
            })
    return out


def compute_delta(
    current: list[Any], previous: list[Any], *, previous_run_id: str | None,
    current_repos: set[str] | None = None,
) -> DepDelta:
    """Compare two runs' findings. Pure — no I/O, no ordering assumptions.

    `current_repos` is the set of repository slugs the CURRENT run actually
    audited. It is what separates "somebody fixed this" from "we stopped
    looking", and it cannot be derived from the findings alone: a repository
    that is still in scope and now perfectly clean contributes no findings, so
    inferring scope from findings would file its genuine fixes under "no
    longer audited" — the same error in the opposite direction.

    When it is not supplied the split is NOT made — everything that vanished
    stays in `resolved`, as it always did. Guessing scope from the findings
    was tried and is worse than not splitting: it files a repository that was
    cleaned up completely under "no longer audited", which is the same lie
    with the sign flipped. A caller that knows the scope says so; a caller
    that does not gets the old, undifferentiated answer.
    """
    if previous_run_id is None:
        # Everything is "new" on a first run, and saying so would drown the
        # reader on the one day they have no baseline. The honest answer is
        # that there is nothing to compare.
        return DepDelta(previous_run_id=None)

    now = _flatten(current)
    before = _flatten(previous)

    appeared = [now[k] for k in now.keys() - before.keys()]
    gone = [before[k] for k in before.keys() - now.keys()]
    if current_repos is None:
        resolved, out_of_scope = gone, []
    else:
        resolved = [f for f in gone if f.get("repo") in current_repos]
        out_of_scope = [f for f in gone if f.get("repo") not in current_repos]
    unchanged = len(now.keys() & before.keys())

    # Worst first — a digest is skimmed, and "3 new" means nothing until you
    # know whether one of them is critical.
    order = {"critical": 0, "high": 1, "medium": 2, "moderate": 2,
             "low": 3, "info": 4}
    appeared.sort(key=lambda f: (order.get(str(f.get("severity")).lower(), 5),
                                 f["repo"], f["package"]))
    for group in (resolved, out_of_scope):
        group.sort(key=lambda f: (order.get(str(f.get("severity")).lower(), 5),
                                  f["repo"], f["package"]))

    logger.info(
        "deps_delta previous=%s appeared=%d resolved=%d out_of_scope=%d unchanged=%d",
        previous_run_id, len(appeared), len(resolved), len(out_of_scope), unchanged,
    )
    return DepDelta(previous_run_id=previous_run_id, appeared=appeared,
                    resolved=resolved, out_of_scope=out_of_scope,
                    unchanged=unchanged)


__all__ = ["DepDelta", "compute_delta"]
