"""A small synchronous client over the real MCP SDK, for tests and the runner.

Every call opens its own Streamable-HTTP session: slower than a long-lived one,
and exactly what an agent that reconnects does, so the authentication path is
exercised on every call rather than once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class McpHttpError(RuntimeError):
    """The server refused at the HTTP layer (401, 403, 404 ...)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


@dataclass
class ToolResult:
    text: str
    is_error: bool
    structured: Any = None
    #: Everything the client received, serialised: content blocks AND structured
    #: content. Secret scans run on this, not on `text` alone.
    raw: str = ""


def _http_error(exc: BaseException) -> McpHttpError | None:
    """Dig an HTTPStatusError out of the SDK's TaskGroup wrappers."""
    seen = [exc]
    while seen:
        e = seen.pop()
        if isinstance(e, httpx.HTTPStatusError):
            try:
                body = e.response.text
            except httpx.ResponseNotRead:
                body = ""
            return McpHttpError(e.response.status_code, body)
        seen.extend(getattr(e, "exceptions", ()) or ())
        if e.__cause__ is not None:
            seen.append(e.__cause__)
    return None


class McpClient:
    def __init__(self, base_url: str, path: str, token: str | None) -> None:
        self.url = base_url.rstrip("/") + path
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _run(self, fn):
        async def go():
            async with streamablehttp_client(self.url, headers=self.headers) as (r, w, _), \
                    ClientSession(r, w) as session:
                await session.initialize()
                return await fn(session)

        try:
            return asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001
            http = _http_error(exc)
            if http is not None:
                raise http from None
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        async def fn(s):
            return [t.model_dump(mode="json", exclude_none=True) for t in (await s.list_tools()).tools]

        return self._run(fn)

    def call(self, tool: str, args: dict[str, Any] | None = None) -> ToolResult:
        async def fn(s):
            res = await s.call_tool(tool, args or {})
            text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            return ToolResult(text=text, is_error=bool(res.isError),
                              structured=res.structuredContent,
                              raw=res.model_dump_json(exclude_none=True))

        return self._run(fn)

    def initialize_status(self) -> int:
        """HTTP status of a bare initialize POST: 200, 401, 403, 404 ..."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "e2e", "version": "1"}}}
        r = httpx.post(self.url, json=body, headers={
            **self.headers, "Accept": "application/json, text/event-stream"}, timeout=30)
        return r.status_code
