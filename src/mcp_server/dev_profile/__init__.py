"""The compact `/mcp/dev` profile: a small, read-only, token-frugal tool set.

The full `/mcp` server lists 48 tools (about 9k tokens of definitions) and
answers in pretty-printed JSON, twice. That is the wrong shape for a coding
agent that calls code search dozens of times a session. This profile is its
counterpart:

  * nine read-only tools with short descriptions (the whole list is under
    6,000 characters): eight defined here plus `howto`, which lives in its own
    package and is plugged in through :func:`register_dev_tool`;
  * every response is plain text, starts with an `idx:` line saying which
    revision of which repository the answer is about and how fresh that is,
    and stays inside a token budget, with a cursor for the rest;
  * every body goes through one redaction hook (`emit._redact`);
  * access is the caller's, applied to results — the tool LIST is the same for
    everybody.

Mounted at `/mcp/dev` by `http_app.mount_mcp`, behind the `read:code` scope.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from src.mcp_server.dev_contract import DEV_PATH, SCOPE_DEV
from src.mcp_server.dev_profile.freshness import read_freshness

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Celmis indexes the team's repositories. Prefer it over grepping or reading "
    "whole files for definitions, callers, cross-repo questions and outlines.\n"
    "Workflow: repos (what you can read) -> find (ranked symbols) -> outline "
    "(a file or directory) -> read_symbol (one body) -> refs (callers). grep for "
    "literals and config keys; map for the layout; ask only when those cannot "
    "answer (paid).\n"
    "Every answer starts with 'idx: repo branch@sha age fresh|STALE|unknown'. "
    "Line numbers refer to that sha, which may differ from your checkout: use "
    "local Grep/Read for uncommitted or just-edited files and for literals "
    "inside the repo you are working in. If a result says STALE, treat it as "
    "the code before the latest pushes.\n"
    "Secret values are never returned. If Celmis is unavailable, fall back to "
    "local search; never insist on it."
)


@dataclass(frozen=True)
class ExtraTool:
    name: str
    fn: Callable[..., Any]
    description: str


#: Extra tools (e.g. `howto`) plugged in by their own modules.
_EXTRA: dict[str, ExtraTool] = {}


def register_dev_tool(name: str, fn: Callable[..., Any], description: str) -> None:
    """Add a tool to the dev profile (call at import time, before the profile
    is built).

    `fn` must return `str`, be a plain function or coroutine function with
    typed parameters, and build its output through `dev_profile.emit`
    (`render_list` / `render_text` / `error`) so the idx line, the budget and
    the redaction hook apply. `description` is held to 200 characters.
    `name` must be one of `dev_contract.DEV_TOOLS`.
    """
    from src.mcp_server.dev_contract import DEV_TOOLS

    if name not in DEV_TOOLS:
        raise ValueError(f"{name!r} is not a dev-profile tool name")
    if len(description) > 200:
        raise ValueError("a dev tool description is at most 200 characters")
    _EXTRA[name] = ExtraTool(name, fn, description)


def _howto_idx(slug: str, repo_path: Any) -> Any:
    """The `idx:` entry of a howto answer, from the same freshness source as
    every other dev tool (indexed sha and branch, age, fresh/STALE/unknown)."""
    from src.mcp_server.howto import IdxInfo
    from src.mcp_server.howto.engine import default_idx_provider

    fresh = read_freshness([slug]).get(slug)
    if fresh is None:
        return default_idx_provider(slug, repo_path)
    age = fresh.age if fresh.age != "?" else "unknown"
    return IdxInfo(slug=slug, branch=fresh.branch, sha=fresh.sha, age=age, state=fresh.state)


def _plug_in_howto() -> None:
    """Register `howto` (its own package) on the profile and give it the
    profile's freshness. Idempotent."""
    from src.mcp_server import howto as howto_pkg

    howto_pkg.set_idx_provider(_howto_idx)
    if howto_pkg.TOOL_NAME not in _EXTRA:
        register_dev_tool(howto_pkg.TOOL_NAME, howto_pkg.howto, howto_pkg.DESCRIPTION)


def _strip_titles(node: Any) -> None:
    """Drop the `title` keys pydantic puts on every property: a few hundred
    characters of the tool list that tell the model nothing."""
    if isinstance(node, dict):
        node.pop("title", None)
        for v in node.values():
            _strip_titles(v)
    elif isinstance(node, list):
        for v in node:
            _strip_titles(v)


