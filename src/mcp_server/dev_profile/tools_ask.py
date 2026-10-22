"""`ask`: a written answer from the Q&A pipeline, for what the cheap tools cannot say."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

from src.mcp_server.dev_profile import common, emit, paths
from src.mcp_server.dev_profile.access import DevScope, RepoNotAccessible

MAX_REPOS = 8
#: Calls per user per window. `ask` spends model money and takes seconds.
RATE_LIMIT = 5
RATE_WINDOW_S = 60.0
#: Per worker process: with several workers the effective limit is RATE_LIMIT x workers.
_calls: dict[str, list[float]] = {}
_MAX_TRACKED = 1000
_lock = threading.Lock()


def _rate_limited(user: str) -> float:
    """Seconds to wait, or 0 when the call may go ahead (and is counted)."""
    now = time.monotonic()
    with _lock:
        if len(_calls) > _MAX_TRACKED:  # bounded: forget callers whose window has passed
            for k in [k for k, v in _calls.items() if not v or now - v[-1] >= RATE_WINDOW_S]:
                del _calls[k]
        recent = [t for t in _calls.get(user, []) if now - t < RATE_WINDOW_S]
        if len(recent) >= RATE_LIMIT:
            _calls[user] = recent
            return RATE_WINDOW_S - (now - recent[0])
        recent.append(now)
        _calls[user] = recent
    return 0.0


def _actor():  # noqa: ANN202
    from src.automation.actions import ActionError, Actor
    from src.mcp_server.identity import resolve_caller

    caller = resolve_caller()
    if caller.refused:
        raise ActionError(caller.refused)
    # The token's own repo list travels with the actor: the Q&A pipeline narrows
    # to it too, so `ask` can never read wider than the token that asked. The ids
    # of its shares travel beside the list and never inside it (nothing is matched
    # against them); the pipeline reads each share again, on this call, and uses
    # only one whose owner allowed questions.
    patterns = getattr(caller, "repo_patterns", None)
    return Actor(user_id=caller.user_id, email=getattr(caller, "email", "") or caller.user_id,
                 workspace_id=caller.workspace_id, label="mcp-dev",
                 token_filter=tuple(patterns) if patterns is not None else None,
                 token_shares=tuple(getattr(caller, "share_ids", ()) or ()))


def _askable_scope(scope: DevScope) -> DevScope:
    """The scope `ask` resolves repository names in: what the caller can read, minus
    the shared repositories whose owner has not allowed questions.

    A question sends pieces of the owner's code to the asker's model provider and
    bills the asker's workspace, which is why an owner opts in per share
    (`allow_ask`, off by default). A share that did not opt in is therefore absent
    HERE, and nowhere else: the cheap tools still read it. Absent means that, with no
    names, it is left out without a word, and that a name meets :func:`_closed_share`
    or, for anybody who cannot read it at all, the answer for a repository that is not
    there, word for word (`common.repo_error`), including the `similar:` line, which
    is drawn from this scope and so never offers the repository it just refused."""
    kept = {slug: dec for slug, dec in scope.decisions.items()
            if getattr(dec, "via_share", None) is None or getattr(dec, "ask_ok", False)}
    return dataclasses.replace(scope, decisions=kept)


#: What a holder is told when they name a shared repository whose owner has not
#: allowed questions. Only somebody who can read the repository gets it (they see it
#: in `repos`); everybody else gets the answer for a repository that does not exist.
CLOSED_SHARE = "ask is not enabled for this shared repository"


def _closed_share(scope: DevScope, asking: DevScope, name: str) -> bool:
    """Does ``name`` mean a shared repository this caller can read and cannot ask about?

    ``scope`` holds what the caller reads and ``asking`` what they may ask about, so
    the difference is the closed shares. The name is looked up in it alone, on a copy
    that counts them all as touched already: refusing a question is not a read, and
    must not put a row in the owner's view of who read the share."""
    closed = {slug: dec for slug, dec in scope.decisions.items()
              if slug not in asking.decisions}
    if not closed:
        return False
    probe = dataclasses.replace(scope, decisions=closed, touched=list(closed))
    try:
        probe.resolve(name, need="code")
    except RepoNotAccessible:
        return False
    return True


async def run(question: str, repos: list[str] | None = None, budget_tokens: int = 1500) -> str:
    from src.automation.actions import ActionError
    from src.automation.actions_reviews import ask_code

    scope = await asyncio.to_thread(common.begin)
    q = (question or "").strip()
    if not q:
        return emit.error(None, "question is empty")
    budget = common.clamp(budget_tokens, 200, emit.BUDGETS["ask"][1])
    asking = _askable_scope(scope)
    slugs: list[str] = []
    for name in repos or []:
        try:
            slugs.append(asking.resolve(name, need="code"))
        except RepoNotAccessible:
            if _closed_share(scope, asking, name):
                return emit.error(None, f'repo "{name}": {CLOSED_SHARE}',
                                  "its owner decides this; find, grep and read_symbol still read it")
            return common.repo_error(asking, name)
    if not slugs:
        # "Every repository you can read" is what `asking` holds: the caller's own,
        # and the shared ones whose owner allowed questions. A share that did not is
        # left out without a word (and does not count towards the limit below).
        slugs = asking.slugs(need="code")
        asking.note_shared(slugs)
        if len(slugs) > MAX_REPOS:
            return emit.error(
                None, f"you can read {len(slugs)} repos; name at most {MAX_REPOS} in repos=",
                "call repos to see them")
    if not slugs:
        return emit.error(None, "no repositories you can read; ask a superadmin for access")
    wait = _rate_limited(scope.user_id or "anonymous")
    if wait:
        return emit.error(await asyncio.to_thread(common.fresh_entries, slugs),
                          f"ask is rate limited; retry in {int(wait) + 1}s",
                          "prefer find / read_symbol / refs, which are free")
    entries = await asyncio.to_thread(common.fresh_entries, slugs)
    try:
        res = await ask_code(_actor(), None, question=q, repo_slugs=slugs)
    except ActionError as exc:
        return emit.error(entries, str(exc))
    answer = str(res.get("answer") or "").strip() or "no answer"
    lines = answer.split("\n")
    # The pipeline names the files it read; a secret file is never listed, even as a source.
    files = [str(f) for f in (res.get("files") or [])
             if not paths.is_secret_path(str(f)) and not paths.is_secret_path(str(f).split(":", 1)[-1])][:8]
    if files:
        lines.append("sources: " + ", ".join(files))
    return emit.render_text("ask", entries, lines, budget_tokens=budget)
