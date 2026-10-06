"""`ask`: a written answer from the Q&A pipeline, for what the cheap tools cannot say."""

from __future__ import annotations

import asyncio
import threading
import time

from src.mcp_server.dev_profile import common, emit, paths
from src.mcp_server.dev_profile.access import RepoNotAccessible

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
    # to it too, so `ask` can never read wider than the token that asked.
    patterns = getattr(caller, "repo_patterns", None)
    return Actor(user_id=caller.user_id, email=getattr(caller, "email", "") or caller.user_id,
                 workspace_id=caller.workspace_id, label="mcp-dev",
                 token_filter=tuple(patterns) if patterns is not None else None)


async def run(question: str, repos: list[str] | None = None, budget_tokens: int = 1500) -> str:
    from src.automation.actions import ActionError
    from src.automation.actions_reviews import ask_code

    scope = await asyncio.to_thread(common.begin)
    q = (question or "").strip()
    if not q:
        return emit.error(None, "question is empty")
    budget = common.clamp(budget_tokens, 200, emit.BUDGETS["ask"][1])
    slugs: list[str] = []
    for name in repos or []:
        try:
            slugs.append(scope.resolve(name, need="code"))
        except RepoNotAccessible:
            return common.repo_error(scope, name)
    if not slugs:
        slugs = scope.slugs(need="code")
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
