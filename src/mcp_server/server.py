"""MCP server — wraps tools.py functions in FastMCP decorators.

MCP SDK 1.23.x (May 2026):
    - FastMCP — decorator-based, like FastAPI
    - Stdio transport (default) — for Claude Code / Cursor IDE integration
    - Streamable HTTP transport — for remote / multi-tenant scenarios

Run patterns:
    Standard (Claude Code, Cursor):
        analyzer mcp serve              # stdio, blocking
    Remote:
        analyzer mcp serve --transport http --port 8080

Usage in the Claude Code config (~/.config/claude-code/mcp.json):
    {
      "mcpServers": {
        "code-analyzer": {
          "command": "analyzer",
          "args": ["mcp", "serve"]
        }
      }
    }
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from src.mcp_server import tools
from src.mcp_server.scopes import require_scopes

logger = logging.getLogger(__name__)


SERVER_NAME = "code-analyzer"
SERVER_DESCRIPTION = (
    "Code intelligence platform — query indexed repositories in a group for "
    "cross-team integration analysis. Wraps tree-sitter graph + cross-repo edges."
)


def build_server(*, enable_auth: bool = False) -> FastMCP:
    """Create a FastMCP server with all tools registered.

    Args:
        enable_auth: if True, JWT Bearer auth is activated via TokenVerifier.
            Requires the MCP_JWT_SECRET env var.
    """
    if enable_auth:
        from mcp.server.auth.settings import AuthSettings

        from src.mcp_server.auth import JwtTokenVerifier

        verifier = JwtTokenVerifier()
        # AuthSettings requires issuer_url for the metadata endpoint
        issuer_url = f"https://{verifier.config.issuer}"
        auth_settings = AuthSettings(
            issuer_url=issuer_url,
            resource_server_url=f"https://{verifier.config.audience}",
            required_scopes=[],  # tools have per-tool scope checks (V2)
        )
        mcp = FastMCP(
            SERVER_NAME,
            instructions=SERVER_DESCRIPTION,
            token_verifier=verifier,
            auth=auth_settings,
        )
        logger.info("mcp_server_auth_enabled issuer=%s", verifier.config.issuer)
    else:
        mcp = FastMCP(SERVER_NAME, instructions=SERVER_DESCRIPTION)

    @mcp.tool(
        name="list_groups",
        description=(
            "List all repository groups defined in the workspace. "
            "Each group represents related repos for cross-team analysis "
            "(for example, frontend + backend + mobile of one product)."
        ),
    )
    @require_scopes("read:groups")
    def _list_groups() -> dict[str, Any]:
        items = tools.list_groups()
        return {
            "groups": [
                {
                    "name": g.name,
                    "description": g.description,
                    "repo_count": g.repo_count,
                    "indexed": g.indexed,
                    "cross_repo_indexed": g.cross_repo_indexed,
                    "cross_repo_edges": g.cross_repo_edges,
                }
                for g in items
            ],
            "count": len(items),
        }

    @mcp.tool(
        name="list_repos",
        description=(
            "List repositories: either all indexed repos (group_name=None) or "
            "repos in a specific group. Returns slug + indexing status + symbol count."
        ),
    )
    @require_scopes("read:groups")
    def _list_repos(group_name: str | None = None) -> dict[str, Any]:
        items = tools.list_repos(group_name=group_name)
        return {
            "repos": [
                {
                    "slug": r.slug,
                    "full_path": r.full_path,
                    "indexed": r.indexed,
                    "symbol_count": r.symbol_count,
                }
                for r in items
            ],
            "group": group_name,
            "count": len(items),
        }

    @mcp.tool(
        name="find_symbol",
        description=(
            "Find symbols by name (exact match) in a specific repo's graph. "
            "Returns: list of symbols (function/class/method/etc.) with file path, "
            "kind, language, exported flag."
        ),
    )
    @require_scopes("read:graph")
    def _find_symbol(
        name: str,
        repo_slug: str,
        limit: int = 20,
    ) -> dict[str, Any]:
        items = tools.find_symbol(name=name, repo_slug=repo_slug, limit=limit)
        return {
            "name": name,
            "repo_slug": repo_slug,
            "matches": items,
            "count": len(items),
        }

    @mcp.tool(
        name="get_symbol",
        description=(
            "Fetch full details about a symbol by id. Use after find_symbol() to "
            "get the extra fields (signature, docstring, exports and so on)."
        ),
    )
    @require_scopes("read:graph")
    def _get_symbol(
        symbol_id: str,
        repo_slug: str,
    ) -> dict[str, Any] | None:
        return tools.get_symbol(symbol_id=symbol_id, repo_slug=repo_slug)

    @mcp.tool(
        name="find_callers",
        description=(
            "Find symbols that call the target (incoming CALLS / IMPORTS edges). "
            "BFS expansion with controllable depth. depth=1 — direct callers; "
            "depth>1 — transitive callers through the chain."
        ),
    )
    @require_scopes("read:graph")
    def _find_callers(
        symbol_id: str,
        repo_slug: str,
        depth: int = 2,
        max_nodes: int = 100,
    ) -> dict[str, Any]:
        return tools.find_callers(
            symbol_id=symbol_id, repo_slug=repo_slug,
            depth=depth, max_nodes=max_nodes,
        )

    @mcp.tool(
        name="find_callees",
        description=(
            "Find symbols called by the target (outgoing CALLS / IMPORTS edges). "
            "BFS expansion. Useful for analysing a function's dependency graph."
        ),
    )
    @require_scopes("read:graph")
    def _find_callees(
        symbol_id: str,
        repo_slug: str,
        depth: int = 2,
        max_nodes: int = 100,
    ) -> dict[str, Any]:
        return tools.find_callees(
            symbol_id=symbol_id, repo_slug=repo_slug,
            depth=depth, max_nodes=max_nodes,
        )

    @mcp.tool(
        name="cross_repo_edges",
        description=(
            "Get all cross-repo edges materialized for a group. "
            "Edge kinds: REFERENCES_REPO (image references → repo), "
            "BUILD_CONTEXT (compose build path → repo). "
            "For cross-team integration analysis — which frontend repo references "
            "which backend repo."
        ),
    )
    @require_scopes("read:groups")
    def _cross_repo_edges(group_name: str) -> dict[str, Any]:
        edges = tools.cross_repo_edges(group_name=group_name)
        return {
            "group": group_name,
            "edges": edges,
            "count": len(edges),
        }

    @mcp.tool(
        name="review_pr",
        description=(
            "Review pull/merge request multi-agent reviewer. "
            "Architect+Security (Gemini 3 Pro), Quality+Tests (Flash), Verifier. "
            "Provider: 'github'|'gitlab'|'bitbucket'. "
            "Returns findings + verdict. dry_run=True (default) — do not post comments."
        ),
    )
    @require_scopes("review:pr")
    def _review_pr(
        provider: str,
        repo: str,
        pr_number: int,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        from src.mcp_server.identity import resolve_caller
        from src.review.orchestrator import ReviewOrchestrator
        from src.review.providers import get_provider_for
        from src.review.providers.base import PullRequestProviderError

        # Resolve the authenticated caller's tenant so the review uses THEIR
        # git token + LLM key/policy, not the shared default workspace.
        caller = resolve_caller()

        try:
            pr_provider = get_provider_for(
                provider, user_id=caller.user_id, workspace_id=caller.workspace_id,
            )
        except (ValueError, PullRequestProviderError) as exc:
            return {"ok": False, "error": str(exc)}

        try:
            orchestrator = ReviewOrchestrator()
            result = orchestrator.review(
                provider, repo, pr_number,
                dry_run=dry_run,
                post_comments=not dry_run,
                provider=pr_provider,
                user_id=caller.user_id,
                workspace_id=caller.workspace_id,
            )
        except PullRequestProviderError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            pr_provider.close()

        batch = result.batch
        return {
            "ok": True,
            "verdict": batch.verdict.value,
            "findings_count": len(batch.findings),
            "critical": batch.critical_count,
            "error": batch.error_count,
            "warning": batch.warning_count,
            "info": batch.info_count,
            "cross_repo_callers": batch.cross_repo_callers,
            "agents_run": batch.agents_run,
            "tokens": {
                "in": batch.tokens_in,
                "out": batch.tokens_out,
            },
            "elapsed_seconds": batch.elapsed_seconds,
            "posted": result.posted,
            "summary": batch.summary[:500] if batch.summary else "",
            "top_findings": [
                {
                    "file": f.file_path,
                    "line": f.line,
                    "severity": f.severity.value,
                    "title": f.title,
                    "agent": f.agent,
                    "rule": f.rule_id,
                }
                for f in batch.findings[:10]
            ],
        }

    @mcp.tool(
        name="query_graph",
        description=(
            "Execute read-only Cypher query against per-repo (repo_slug) or "
            "cross-repo (group_name) graph. Write keywords blocked: "
            "CREATE/DELETE/SET/MERGE/REMOVE/DROP. "
            "Returns rows as a list of dicts. Use this for complex queries that "
            "the tools above do not cover."
        ),
    )
    @require_scopes("read:graph")
    def _query_graph(
        cypher: str,
        repo_slug: str | None = None,
        group_name: str | None = None,
    ) -> dict[str, Any]:
        return tools.query_graph(
            cypher=cypher, repo_slug=repo_slug, group_name=group_name,
        )

    # ─── Automation: the write half ──────────────────────────────────
    #
    # Everything above reads. These four let an external Claude Code do the
    # whole loop — register the repositories somebody listed, audit them, read
    # the findings back — instead of describing what a person should click.
    #
    # They sit behind `write:repos`, a scope no existing token carries, so an
    # already-issued read token cannot suddenly spend money. And they call the
    # same src.automation.actions the HTTP API and the coming ticket connector
    # use, so the tenancy check and the live-run rule are written once.

    def _actor(label: str, *, writing: bool = False):
        from src.automation.actions import ActionError, Actor
        from src.mcp_server.identity import resolve_caller

        caller = resolve_caller()
        if writing and caller.authenticated and not caller.workspace_resolved:
            # A client_credentials token whose owner cannot be resolved lands
            # on the "default" workspace by fallback. Reading there is
            # harmless; writing would register a repository into a tenant
            # nobody chose. Refuse and say what to fix.
            raise ActionError(
                "This token is not tied to a workspace. Register the OAuth "
                "client from an account that belongs to the workspace you "
                "want to write to, or use a user token.",
            )
        return Actor(
            user_id=caller.user_id,
            email=getattr(caller, "email", "") or caller.user_id,
            workspace_id=caller.workspace_id,
            label=label,
        )


    async def _in_session(fn):
        """FastMCP awaits async tool bodies, so there is no loop to juggle —
        the earlier version opened its own and died with "Cannot run the event
        loop while another loop is running" the first time it was called over
        HTTP."""
        from src.db import async_session

        async with async_session() as session:
            return await fn(session)

    @mcp.tool(
        name="add_repo",
        description=(
            "Register a repository in the caller's workspace so it can be "
            "indexed, audited and reviewed. Accepts a full URL, 'owner/name', "
            "or 'provider:owner/name'. Optional branch — empty means the "
            "provider's default. Idempotent: registering an already-registered "
            "repo returns it with already_registered=true. index (default "
            "true) queues the code-graph build; pass false only for bulk "
            "registration that must not clone. The reply carries index_status "
            "— queued | already_queued | already_indexed | not_requested | "
            "queue_unavailable — so an unindexed repo is never a silent one."
        ),
    )
    @require_scopes("write:repos")
    def _add_repo(
        url: str, branch: str | None = None, index: bool = True,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, register_repo

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **register_repo(actor, url, branch, index=index)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="start_dep_audit",
        description=(
            "Queue a dependency + vulnerability audit over registered repos. "
            "repo_slugs empty = every repo in the workspace. owner filters by "
            "the 'owner/' prefix. branch overrides the branch for this run "
            "only. report_engine: none | api | claude_code — who writes the "
            "prose summary; the findings themselves are always deterministic. "
            "Returns a run_id to poll with get_dep_audit."
        ),
    )
    @require_scopes("write:repos")
    async def _start_dep_audit(
        repo_slugs: list[str] | None = None,
        owner: str | None = None,
        branch: str | None = None,
        report_engine: str = "none",
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, start_dep_audit

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **await _in_session(
                lambda s: start_dep_audit(
                    actor, s, repo_slugs=repo_slugs, owner=owner,
                    branch=branch, report_engine=report_engine,
                )
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="get_dep_audit",
        description=(
            "Status and summary of a dependency audit. Omit run_id for the "
            "latest run in the workspace. status is queued|running|done|error; "
            "poll until it leaves queued/running, then read list_dep_findings."
        ),
    )
    @require_scopes("read:graph")
    async def _get_dep_audit(run_id: str | None = None) -> dict[str, Any]:
        from src.automation.actions import ActionError, get_dep_audit

        actor = _actor("mcp")
        try:
            return {"ok": True, **await _in_session(
                lambda s: get_dep_audit(actor, s, run_id)
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="list_dep_findings",
        description=(
            "Findings of one audit run, worst severity first: package, "
            "installed vs latest version, severity, recommendation. Optional "
            "severity filter (critical|high|medium|low). This is the material "
            "to write a report from."
        ),
    )
    @require_scopes("read:graph")
    async def _list_dep_findings(
        run_id: str, severity: str | None = None, limit: int = 100,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, list_dep_findings

        actor = _actor("mcp")
        try:
            rows = await _in_session(
                lambda s: list_dep_findings(
                    actor, s, run_id, severity=severity, limit=limit,
                )
            )
            return {"ok": True, "findings": rows, "count": len(rows)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    logger.info("mcp_server_built name=%s", SERVER_NAME)
    return mcp


def run_server(
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio",
    port: int | None = None,
    *,
    enable_auth: bool = False,
) -> None:
    """Run MCP server. Blocking call.

    Stdio (default): JSON-RPC over stdin/stdout. Used by Claude Code / Cursor.
        WARNING: do NOT write logs to stdout — that breaks JSON-RPC. Logging has
        to go to stderr.
        Auth is not needed — the subprocess is the trust boundary.

    Streamable HTTP: HTTP transport with optional OAuth/JWT auth. Default port = 8000.
        Production: enable_auth=True, requires the MCP_JWT_SECRET env var.
    """
    if enable_auth and transport == "stdio":
        logger.warning(
            "auth_enabled_but_stdio_transport — auth is ignored for stdio "
            "(the subprocess is the trust boundary). For production usage — streamable-http."
        )
        enable_auth = False

    mcp = build_server(enable_auth=enable_auth)

    if transport == "streamable-http" and port is not None:
        # FastMCP determines the port through settings — for FastMCP 1.23.x
        # that goes through mount_path or an envvar. Simple strategy: use the
        # default 8000 and document that the port override goes through the
        # FASTMCP_PORT env var.
        import os
        os.environ["FASTMCP_PORT"] = str(port)

    mcp.run(transport=transport)