def _make_server():  # noqa: ANN202
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP

    from src.mcp_server.auth import JwtTokenVerifier
    from src.mcp_server.http_app import _security_kwargs

    common_kwargs: dict[str, Any] = {
        "instructions": INSTRUCTIONS,
        # Served at the sub-app root and mounted at DEV_PATH (see http_app).
        "streamable_http_path": "/",
        **_security_kwargs(),
    }
    try:
        verifier = JwtTokenVerifier()
        return FastMCP(
            "celmis-dev",
            token_verifier=verifier,
            # The transport refuses a token without `read:code` (403). Legacy
            # scope-less tokens are therefore refused here by design: the
            # developer scope is only ever issued deliberately.
            auth=AuthSettings(
                issuer_url=f"https://{verifier.config.issuer}",
                resource_server_url=f"https://{verifier.config.audience}",
                required_scopes=[SCOPE_DEV],
            ),
            **common_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        import os

        if os.environ.get("MCP_ALLOW_UNAUTHENTICATED", "").strip().lower() not in (
            "1", "true", "yes",
        ):
            logger.error("mcp_dev_auth_unavailable err=%s — refusing to start", exc)
            raise
        logger.warning("mcp_dev_unauthenticated_mode err=%s", exc)
        return FastMCP("celmis-dev", **common_kwargs)


def build_dev_mcp():  # noqa: ANN201
    """The `celmis-dev` FastMCP server (not yet mounted)."""
    from mcp.types import ToolAnnotations

    from src.mcp_server.dev_profile import (
        tools_ask,
        tools_find,
        tools_grep,
        tools_read,
        tools_refs,
        tools_repos,
    )

    mcp = _make_server()
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def tool(name: str, description: str):  # noqa: ANN202
        assert len(description) <= 200, name  # noqa: S101
        return mcp.tool(name=name, description=description, annotations=ro,
                        structured_output=False)

    Fmt = Literal["concise", "detailed"]  # noqa: N806

    @tool("repos", "Repos you can read: slug, access level, branch@sha, age, freshness. Call first.")
    async def _repos(query: str = "", cursor: str = "") -> str:
        return await asyncio.to_thread(tools_repos.run, query, cursor)

    @tool("find", "Ranked symbol search across your repos (exact > prefix > token > substring > "
                  "fuzzy): path:lines, kind, signature, caller count.")
    async def _find(query: str, repo: str = "", kind: str = "",
              mode: Literal["auto", "exact", "prefix", "fuzzy"] = "auto",
              limit: int = 10, cursor: str = "", response_format: Fmt = "concise") -> str:
        return await asyncio.to_thread(
            tools_find.run, query, repo, kind, mode, limit, cursor, response_format)

    @tool("outline", "Symbols of a file (by line) or files and top symbols of a directory. "
                     "Use instead of reading whole files.")
    async def _outline(repo: str, path: str, depth: int = 1, response_format: Fmt = "concise") -> str:
        return await asyncio.to_thread(tools_read.outline, repo, path, depth, response_format)

    @tool("read_symbol", "Signature, docstring, body slice (redacted), location and top "
                         "callers/callees of one symbol, at the indexed sha.")
    async def _read_symbol(repo: str, name: str, path: str = "", max_lines: int = 60,
                     include: str = "callers,callees", response_format: Fmt = "concise") -> str:
        return await asyncio.to_thread(
            tools_read.read_symbol, repo, name, path, max_lines, include, response_format)

    @tool("refs", "Who calls/imports a symbol (or what it calls), with hop counts; "
                  "cross-repo via text refs in group repos.")
    async def _refs(repo: str, symbol: str, direction: Literal["callers", "callees", "imports"] = "callers",
              depth: int = 1, cross_repo: bool = True, limit: int = 20, cursor: str = "",
              response_format: Fmt = "concise") -> str:
        return await asyncio.to_thread(
            tools_refs.run, repo, symbol, direction, depth, cross_repo, limit, cursor,
            response_format)

    @tool("grep", "Text search in committed code at the indexed sha: ranked, with line numbers "
                  "and enclosing symbol. For literals, config keys, env names. Secret files excluded.")
    async def _grep(pattern: str, repo: str = "", path_glob: str = "", regex: bool = False,
              limit: int = 20, cursor: str = "", response_format: Fmt = "concise") -> str:
        return await asyncio.to_thread(
            tools_grep.run, pattern, repo, path_glob, regex, limit, cursor, response_format)

    @tool("map", "Repo layout: directories with file/symbol counts and the most-used symbols, "
                 "under a token budget.")
    async def _map(repo: str, path: str = "", budget_tokens: int = 800) -> str:
        return await asyncio.to_thread(tools_read.repo_map, repo, path, budget_tokens)

    @tool("ask", "LLM answer from the Q&A pipeline (paid, slow, rate-limited; "
                 "budget_tokens caps the printed answer, not the model's work). Only "
                 "when find/read_symbol/refs cannot answer.")
    async def _ask(question: str, repos: list[str] | None = None,
                   budget_tokens: int = 1500) -> str:
        return await tools_ask.run(question, repos, budget_tokens)

    _plug_in_howto()
    for extra in _EXTRA.values():
        mcp.tool(name=extra.name, description=extra.description, annotations=ro,
                 structured_output=False)(extra.fn)

    for t in mcp._tool_manager.list_tools():
        _strip_titles(t.parameters)

    from src.mcp_server.call_envelope import install_call_envelope
    from src.mcp_server.guard import guard_every_tool

    guard_every_tool(mcp)
    # One wrapper around every call: record it, redact it (the tools redact
    # their own bodies first; the envelope is the net), audit it.
    install_call_envelope(mcp, profile="dev")
    return mcp


__all__ = ["DEV_PATH", "INSTRUCTIONS", "build_dev_mcp", "register_dev_tool"]
