"""One wrapper around every MCP tool call: record it, redact it, audit it.

Installed on the low-level ``CallToolRequest`` handler — the same mechanism as
the scope filter on ``tools/list`` — so it covers every tool of a server
whichever way the tool was registered, and a tool added tomorrow without
anybody remembering to.

For each call:

1. open the call record (:mod:`src.mcp_server.callctx`);
2. run the existing handler (the refusal guard still applies inside it);
3. pass the result through :func:`src.mcp_server.output_guard.redact_result`
   — if that raises, the answer is withheld (fail closed);
4. measure bytes and items;
5. write the audit row (:mod:`src.mcp_server.audit`) — failures are swallowed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Literal

import anyio

logger = logging.getLogger(__name__)

_MARK = "_celmis_call_envelope"

WITHHELD = "output withheld: redaction failed"

Profile = Literal["full", "dev", "stdio"]


_SALT: bytes | None = None


def _salt() -> bytes:
    """A per-installation key for the digest, derived from the signing secret.
    Without it the 16 hex characters of a plain SHA-256 can be reversed by a
    dictionary of slugs and short queries; with it, only the installation can
    tell what a digest stands for."""
    global _SALT
    if _SALT is None:
        try:
            from src.mcp_server.auth import JwtConfig

            secret = JwtConfig.from_env().secret
        except Exception:  # noqa: BLE001 — no secret, no salt: still one-way
            secret = ""
        _SALT = hashlib.sha256(b"celmis-mcp-args-hash:" + secret.encode()).digest()
    return _SALT


def _args_hash(arguments) -> str:  # noqa: ANN001
    """A keyed digest of the call's arguments. One-way: it separates two calls,
    it does not let anybody read what was asked."""
    try:
        blob = json.dumps(arguments or {}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = repr(arguments)
    return hmac.new(_salt(), blob.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:16]


def _measure(root) -> tuple[int, int]:  # noqa: ANN001
    """(bytes, items) of a CallToolResult's visible output."""
    size = 0
    items = 0
    for block in getattr(root, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            size += len(text.encode("utf-8", "replace"))
            items += sum(1 for ln in text.splitlines() if ln.strip())
    structured = getattr(root, "structuredContent", None)
    if isinstance(structured, dict):
        lists = [len(v) for v in structured.values() if isinstance(v, list)]
        if lists:
            items = max(lists)
    return size, items


#: What an error answer says when the tool refused the caller rather than failed.
_REFUSAL_MARKERS = ("repo not found or not accessible", "Required scope(s)",
                    "not permitted", "is refused", "no longer a member",
                    "This MCP token", "Could not confirm your membership")


def _is_refusal(root) -> bool:  # noqa: ANN001
    text = " ".join(getattr(b, "text", "") or "" for b in getattr(root, "content", None) or [])
    return any(m in text for m in _REFUSAL_MARKERS)


def _error_result(message: str):  # noqa: ANN202
    import mcp.types as types

    return types.ServerResult(types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], isError=True))


async def _within_limits(original, req):  # noqa: ANN001, ANN202
    """Run the handler under the token's rate, concurrency and time limits.
    Returns ``(result, refused_by_a_limit)``."""
    from src.mcp_server import limits

    key = limits.key_for_request()
    wait = limits.check_rate(key)
    if wait:
        return _error_result(limits.RATE_LIMITED.format(wait=wait)), True
    seconds = limits.call_timeout()
    try:
        async with limits.slot(key):
            if seconds > 0:
                with anyio.fail_after(seconds):
                    return await original(req), False
            return await original(req), False
    except limits.Busy:
        return _error_result(limits.BUSY), True
    except TimeoutError:
        return _error_result(limits.TIMED_OUT.format(seconds=int(seconds))), True


def install_call_envelope(mcp, *, profile: Profile) -> bool:  # noqa: ANN001
    """Wrap ``mcp``'s tool-call handler. Returns True when installed.

    Raises if the SDK no longer exposes the handler table: an envelope that
    silently covers nothing is worse than a server that does not start.
    """
    import mcp.types as types

    inner_server = mcp._mcp_server  # low-level Server
    key = types.CallToolRequest
    original = inner_server.request_handlers.get(key)
    if original is None:
        raise RuntimeError("the MCP SDK registered no tool-call handler to wrap")
    if getattr(original, _MARK, False):
        return True

    async def enveloped(req):  # noqa: ANN001, ANN202
        from src.mcp_server import audit, callctx, output_guard

        name = getattr(req.params, "name", "") or ""
        rec = callctx.begin(name, profile)
        rec.args_hash = _args_hash(getattr(req.params, "arguments", None))
        started = time.monotonic()
        try:
            result, limited = await _within_limits(original, req)
            if limited:
                callctx.set_status("denied")
            try:
                # Off the loop: redaction is CPU work, and a big output must not stall other callers.
                result = await anyio.to_thread.run_sync(output_guard.redact_result, result, name)
            except Exception as exc:  # noqa: BLE001 — fail closed
                logger.error("mcp_output_redaction_failed tool=%s err=%s", name,
                             type(exc).__name__)
                result = _error_result(WITHHELD)
                callctx.set_status("error")
            root = getattr(result, "root", result)
            rec.result_bytes, rec.result_items = _measure(root)
            if getattr(root, "isError", False) and rec.status == "ok":
                # A guard that already said "denied" is not overruled by wording.
                callctx.set_status("denied" if _is_refusal(root) else "error")
            return result
        except Exception:
            callctx.set_status("error")
            raise
        finally:
            rec.duration_ms = int((time.monotonic() - started) * 1000)
            try:
                audit.record_call(rec)
            except Exception:  # noqa: BLE001 — an audit failure never breaks a call
                logger.warning("mcp_audit_failed tool=%s", name)

    setattr(enveloped, _MARK, True)
    inner_server.request_handlers[key] = enveloped
    logger.info("mcp_call_envelope_installed profile=%s", profile)
    return True


def is_enveloped(mcp) -> bool:  # noqa: ANN001
    import mcp.types as types

    handler = mcp._mcp_server.request_handlers.get(types.CallToolRequest)
    return bool(getattr(handler, _MARK, False))


__all__ = ["WITHHELD", "install_call_envelope", "is_enveloped"]
