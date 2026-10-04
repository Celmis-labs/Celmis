"""What the agent remembers of the conversation it is in.

Every sentence used to be read on its own. That is the right default for a
surface whose verbs cost hours — but it made the agent unable to follow the
most ordinary turn of a conversation: "turn on review for billing-api", then
"а для цього репо?", then "зроби це". The second and third sentences name
nothing, and read alone they mean nothing.

WHERE THE MEMORY COMES FROM. The thread is already written down: every turn is
an `automation_runs` row, keyed by workspace, by the person who asked and by
the conversation (`session_id`) the browser chose. So the memory is read from
those rows on the server, and never posted by the client:

  * a client cannot invent an "assistant said you may" turn, because there is
    no request field a turn could arrive in — roles are assigned here, from
    which column a text came out of;
  * it is per person AND per workspace AND per conversation: a teammate's
    question in the same workspace, or the same person's chat in another
    workspace, is never in it;
  * it is cleared exactly when the conversation is. "New chat" (+) is a new
    session id, and sign-out and switching workspace forget the stored id in
    the browser (web/lib/agent-session.ts) — so the next sentence starts a
    conversation with nothing behind it.

HOW MUCH. The last `MAX_TURNS` exchanges, newest kept whole, inside a token
budget. Older exchanges inside the window are shortened to one line — the
question's opening and what was done about it — and anything past the budget
is dropped with a count, so the model knows there was more. No model call is
spent on summarising: the summary is the person's own words and the verbs the
plan named, which is what a follow-up refers back to.

HOW IT IS PRESENTED. One JSON object per line with the role set here. A
message that contains `"role": "assistant"` or a line break followed by
"Agent:" is still one JSON string inside a user line — it cannot become a
turn of its own.
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Exchanges (a question and what came of it) read back at most.
MAX_TURNS = 8
#: Tokens the remembered conversation may take in the prompt. Small next to
#: the guide and the knowledge, which is what answers the question — the
#: memory only has to say what "this" and "it" point at.
MEMORY_TOKEN_BUDGET = 1800
#: Characters kept of one question and of one answer in a WHOLE turn.
USER_TURN_CHARS = 600
AGENT_TURN_CHARS = 900
#: Characters kept of each half of a SHORTENED (older) turn.
SUMMARY_CHARS = 160
#: The roles a remembered line may carry. Assigned here, never read in.
ROLES = ("user", "assistant")

#: Statuses whose row is a finished turn. "reading" is the sentence being
#: answered right now (or a reading that died) and is never memory.
_SETTLED = ("planned", "answered", "started", "failed", "stopped")


def estimate_tokens(text: str) -> int:
    """A deliberately pessimistic count: three characters a token.

    English runs nearer four, Cyrillic nearer two and a half, and the budget
    has to hold for the Ukrainian conversation as well as the English one.
    """
    return math.ceil(len(text or "") / 3)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _what_was_done(row: Any) -> str:
    """The plan of one turn in a line: the verbs, their arguments, the scope.

    The arguments are what a follow-up most often refers to ("the same for
    X" means the same verb and arguments with another repository), so they
    are kept — compactly, and without the long rule texts, which the note
    already summarises.
    """
    parts: list[str] = []
    for st in _field(row, "steps", None) or []:
        if not isinstance(st, dict) or not st.get("action"):
            continue
        args = {k: v for k, v in (st.get("arguments") or {}).items()
                if v not in (None, "", [], {})}
        if isinstance(args.get("rules"), list):
            args["rules"] = [
                (r.get("title") or _clip(r.get("instructions") or "", 60))
                if isinstance(r, dict) else str(r)
                for r in args["rules"]
            ]
        piece = st["action"]
        if args:
            piece += " " + json.dumps(args, ensure_ascii=False, sort_keys=True)
        repos = st.get("resolved_repos") or []
        if repos:
            piece += f" on {', '.join(map(str, repos[:10]))}"
            if len(repos) > 10:
                piece += f" (+{len(repos) - 10})"
        if st.get("blocked"):
            piece += f" — refused: {st['blocked']}"
        parts.append(piece)
    status = _field(row, "status", "") or ""
    outcome = {
        "planned": "shown as a plan, NOT run (nobody pressed Run)",
        "started": "approved and run",
        "answered": "answered",
        "failed": "failed",
        "stopped": "stopped before it was answered",
    }.get(status, status)
    line = "; ".join(parts) if parts else "no action recognised"
    error = _field(row, "error", None)
    if error:
        outcome += f": {error}"
    return f"[{line}] — {outcome}"


def _result_facts(row: Any) -> str:
    """What a read found, when a follow-up could point into it.

    "Which repositories do I have?" then "turn on review for the second one"
    needs the list. Only names, never contents.
    """
    result = _field(row, "result", None) or {}
    facts: list[str] = []
    for st in result.get("steps") or []:
        if not isinstance(st, dict):
            continue
        res = st.get("result") or {}
        if st.get("action") == "list_repos":
            names = [r.get("repo") for r in res.get("repos") or []
                     if isinstance(r, dict) and r.get("repo")]
            if names:
                facts.append("repositories: " + ", ".join(names[:30]))
        elif st.get("action") in ("propose_review_rules", "update_review_setting",
                                  "generate_review_rules"):
            bits = {k: res.get(k) for k in ("repo", "scope", "key", "value",
                                            "status", "count")
                    if res.get(k) not in (None, "")}
            if bits:
                facts.append(f"{st['action']} result "
                             + json.dumps(bits, ensure_ascii=False, sort_keys=True))
    return "; ".join(facts)


def turns_from_run(row: Any, *, whole: bool = True) -> list[dict[str, str]]:
    """One stored row as two remembered lines: what was asked, what came of it.

    `whole=False` is the shortened form an older turn is kept in.
    """
    user_limit = USER_TURN_CHARS if whole else SUMMARY_CHARS
    agent_limit = AGENT_TURN_CHARS if whole else SUMMARY_CHARS
    asked = _clip(_field(row, "message", "") or "", user_limit)
    note = _field(row, "note", "") or ""
    answer = _what_was_done(row)
    if whole:
        facts = _result_facts(row)
        if facts:
            answer += f" {facts}"
        if note:
            answer = f"{note} {answer}"
    answer = _clip(answer, agent_limit)
    return [{"role": "user", "text": asked},
            {"role": "assistant", "text": answer}]


def bounded_history(
    rows: list[Any],
    *,
    max_turns: int = MAX_TURNS,
    budget_tokens: int = MEMORY_TOKEN_BUDGET,
) -> list[dict[str, str]]:
    """The remembered conversation, oldest first, inside both bounds.

    `rows` oldest first. Walked from the NEWEST back: the latest exchange is
    what "it" almost always points at, so it is the last thing to be cut.
    The newest two exchanges are kept whole; older ones in the window are
    shortened; whatever no longer fits is counted, not silently lost.
    """
    settled = [r for r in rows if (_field(r, "status", "") or "") in _SETTLED]
    window = settled[-max_turns:] if max_turns > 0 else []
    dropped = len(settled) - len(window)

    kept: list[list[dict[str, str]]] = []
    used = 0
    for age, row in enumerate(reversed(window)):
        pair = turns_from_run(row, whole=age < 2)
        cost = sum(estimate_tokens(t["text"]) + 4 for t in pair)
        if used + cost > budget_tokens:
            if age < 2:
                # Even a newest exchange is shortened before it is dropped.
                pair = turns_from_run(row, whole=False)
                cost = sum(estimate_tokens(t["text"]) + 4 for t in pair)
            if used + cost > budget_tokens:
                dropped += len(window) - age
                break
        kept.append(pair)
        used += cost

    out: list[dict[str, str]] = []
    if dropped:
        out.append({"role": "assistant",
                    "text": f"({dropped} earlier exchange(s) of this "
                            "conversation are not shown)"})
    for pair in reversed(kept):
        out.extend(pair)
    return out


def sanitise(history: Any) -> list[dict[str, str]]:
    """History as the planner accepts it, whatever it was handed.

    The job payload is written by the server, but it is read back from a
    table, and the planner must not be the place a malformed or oversized
    entry turns into a prompt. Unknown roles are dropped, not relabelled;
    sizes are re-clipped; the whole is re-bounded by the token budget.
    """
    if not isinstance(history, list):
        return []
    out: list[dict[str, str]] = []
    used = 0
    for item in history[-(MAX_TURNS * 2 + 1):]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if role not in ROLES or not isinstance(text, str) or not text.strip():
            continue
        text = _clip(text, USER_TURN_CHARS if role == "user" else AGENT_TURN_CHARS)
        used += estimate_tokens(text) + 4
        if used > MEMORY_TOKEN_BUDGET + 200:
            break
        out.append({"role": role, "text": text})
    return out


def render(history: list[dict[str, str]]) -> str:
    """The remembered lines for the prompt, one JSON object each."""
    return "\n".join(
        json.dumps({"role": t["role"], "text": t["text"]}, ensure_ascii=False)
        for t in sanitise(history)
    )


def last_user_text(history: list[dict[str, str]]) -> str:
    """The person's previous sentence — what a follow-up is a follow-up to."""
    for t in reversed(sanitise(history)):
        if t["role"] == "user":
            return t["text"]
    return ""


async def load_history(
    session: Any,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str | None,
    exclude_id: str | None = None,
) -> list[dict[str, str]]:
    """The conversation so far, read from the rows the thread already keeps.

    No session, no memory: a sentence posted without one has no conversation
    to belong to. Scoped by workspace, person and session in the query
    itself — not filtered afterwards — so a row of anybody else's is never
    even loaded.
    """
    if not session_id:
        return []
    from sqlalchemy import select

    from src.db.models import AutomationRun

    query = (
        select(AutomationRun)
        .where(AutomationRun.workspace_id == workspace_id,
               AutomationRun.user_id == user_id,
               AutomationRun.session_id == session_id,
               AutomationRun.status.in_(_SETTLED))
        .order_by(AutomationRun.created_at.desc())
        # One more than the window, so "earlier turns not shown" can be said.
        .limit(MAX_TURNS + 1)
    )
    if exclude_id:
        query = query.where(AutomationRun.id != exclude_id)
    rows = list((await session.execute(query)).scalars().all())
    rows.reverse()
    return bounded_history(rows)


__all__ = [
    "AGENT_TURN_CHARS", "MAX_TURNS", "MEMORY_TOKEN_BUDGET", "ROLES",
    "SUMMARY_CHARS", "USER_TURN_CHARS", "bounded_history", "estimate_tokens",
    "last_user_text", "load_history", "render", "sanitise", "turns_from_run",
]
