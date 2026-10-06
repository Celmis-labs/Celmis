"""The ONLY way a dev tool turns what it found into text.

A tool hands over a freshness header, the items it found and the paging it was
asked for; this module redacts, applies the token budget and prints. Nothing
else writes a dev-tool response, so there is one place where a secret can be
caught and one place where a response can be too long.

Output contract (see `dev_contract`):

  * plain text, one hit per line — no JSON, no nulls, no URLs;
  * line 1 is the idx line (repository, branch@sha, age, fresh|STALE|unknown),
    present on success, on empty results and on errors;
  * when the budget (or the page) cuts the list, the last line is
    ``… +N more · cursor=<opaque> · narrow with <hint>``.

Budgets are tokens estimated as chars / 3.5. `concise` (the default) uses the
tool's default budget; `detailed` doubles it; a caller that asks for a bigger
page than the default gets the tool's hard cap, never more.
"""

from __future__ import annotations

import logging

from src.mcp_server.dev_profile import cursor as cursor_mod
from src.mcp_server.dev_profile import literals
from src.mcp_server.dev_profile.freshness import RepoFresh, idx_line

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5

#: tool -> (default tokens, hard cap tokens)
BUDGETS: dict[str, tuple[int, int]] = {
    "repos": (400, 1500),
    "find": (600, 4000),
    "outline": (800, 4000),
    "read_symbol": (1200, 4000),
    "refs": (1000, 4000),
    "grep": (800, 4000),
    "map": (800, 2000),
    "ask": (1500, 2000),
    "howto": (1200, 4000),
}
WITHHELD = "output withheld: redaction failed"


# ─── THE redaction hook ──────────────────────────────────────────────

def _central():
    """The central MCP redaction layer, when this build has one."""
    try:
        from src.security.mcp_redact import redact_for_mcp
    except ImportError:
        return None
    return redact_for_mcp


def _redact(text: str) -> str:
    """Every byte of body text a dev tool prints passes through here.

    Three layers, each of which must pass the text through unchanged or
    mask it, never print what it could not check:

    1. the central MCP layer (`redact_for_mcp`: DSN passwords, auth headers,
       kv-passwords, high-entropy literals) when this build has it;
       otherwise the repository's code redactor, the one that guards the Q&A
       pipeline's snippets;
    2. :func:`literals.mask_literals`, always: ``KEY=value``, ``key: value``,
       ``password='x'``, ``redis://:x@host`` and env defaults. It is the
       floor, so the profile is safe even if layer 1's rules change.

    Raises rather than returning unchecked text: the caller turns an
    exception into :data:`WITHHELD`.
    """
    central = _central()
    if central is not None:
        out, _stats = central(text, source_hint="dev_profile")
    else:
        from src.security.redactor import redact

        out, _stats = redact(text, mode="code")
    return literals.mask_literals(out)


def redact_text(text: str) -> str:
    """Public alias for tools that must redact BEFORE slicing a body."""
    return _redact(text)


def clip(text: str, limit: int) -> str:
    """One whitespace-collapsed line, redacted FIRST and cut second.

    Cutting before redaction can sever a secret's closing quote or tail so
    that no rule recognises the remainder; a cut of already-masked text can
    only drop characters.
    """
    flat = " ".join(str(text or "").split())
    try:
        flat = _redact(flat)
    except Exception:  # noqa: BLE001 — fail closed
        logger.error("dev_redaction_failed in clip")
        return "[withheld]"
    return flat if len(flat) <= limit else flat[: max(limit - 1, 1)] + "…"


class RedactionFailed(RuntimeError):
    """The redactor raised: the body is withheld, never printed unchecked."""


def _safe(text: str) -> str:
    try:
        return _redact(text)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.error("dev_redaction_failed err=%s", type(exc).__name__)
        raise RedactionFailed from exc


# ─── budgets ─────────────────────────────────────────────────────────

def budget_chars(tool: str, *, detailed: bool = False, raised: bool = False,
                 budget_tokens: int | None = None) -> int:
    default, hard = BUDGETS[tool]
    tokens = default * (2 if detailed else 1)
    if raised:
        tokens = hard
    if budget_tokens is not None:
        tokens = budget_tokens
    return int(min(tokens, hard) * CHARS_PER_TOKEN)


def withheld(entries: list[RepoFresh] | None = None) -> str:
    return f"{idx_line(entries or [])}\n{WITHHELD}"


def error(entries: list[RepoFresh] | None, message: str, *hints: str) -> str:
    """An actionable one-liner under the idx line."""
    body = [message, *[h for h in hints if h]]
    try:
        return idx_line(entries or []) + "\n" + _safe("\n".join(body))
    except RedactionFailed:
        return withheld(entries)


# ─── rendering ───────────────────────────────────────────────────────

def render_list(
    tool: str, entries: list[RepoFresh], page: list[str], *,
    total: int, offset: int = 0, query_hash: str = "", head: list[str] | None = None,
    note: str = "", empty: str = "no results", more_hint: str = "",
    detailed: bool = False, raised: bool = False,
    pin: dict[str, str] | None = None,
) -> str:
    """Header, `head` lines, then `page` (the items at `offset` of `total` in
    the full ranked list) trimmed to the budget, then the truncation line when
    anything — the rest of the page or later pages — was left out.

    `pin` is the slug -> sha map the cursor is bound to; the tool passes the
    same one it validates an incoming cursor against (default: the idx entries).
    """
    try:
        header = idx_line(entries)
        lines: list[str] = []
        if note:
            lines.append(note)
        lines.extend(_safe(h) for h in (head or []))
        cap = budget_chars(tool, detailed=detailed, raised=raised)
        used = len(header) + sum(len(x) + 1 for x in lines)
        shown = 0
        for it in page:
            text = _safe(it)
            if shown and used + len(text) + 1 > cap:
                break
            lines.append(text)
            used += len(text) + 1
            shown += 1
        if not page:
            lines.append(empty)
        remaining = total - (offset + shown)
        if remaining > 0:
            cur = cursor_mod.encode(
                query_hash, offset + shown,
                pin if pin is not None else {e.slug: e.sha for e in entries})
            tail = f"… +{remaining} more · cursor={cur}"
            if more_hint:
                tail += f" · narrow with {more_hint}"
            lines.append(tail)
        return header + "\n" + "\n".join(lines)
    except RedactionFailed:
        return withheld(entries)


def render_text(
    tool: str, entries: list[RepoFresh], lines: list[str], *,
    detailed: bool = False, budget_tokens: int | None = None,
) -> str:
    """Header plus a block of text that is not a list (a symbol, an answer).

    The block is redacted as ONE text — a multi-line secret (a PEM body) is
    only recognisable whole — and cut at the budget on a line boundary.
    """
    try:
        header = idx_line(entries)
        body = _safe("\n".join(lines))
        cap = budget_chars(tool, detailed=detailed, budget_tokens=budget_tokens)
        out: list[str] = []
        used = len(header)
        cut = False
        for ln in body.split("\n"):
            if out and used + len(ln) + 1 > cap:
                cut = True
                break
            out.append(ln)
            used += len(ln) + 1
        if cut:
            out.append("… truncated to the token budget")
        return header + "\n" + "\n".join(out)
    except RedactionFailed:
        return withheld(entries)
