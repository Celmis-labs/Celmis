"""Nothing leaves an MCP server unredacted.

Every tool result, on every MCP surface (the HTTP ``/mcp`` mount, the stdio
server and any profile built later), passes through :func:`redact_result`
before it is serialised to the client. One wrapper on the SDK's low-level
``tools/call`` handler covers every tool, including the ones added tomorrow,
the same way ``guard_every_tool`` covers the refusal.

* It always runs. ``settings.redaction_enabled`` only governs the LLM path.
* It fails closed: if redaction raises, the client gets
  ``output withheld: redaction failed`` and no part of the original result.
* Only references the client needs verbatim (``indexed_sha``, cursors, ids)
  are exempt, and only when they have the shape of one.
* What was redacted is counted per label (never the value) and handed to the
  call context, when one is installed, for the audit row.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anyio.to_thread

from src.security.mcp_redact import redact_for_mcp, redact_structure
from src.security.redactor import RedactionStats

logger = logging.getLogger(__name__)

WITHHELD = "output withheld: redaction failed"


def _redact_text(text: str, stats: RedactionStats) -> str:
    """Redact the text block of a tool result.

    FastMCP renders a dict result as indented JSON; that text is parsed and
    judged key by key (``{"password": "x"}`` has nothing a text rule could
    see), and re-serialised only when something changed.
    """
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        else:
            sub = RedactionStats()
            red = redact_structure(data, stats=sub)
            # The structural pass catches fields; the text pass catches what
            # sits inside multi-line strings and anything the walker could not
            # judge. Both run.
            stats.secrets_found += sub.secrets_found
            stats.patterns_matched.extend(sub.patterns_matched)
            if sub.secrets_found == 0 and red == data:
                return _redact_plain(text, stats)
            return _redact_plain(json.dumps(red, indent=2), stats)
    return _redact_plain(text, stats)


def _redact_plain(text: str, stats: RedactionStats) -> str:
    out, s = redact_for_mcp(text)
    stats.secrets_found += s.secrets_found
    stats.patterns_matched.extend(s.patterns_matched)
    return out


def redact_result(result: Any, tool: str = "") -> Any:
    """Return ``result`` with every secret removed from its text and
    structured content. Accepts a ``CallToolResult`` or a ``ServerResult``
    wrapping one. Raises on failure: the caller turns that into an error."""
    from mcp import types

    root = getattr(result, "root", result)
    if not isinstance(root, types.CallToolResult):
        return result

    stats = RedactionStats()
    new_content: list[Any] = []
    for block in root.content or []:
        if isinstance(block, types.TextContent):
            block = block.model_copy(update={"text": _redact_text(block.text, stats)})
        elif isinstance(block, types.EmbeddedResource) and isinstance(
            block.resource, types.TextResourceContents
        ):
            res = block.resource.model_copy(
                update={"text": _redact_text(block.resource.text, stats)}
            )
            block = block.model_copy(update={"resource": res})
        new_content.append(block)

    update: dict[str, Any] = {"content": new_content}
    if root.structuredContent is not None:
        update["structuredContent"] = redact_structure(root.structuredContent, stats=stats)
    new_root = root.model_copy(update=update)

    if stats.secrets_found:
        logger.info(
            "mcp_output_redacted tool=%s count=%d labels=%s",
            tool, stats.secrets_found, sorted(set(stats.patterns_matched)),
        )
        _note(stats)
    return types.ServerResult(new_root) if result is not root else new_root


def _note(stats: RedactionStats) -> None:
    """Hand the counts to the call context, when the audit lane installed one."""
    try:
        from src.mcp_server import callctx  # type: ignore[attr-defined]
    except ImportError:
        return
    try:
        callctx.note_redactions(stats)
    except Exception:  # noqa: BLE001 - audit trouble must not break the answer
        logger.debug("mcp_redaction_note_failed", exc_info=True)


def _withheld() -> Any:
    from mcp import types

    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=WITHHELD)], isError=True
        )
    )


def install_output_guard(mcp: Any, *, profile: str = "full") -> bool:
    """Wrap the low-level ``tools/call`` handler of ``mcp``. Idempotent.

    Raises when the SDK no longer exposes its handler table: a guard that
    silently covers nothing is worse than a server that does not start.
    """
    from mcp import types

    inner = mcp._mcp_server
    original = inner.request_handlers.get(types.CallToolRequest)
    if original is None:
        raise RuntimeError("tools/call handler missing: output guard not installed")
    if getattr(original, "_celmis_output_guard", False):
        return False

    async def guarded(req: Any) -> Any:
        result = await original(req)
        try:
            # Off the loop: redaction is CPU work, and a big output must not stall other callers.
            return await anyio.to_thread.run_sync(
                redact_result, result, getattr(getattr(req, "params", None), "name", "")
            )
        except Exception:  # noqa: BLE001 - fail closed
            logger.exception("mcp_output_redaction_failed profile=%s", profile)
            return _withheld()

    guarded._celmis_output_guard = True  # type: ignore[attr-defined]
    inner.request_handlers[types.CallToolRequest] = guarded
    logger.info("mcp_output_guard_installed profile=%s", profile)
    return True


__all__ = ["WITHHELD", "install_output_guard", "redact_result"]
