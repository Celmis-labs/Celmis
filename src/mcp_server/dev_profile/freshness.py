"""What every dev answer says about how old the index it came from is.

Line 1 of every response (`idx: ...`, contract in `dev_contract`) carries, per
repository: `slug branch@sha age state`. The sha is the revision the index was
built from, so a line number in the answer is a line number of THAT commit;
`state` says whether the remote has moved on since:

  fresh    the remote's head equals the indexed sha, checked within 6 hours
  STALE    the remote's head differs: the answer is about an older tree
  unknown  never checked, the check failed, or no revision was recorded

`unknown` is an answer, not a gap: a surface that rendered it as "fresh" would
be the failure this header exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from src.mcp_server.dev_profile import git_io

logger = logging.getLogger(__name__)

FRESH_CHECK_HOURS = 6
#: Longest idx line: beyond this many entries the rest is summarised.
MAX_ENTRIES = 12


@dataclass(frozen=True)
class RepoFresh:
    slug: str
    branch: str
    sha: str
    age: str
    state: str  # fresh | STALE | unknown

    def entry(self) -> str:
        return f"{self.slug} {self.branch}@{self.sha[:8]} {self.age} {self.state}"


def human_age(then: datetime | None, now: datetime | None = None) -> str:
    if then is None:
        return "?"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    secs = int(((now or datetime.now(UTC)) - then).total_seconds())
    if secs < 90:
        return "now"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _state(info, now: datetime) -> str:  # noqa: ANN001
    if info is None:
        return "unknown"
    ok = info.up_to_date  # True / False / None
    if ok is False:
        return "STALE"
    if ok is True:
        checked = info.last_checked_at
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        if checked is not None and (now - checked).total_seconds() <= FRESH_CHECK_HOURS * 3600:
            return "fresh"
    return "unknown"


def read_freshness(slugs: list[str]) -> dict[str, RepoFresh]:
    """Freshness of each slug that has a nameable revision.

    A slug with no recorded sha falls back to the clone's HEAD, state
    `unknown` (the graph exists, but nothing says which commit it was built
    from). A slug with neither is absent: there is nothing true to print.
    """
    from src.repos import index_state

    now = datetime.now(UTC)
    try:
        states = index_state.read_index_states(slugs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dev_freshness_read_failed err=%s", exc)
        states = {}
    out: dict[str, RepoFresh] = {}
    for slug in slugs:
        info = states.get(slug)
        sha = info.last_indexed_sha if info and info.last_indexed_sha else None
        state = _state(info, now)
        if sha is None:
            sha = git_io.head_sha(slug)
            state = "unknown"
        if sha is None:
            continue
        branch = (info.indexed_branch if info and info.indexed_branch else None) \
            or git_io.head_branch(slug) or "?"
        out[slug] = RepoFresh(
            slug=slug, branch=branch.replace(" ", "_"), sha=sha,
            age=human_age(info.last_indexed_at if info else None, now), state=state,
        )
    return out


def idx_line(entries: list[RepoFresh]) -> str:
    """The first line of a response. `idx: none` when no repository is involved."""
    if not entries:
        return "idx: none"
    shown = entries[:MAX_ENTRIES]
    line = "idx: " + " · ".join(e.entry() for e in shown)
    if len(entries) > len(shown):
        line += f" · +{len(entries) - len(shown)} more repos"
    return line
