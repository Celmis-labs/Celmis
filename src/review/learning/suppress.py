"""The "learned filter" stage: do not post a finding the team already judged.

Runs between the deterministic prefilter and the model's veto, only when the
effective `learning_suppression` is not `off`, as a stage of its own so a run
shows what it did.

A finding is a CANDIDATE when something on this repository that matches it
(see `similarity`) was dismissed. It is then scored over the distinct
(PR, person) neighbours inside the window:

    S_dis = sum of weight * 0.5 ** (age / half_life)   over dismissals
    S_acc = the same over accepted and implemented signals

and hidden iff

    N_dis >= needed            distinct (PR, person) dismissals: 1 for an
                               exact repeat, `learning_min_dismissals` (2)
                               otherwise — one pull request cannot train the
                               filter alone
    S_dis >= needed * 0.5      the decayed weight still amounts to half a vote
                               each, so a signal a half-life old still counts
                               and a stale pile does not
    S_dis >= 2 * S_acc         somebody who said "this was right" blocks it

A critical finding, and one whose evidence is `proven` (a rule that matched
text, not a model's opinion), is never hidden. Every other decision is the
data's: this module has no allow-list and no setting that names a rule.

Modes: `shadow` (the built-in) keeps every finding and reports what it WOULD
have hidden; `on` removes them and says how many.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.review.learning import similarity as sim
from src.review.learning.signals import NEUTRAL_REASONS, snapshot_of, window_start

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")
#: How many hidden / would-hide findings a run record lists by name.
MAX_ITEMS = 10
#: Decayed weight each required dismissal must still carry on average: a lone
#: dismissal a day old is worth 0.99 of a vote, and "exactly 1.0" would never be
#: reached by anything but a signal written this second.
MIN_SHARE = 0.5
#: Never hidden by feedback, whatever was said before.
PROTECTED_SEVERITIES = frozenset({"critical"})


@dataclass
class Decision:
    """The verdict on one finding."""

    index: int
    tier: int = 0
    s_dis: float = 0.0
    s_acc: float = 0.0
    n_dis: int = 0
    needed: float = 0.0
    neighbours: list[str] = field(default_factory=list)
    protected: str = ""
    suppress: bool = False

    def as_item(self, finding: Any) -> dict:
        snap = snapshot_of(finding)
        return {
            "title": snap.title[:160], "file": snap.file_path[:200], "tier": self.tier,
            "dismissals": round(self.s_dis, 2), "accepted": round(self.s_acc, 2),
        }


@dataclass
class FilterResult:
    """What the stage did."""

    mode: str
    kept: list[Any]
    hidden: list[Any] = field(default_factory=list)
    would_hide: list[Any] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    checked: int = 0
    #: Why tier 3 did not run ("" when it did or was not needed).
    degraded: str = ""
    skipped: str = ""


def decay(created_at: datetime | None, now: datetime, half_life_days: float) -> float:
    """0.5 ** (age / half-life); 1.0 for a signal with no date."""
    if created_at is None:
        return 1.0
    when = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - when).total_seconds() / 86400.0)
    return 0.5 ** (age_days / max(1.0, float(half_life_days)))


def score(
    neighbours: list[sim.SignalView], now: datetime, half_life_days: float,
) -> tuple[float, float, int]:
    """(S_dis, S_acc, N_dis) over the distinct (PR, person) neighbours. One
    person on one pull request counts once per side, at their strongest signal."""
    dis: dict[tuple, float] = {}
    acc: dict[tuple, float] = {}
    for v in neighbours:
        value = v.weight * decay(v.created_at, now, half_life_days)
        if v.signal == "dismissed":
            if v.reason in NEUTRAL_REASONS:
                continue
            bucket = dis
        elif v.signal in ("accepted", "implemented"):
            bucket = acc
        else:
            continue
        key = v.pr_key
        if value > bucket.get(key, 0.0):
            bucket[key] = value
    return sum(dis.values()), sum(acc.values()), len(dis)


def _protected(finding: Any) -> str:
    severity = str(getattr(getattr(finding, "severity", ""), "value",
                           getattr(finding, "severity", "")) or "").lower()
    if severity in PROTECTED_SEVERITIES:
        return "critical"
    if str(getattr(finding, "evidence_kind", "") or "").lower() == "proven":
        return "proven"
    return ""


def decide(
    findings: list[Any], views: list[sim.SignalView], *, min_dismissals: float,
    half_life_days: float, now: datetime,
    vector_hits: dict[int, list[tuple[str, float]]] | None = None,
) -> list[Decision]:
    """One `Decision` per finding, from tiers 1-2 over `views` and, when given,
    the tier-3 hits (finding index -> [(signal id, cosine)])."""
    by_id = {v.id: v for v in views}
    out: list[Decision] = []
    for i, f in enumerate(findings):
        d = Decision(index=i, protected=_protected(f))
        out.append(d)
        snap = snapshot_of(f)
        exact = sim.match_exact(snap.fingerprint, views)
        near = sim.match_titles(snap.title, snap.file_path, snap.rule_id, snap.category, views)
        found: dict[str, sim.SignalView] = {v.id: v for v in (*exact, *near)}
        for sid, _cos in (vector_hits or {}).get(i, ()):
            if sid in by_id:
                found.setdefault(sid, by_id[sid])
        neighbours = list(found.values())
        d.neighbours = [v.id for v in neighbours]
        if not any(v.signal == "dismissed" and v.reason not in NEUTRAL_REASONS
                   for v in neighbours):
            continue
        d.tier = 1 if any(v.signal == "dismissed" and v.reason not in NEUTRAL_REASONS
                          for v in exact) else (2 if near else 3)
        d.s_dis, d.s_acc, d.n_dis = score(neighbours, now, half_life_days)
        d.needed = 1.0 if d.tier == 1 else float(min_dismissals)
        d.suppress = (not d.protected and d.n_dis >= d.needed
                      and d.s_dis >= d.needed * MIN_SHARE and d.s_dis >= 2.0 * d.s_acc)
    return out


def apply(
    findings: list[Any], *, workspace_id: str, repo_slug: str, mode: str,
    settings: Any = None, now: datetime | None = None, session: Any = None,
    client: Any = None, embed: Callable[[list[str]], list[list[float]]] | None = None,
    use_embeddings: bool = True,
) -> FilterResult:
    """The stage. `findings` are what the prefilter kept; the result's `kept`
    is what goes on (everything in shadow mode). Never raises."""
    if mode not in ("shadow", "on") or not findings:
        return FilterResult(mode=mode, kept=list(findings),
                            skipped="off" if mode not in ("shadow", "on") else "no findings")
    try:
        if settings is None:
            from src.review.settings import get_review_settings

            settings = get_review_settings()
        now = now or datetime.now(UTC)
        views = sim.load_signals(
            workspace_id, repo_slug, window_start(settings.learning_window_days),
            session=session)
        if not any(v.signal == "dismissed" for v in views):
            return FilterResult(mode=mode, kept=list(findings), checked=len(findings),
                                skipped="nothing dismissed on this repository yet")
        hits: dict[int, list[tuple[str, float]]] = {}
        degraded = ""
        if use_embeddings:
            try:
                sim.index_pending(workspace_id, repo_slug, client=client, embed=embed,
                                  session=session)
                texts = []
                for f in findings:
                    s = snapshot_of(f)
                    texts.append(sim.signal_text(s.category, s.title, s.body, s.file_path))
                found = sim.query_similar(
                    workspace_id, repo_slug, texts, threshold=settings.learning_similarity,
                    client=client, embed=embed)
                hits = dict(enumerate(found))
            except Exception as exc:  # noqa: BLE001 — tiers 1-2 still apply
                degraded = type(exc).__name__
                logger.info("learning_embeddings_unavailable ws=%s err_type=%s",
                            workspace_id, degraded)
        decisions = decide(
            findings, views, min_dismissals=settings.learning_min_dismissals,
            half_life_days=settings.learning_half_life_days, now=now, vector_hits=hits)
    except Exception as exc:  # noqa: BLE001
        logger.warning("learned_filter_failed ws=%s err_type=%s", workspace_id,
                       type(exc).__name__)
        return FilterResult(mode=mode, kept=list(findings), skipped=f"failed: {type(exc).__name__}")

    flagged = [(findings[d.index], d) for d in decisions if d.suppress]
    items = [d.as_item(f) for f, d in flagged[:MAX_ITEMS]]
    if mode == "on":
        drop = {d.index for _f, d in flagged}
        return FilterResult(
            mode=mode, kept=[f for i, f in enumerate(findings) if i not in drop],
            hidden=[f for f, _d in flagged], items=items, checked=len(findings),
            degraded=degraded)
    return FilterResult(mode=mode, kept=list(findings), would_hide=[f for f, _d in flagged],
                        items=items, checked=len(findings), degraded=degraded)
