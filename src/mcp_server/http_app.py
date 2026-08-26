"""HTTP-mounted MCP server (Stage 13).

Wraps the existing STDIO-focused server (src.mcp_server.server) into a
Streamable-HTTP ASGI app that we mount on FastAPI at ``/mcp/``. This is
what lets a user point Claude Code / Cursor / any MCP client at
``http://localhost:8001/mcp/`` instead of spawning a local subprocess.

Design decisions:

  * FastMCP's ``streamable_http_app()`` returns a Starlette app — mounts
    cleanly under FastAPI.
  * Auth uses the existing Bearer JWT (``CELMIS_JWT_SECRET`` — same
    key that signs /api/... calls). One token, two surfaces.
  * Tool set = a *composite-workflow* selection (see MCP anti-patterns
    research): not all raw CRUD. Focused on the killer use case — Claude
    Code writing a new service in one repo while querying its sibling
    repos in the same project for API surfaces, callers, existing
    handlers.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _transport_security():
    """Hosts the MCP transport will answer to.

    The SDK refuses a request whose Host header it does not recognise — DNS
    rebinding protection, and correct. Its default list is localhost only, so
    a deployed instance answers every MCP call with "Invalid Host header" and
    the entire surface is unreachable from outside the box. That is what this
    fixes, without turning the check off.

    The allowed list is localhost plus the host this instance is actually
    served on: CELMIS_PUBLIC_BASE_URL when set, and anything named explicitly
    in MCP_ALLOWED_HOSTS (comma-separated) for a deployment behind a proxy
    that rewrites Host. Both spellings of the public URL are read. Returns None when the SDK has no such setting, so an
    older MCP package keeps its own behaviour.
    """
    import os
    from urllib.parse import urlparse

    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:
        return None

    hosts = {"localhost", "127.0.0.1", "localhost:8000", "127.0.0.1:8000"}
    origins = {"http://localhost", "http://127.0.0.1"}

    # Both spellings: the Settings field is CELMIS_PUBLIC_BASE_URL, while
    # compose passes the bare PUBLIC_BASE_URL into the container.
    public = next(
        (v.strip() for v in (os.environ.get("CELMIS_PUBLIC_BASE_URL"),
                             os.environ.get("PUBLIC_BASE_URL")) if (v or "").strip()),
        "",
    )
    if public:
        parsed = urlparse(public if "://" in public else f"http://{public}")
        if parsed.hostname:
            hosts.add(parsed.netloc)
            hosts.add(parsed.hostname)
            origins.add(f"{parsed.scheme}://{parsed.netloc}")

    for extra in (os.environ.get("MCP_ALLOWED_HOSTS", "") or "").split(","):
        extra = extra.strip()
        if extra:
            hosts.add(extra)
            origins.add(f"http://{extra}")
            origins.add(f"https://{extra}")

    return TransportSecuritySettings(
        allowed_hosts=sorted(hosts), allowed_origins=sorted(origins),
    )


def _build_mcp() -> FastMCP:  # noqa: F821 — quoted for typing without an import when the package is absent
    """Build the FastMCP instance with project-aware + review tools.

    Import happens inside the function so a missing `mcp` package
    degrades to a no-op mount instead of crashing app startup during
    a partial install / dev environment.
    """
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import FastMCP

    from src.mcp_server import tools as legacy_tools
    from src.mcp_server.auth import JwtTokenVerifier

    # Reuse the same JWT verifier the API uses (CELMIS_JWT_SECRET).
    #
    # SECURITY: this used to fall back to an unauthenticated server whenever
    # the verifier failed to build. That is a fail-OPEN: an unauthenticated
    # caller resolves to an admin-equivalent principal, so every research
    # access rule is bypassed and the whole graph is readable. A misconfigured
    # secret must therefore refuse to serve, not serve everything.
    #
    # The unauthenticated mode still exists for stdio / offline local dev, but
    # it now requires an explicit opt-in so it can never happen by accident.
    try:
        verifier = JwtTokenVerifier()
        auth_settings = AuthSettings(
            issuer_url=f"https://{verifier.config.issuer}",
            resource_server_url=f"https://{verifier.config.audience}",
            required_scopes=[],
        )
        mcp = FastMCP(
            "celmis",
            instructions=(
                "Celmis code intelligence — query projects and their "
                "repos, look up cross-repo API surfaces, callers, and "
                "recent review findings. Designed for Claude Code to "
                "write new services with awareness of sibling services in "
                "the same project."
            ),
            token_verifier=verifier,
            # Serve at the sub-app ROOT. FastMCP defaults this to "/mcp", and
            # we mount the sub-app under "/mcp" as well — the two concatenate
            # into "/mcp/mcp", so every client hitting "/mcp" got 307 → 404 and
            # silently ran with zero Celmis tools.
            streamable_http_path="/",
            auth=auth_settings,
            **({"transport_security": _security} if (_security := _transport_security()) else {}),
        )
    except Exception as exc:  # noqa: BLE001
        import os as _os

        if _os.environ.get("MCP_ALLOW_UNAUTHENTICATED", "").strip().lower() not in (
            "1", "true", "yes",
        ):
            logger.error(
                "mcp_auth_unavailable err=%s — refusing to start an "
                "unauthenticated MCP server. Set MCP_JWT_SECRET (or "
                "CELMIS_JWT_SECRET), or set MCP_ALLOW_UNAUTHENTICATED=1 "
                "to explicitly accept an open server.",
                exc,
            )
            raise
        logger.warning(
            "mcp_unauthenticated_mode err=%s — MCP_ALLOW_UNAUTHENTICATED is "
            "set, so the server runs WITHOUT auth and every caller has full "
            "research access. Never use this outside local development.",
            exc,
        )
        mcp = FastMCP(
            "celmis",
            instructions=(
                "Celmis code intelligence — query projects, cross-repo "
                "symbols, and review findings."
            ),
            streamable_http_path="/",  # see the note above
        )

    _register_tools(mcp, legacy_tools)
    _install_scope_filter(mcp)
    return mcp


# Scope required to SEE each tool in tools/list (GitHub MCP pattern —
# hide what the token can't call). Tokens with NO scopes (legacy full-
# access JWTs) see everything; scoped tokens see the intersection.
_TOOL_SCOPES: dict[str, str] = {
    "list_projects": "read:graph",
    "get_project": "read:graph",
    "search_symbols": "read:graph",
    "find_consumers": "read:graph",
    "get_api_surface": "read:graph",
    "get_owner": "read:graph",
    "get_architecture": "read:graph",
    "list_deprecations": "read:graph",
    "start_integration_walk": "read:graph",
    "bootstrap_client": "read:graph",
    "route_incident": "read:graph",
    "list_accessible_repos": "read:graph",
    "get_my_access": "read:graph",
    "get_review": "read:reviews",
    "get_review_policy": "read:reviews",
    "migrate_consumers": "write:reviews",
    # The write half of the automation surface. `write:repos` is a scope no
    # token issued for reading carries, so these are invisible to a read
    # client rather than merely refused.
    "add_repo": "write:repos",
    "start_dep_audit": "write:repos",
    "generate_docs": "write:repos",
    "set_auto_review": "write:repos",
    "list_workspace_repos": "read:graph",
    "get_dep_audit": "read:graph",
    "list_dep_findings": "read:graph",
}


def _install_scope_filter(mcp) -> None:  # noqa: ANN001
    """Wrap the low-level list_tools handler so scoped tokens only see
    tools their scopes allow. Defensive: if the SDK's auth-context API
    isn't available (older SDK / stdio transport), we leave the default
    handler untouched."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:
        logger.info("mcp_scope_filter_skipped reason=no_auth_context_api")
        return
    try:
        inner = mcp._mcp_server  # low-level Server
        original_handler = inner.request_handlers.get(
            __import__("mcp.types", fromlist=["ListToolsRequest"]).ListToolsRequest
        )
        if original_handler is None:
            logger.info("mcp_scope_filter_skipped reason=no_list_handler")
            return

        async def filtered_handler(req):  # noqa: ANN001
            result = await original_handler(req)
            try:
                token = get_access_token()
                scopes = set(token.scopes or []) if token else set()
            except Exception:  # noqa: BLE001
                scopes = set()
            if not scopes:
                return result  # legacy full-access token — show everything
            tools_result = result.root
            tools_result.tools = [
                t for t in tools_result.tools
                if _TOOL_SCOPES.get(t.name, "") in scopes
                or _TOOL_SCOPES.get(t.name) is None
            ]
            return result

        inner.request_handlers[
            __import__("mcp.types", fromlist=["ListToolsRequest"]).ListToolsRequest
        ] = filtered_handler
        logger.info("mcp_scope_filter_installed tools=%d", len(_TOOL_SCOPES))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_scope_filter_failed err=%s — default listing kept", exc)


def _register_tools(mcp, legacy_tools) -> None:  # noqa: ANN001
    """Register composite-workflow tools on the FastMCP instance."""

    # ─── Project catalog ──────────────────────────────────────────────

    @mcp.tool(
        name="list_projects",
        description=(
            "List all projects (multi-repo bundles). Each project groups "
            "related repositories that share domain / owning team so "
            "cross-repo lookups make sense. Returns id, name, "
            "description, repo count."
        ),
    )
    def _list_projects() -> dict[str, Any]:
        from src.mcp_server.identity import resolve_caller

        return _list_projects_impl(resolve_caller().workspace_id)

    @mcp.tool(
        name="get_project",
        description=(
            "Return one project's full detail: repos (slugs + roles), "
            "descriptions, chat count. Use this to enumerate the repos "
            "you should also query when writing code for one of them."
        ),
    )
    def _get_project(project_id: str) -> dict[str, Any]:
        from src.mcp_server.identity import resolve_caller

        return _get_project_impl(project_id, resolve_caller().workspace_id)

    # ─── Cross-repo code search (project-scoped) ──────────────────────

    @mcp.tool(
        name="search_symbols",
        description=(
            "Search for a function/class/endpoint by name across every "
            "repo in a project. Returns file path, line, kind, and the "
            "repo slug so you can follow up with find_callers / "
            "get_symbol. Use before writing new code so you do not "
            "reimplement an existing symbol."
        ),
    )
    def _search_symbols(
        project_id: str,
        query: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return _search_symbols_impl(project_id, query, kind, limit)

    @mcp.tool(
        name="find_consumers",
        description=(
            "Find which repos in the project call a given symbol (by "
            "fully-qualified name or repo_slug+symbol). This is the key "
            "safety net when refactoring a shared endpoint/schema — the "
            "returned list is who breaks if you change signature."
        ),
    )
    def _find_consumers(project_id: str, symbol: str) -> dict[str, Any]:
        return _find_consumers_impl(project_id, symbol)

    # ─── API surface (endpoints/handlers) ─────────────────────────────

    @mcp.tool(
        name="get_api_surface",
        description=(
            "Return the HTTP/RPC handlers detected in `repo_slug` — path "
            "pattern, method, function name, file:line. Optional "
            "path_glob filter (e.g. `api/users/*`). Use this before "
            "writing a new endpoint, to check whether an equivalent "
            "already exists."
        ),
    )
    def _get_api_surface(
        repo_slug: str,
        path_glob: str | None = None,
    ) -> dict[str, Any]:
        return _get_api_surface_impl(repo_slug, path_glob)

    # ─── Stage 22: research-access introspection ──────────────────────

    @mcp.tool(
        name="list_accessible_repos",
        description=(
            "List repos the CURRENT caller may research, with visibility "
            "level (metadata|code) and any deny-path patterns. Use this "
            "first to know what you are allowed to investigate — repos you "
            "cannot research are omitted. Mirrors the human UI + REST "
            "/api/access/my so agents and people see the same boundaries."
        ),
    )
    def _list_accessible_repos() -> dict[str, Any]:
        return _list_accessible_repos_impl()

    @mcp.tool(
        name="get_my_access",
        description=(
            "Return the caller's effective research access for `repo_slug`: "
            "whether it is researchable, whether source code is visible, and "
            "which path globs are denied (creds/crypto/db). Use to explain "
            "to the user why some detail is out of bounds."
        ),
    )
    def _get_my_access(repo_slug: str) -> dict[str, Any]:
        from src.mcp_server.identity import caller_access
        caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        payload = dec.to_dict() if dec else {"repo_slug": repo_slug}
        payload["authenticated"] = caller.authenticated
        return payload

    # ─── Review integration ───────────────────────────────────────────

    @mcp.tool(
        name="get_review",
        description=(
            "Fetch the latest review run for a PR reference like "
            "`gitlab:owner/repo#42` or a full URL. Returns verdict, "
            "findings (severity, agent, file:line, suggestion), and cost. "
            "Use to see whether a PR passed, or to trigger a fresh run."
        ),
    )
    def _get_review(pr_ref: str) -> dict[str, Any]:
        return _get_review_impl(pr_ref)

    @mcp.tool(
        name="get_review_policy",
        description=(
            "Read the effective review policy for `repo_slug`: enabled "
            "flag, target branches, natural-language rules, folder "
            "rules, per-agent model overrides, per-agent prompt "
            "overrides. Use to know what the AI reviewer will enforce "
            "before opening the PR."
        ),
    )
    def _get_review_policy(repo_slug: str) -> dict[str, Any]:
        return _get_review_policy_impl(repo_slug)

    # ─── Stage 15: cross-team intelligence ───────────────────────────

    @mcp.tool(
        name="get_owner",
        description=(
            "Return owner info for a file (or fallback ancestor dir) in "
            "`repo_slug`. Data: top git-blame authors from the last N "
            "days, matched CODEOWNERS entries, and the single "
            "'primary_owner' — the top committer identity. Use when you "
            "need to know who to page / cc on a change."
        ),
    )
    def _get_owner(repo_slug: str, path: str) -> dict[str, Any]:
        from src.mcp_server.identity import caller_access
        _caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        if dec is not None and (not dec.researchable or not dec.path_visible(path)):
            return {"repo_slug": repo_slug, "path": path,
                    "blocked_repos": [repo_slug],
                    "access_notice": _boundary_note([repo_slug])}
        from src.ownership.builder import lookup_owner
        result = lookup_owner(repo_slug, path)
        if result is None:
            return {"repo_slug": repo_slug, "path": path,
                    "error": "no ownership snapshot; call rebuild first"}
        return {"repo_slug": repo_slug, "path": path, **result}

    @mcp.tool(
        name="get_architecture",
        description=(
            "Return the cached architecture summary (markdown) for "
            "`repo_slug`. This is a compact orientation doc — entry "
            "points, main flows, ownership, integrations. Regenerate "
            "from UI or via POST /api/intel/architecture/{slug}/rebuild."
        ),
    )
    def _get_architecture(repo_slug: str) -> dict[str, Any]:
        from src.mcp_server.identity import caller_access
        _caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        if dec is not None and not dec.researchable:
            return {"repo_slug": repo_slug, "summary_md": "",
                    "blocked_repos": [repo_slug],
                    "access_notice": _boundary_note([repo_slug])}
        from sqlalchemy.orm import Session

        from src.db.models import RepoSummary
        with Session(_sync_engine()) as s:
            row = s.get(RepoSummary, repo_slug)
            if row is None:
                return {"repo_slug": repo_slug, "summary_md": "",
                        "note": "no summary yet — trigger rebuild"}
            return {
                "repo_slug": row.repo_slug,
                "summary_md": row.summary_md,
                "model_used": row.model_used,
                "computed_at": row.computed_at.isoformat() if row.computed_at else None,
            }

    @mcp.tool(
        name="list_deprecations",
        description=(
            "List every tracked deprecated symbol with its known "
            "consumers, replacement (if any) and target removal date. "
            "Optional filter `repo_slug` narrows to one repo."
        ),
    )
    def _list_deprecations(repo_slug: str | None = None) -> dict[str, Any]:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from src.db.models import DeprecatedSymbol
        with Session(_sync_engine()) as s:
            stmt = select(DeprecatedSymbol)
            if repo_slug:
                stmt = stmt.where(DeprecatedSymbol.repo_slug == repo_slug)
            rows = s.execute(stmt).scalars().all()
            return {"deprecations": [
                {
                    "id": r.id, "repo_slug": r.repo_slug, "symbol": r.symbol,
                    "reason": r.reason, "replacement": r.replacement,
                    "target_removal_at": r.target_removal_at.isoformat() if r.target_removal_at else None,
                    "consumers": list(r.consumers or []),
                }
                for r in rows
            ]}

    @mcp.tool(
        name="bootstrap_client",
        description=(
            "Given a project + target repo you want to integrate with, "
            "return: (1) the target's public API surface, (2) existing "
            "usage examples in sibling repos, (3) suggested client "
            "stub. Optional `target_endpoint` narrows to one endpoint. "
            "This is the killer tool: it lets you write an integration "
            "against team B's service without paging team B."
        ),
    )
    def _bootstrap_client(
        project_id: str,
        target_repo_slug: str,
        target_endpoint: str | None = None,
        language: str = "typescript",
    ) -> dict[str, Any]:
        return _bootstrap_client_impl(
            project_id=project_id,
            target_repo_slug=target_repo_slug,
            target_endpoint=target_endpoint,
            language=language,
        )

    @mcp.tool(
        name="start_integration_walk",
        description=(
            "Return an ordered checklist for a cross-team integration "
            "against `target_repo_slug` in the given project. Steps "
            "include reading the architecture summary, listing owners, "
            "picking endpoints, and drafting a client. Use this at the "
            "start of a new integration task instead of firing 10 "
            "separate tool calls."
        ),
    )
    def _start_integration_walk(
        project_id: str, target_repo_slug: str, goal: str = "integrate",
    ) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "target_repo_slug": target_repo_slug,
            "goal": goal,
            "steps": [
                {"step": 1, "tool": "get_project",
                 "args": {"project_id": project_id},
                 "why": "confirm the project + list sibling repos"},
                {"step": 2, "tool": "get_architecture",
                 "args": {"repo_slug": target_repo_slug},
                 "why": "read the auto-generated summary — orient before diving in"},
                {"step": 3, "tool": "get_api_surface",
                 "args": {"repo_slug": target_repo_slug},
                 "why": "enumerate endpoints/handlers you may call"},
                {"step": 4, "tool": "search_symbols",
                 "args": {"project_id": project_id, "query": goal, "limit": 5},
                 "why": "find prior implementations of this pattern in sibling repos"},
                {"step": 5, "tool": "list_deprecations",
                 "args": {"repo_slug": target_repo_slug},
                 "why": "avoid picking symbols that are on the removal list"},
                {"step": 6, "tool": "get_owner",
                 "args": {"repo_slug": target_repo_slug, "path": "<file you touch>"},
                 "why": "identify the human owner in case a design question comes up"},
                {"step": 7, "tool": "bootstrap_client",
                 "args": {"project_id": project_id,
                          "target_repo_slug": target_repo_slug,
                          "target_endpoint": "<pick from step 3>",
                          "language": "typescript"},
                 "why": "generate the initial client stub"},
            ],
        }

    @mcp.tool(
        name="migrate_consumers",
        description=(
            "Bulk-apply a text replacement across all consumers of "
            "`symbol` in the given project. For each consumer repo we "
            "open branch `celmis-migrate/<symbol>-<n>` and commit "
            "`old_text` → `new_text` on the file/line where the "
            "consumer sits. Best-effort: any repo whose provider isn't "
            "wired for apply-fix (currently only github) is reported as "
            "skipped. Use for coordinated renames or v1→v2 API rollouts."
        ),
    )
    def _migrate_consumers(
        project_id: str,
        symbol: str,
        old_text: str,
        new_text: str,
        user_id: str,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        return _migrate_consumers_impl(
            project_id=project_id, symbol=symbol,
            old_text=old_text, new_text=new_text,
            user_id=user_id, commit_message=commit_message,
        )

    @mcp.tool(
        name="route_incident",
        description=(
            "Given a stack trace (Sentry-style or plain text) referencing "
            "files inside `repo_slug`, return the owner(s) most likely "
            "to be responsible. Uses the ownership snapshot. Optional "
            "`title` is used only in the response payload."
        ),
    )
    def _route_incident(
        repo_slug: str, stack_trace: str, title: str = "",
    ) -> dict[str, Any]:
        # Stage 22: same gate as get_owner — ownership/blame/CODEOWNERS must
        # not leak for repos/paths the caller may not research.
        from src.mcp_server.identity import caller_access
        _caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        if dec is not None and not dec.researchable:
            return {"repo_slug": repo_slug, "title": title,
                    "blocked_repos": [repo_slug],
                    "access_notice": _boundary_note([repo_slug])}
        from src.ownership.builder import lookup_owner
        paths: list[str] = []
        import re as _re
        for m in _re.finditer(r"[\w./-]+\.\w{1,4}", stack_trace):
            f = m.group(0)
            if "/" in f and f not in paths:
                paths.append(f)
        if not paths:
            return {"error": "no file paths detected in stack trace"}
        owners: list[dict[str, Any]] = []
        hidden = 0
        for f in paths[:10]:
            if dec is not None and not dec.path_visible(f):
                hidden += 1
                continue
            info = lookup_owner(repo_slug, f)
            if info:
                owners.append({"path": f, **info})
        out = {"repo_slug": repo_slug, "title": title,
               "paths_scanned": paths[:10], "owners": owners}
        if hidden:
            out["hidden_path_count"] = hidden
        return out


# ─── Tool implementations ────────────────────────────────────────────


    # ─── Automation: register, audit, read back ───────────────────────
    #
    # The verbs that let an external Claude Code do the loop instead of
    # describing it: take the repositories somebody listed, audit them, read
    # the findings. Bodies live in src.automation.actions so the HTTP API, this
    # server and the coming ticket connector share one implementation of the
    # tenancy check and the live-run rule.

    def _actor(label: str, *, writing: bool = False):
        from src.automation.actions import ActionError, Actor
        from src.mcp_server.identity import resolve_caller

        caller = resolve_caller()
        if writing and caller.authenticated and not caller.workspace_resolved:
            # A client_credentials token whose owner cannot be resolved lands
            # on the "default" workspace by fallback. Reading there is
            # harmless; writing would register a repository into a tenant
            # nobody chose.
            raise ActionError(
                "This token is not tied to a workspace. Register the OAuth "
                "client from an account that belongs to the workspace you "
                "want to write to.",
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
            "indexed, audited and reviewed. Accepts a full URL, 'owner/name' "
            "or 'provider:owner/name'. branch empty = the provider default. "
            "Idempotent — an already-registered repo comes back with "
            "already_registered=true. index (default true) queues the "
            "code-graph build; pass false only for bulk registration that must "
            "not clone. The reply carries index_status — queued | "
            "already_queued | already_indexed | not_requested | "
            "queue_unavailable — so an unindexed repo is never a silent one."
        ),
    )
    def _add_repo(
        url: str, branch: str | None = None, index: bool = True,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, register_repo

        try:
            return {"ok": True, **register_repo(
                _actor("mcp", writing=True), url, branch, index=index,
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="start_dep_audit",
        description=(
            "Queue a dependency + vulnerability audit over registered repos. "
            "repo_slugs empty = every repo in the workspace; owner filters by "
            "the 'owner/' prefix; branch overrides the branch for this run "
            "only. report_engine none|api|claude_code chooses who writes the "
            "prose — the findings are always deterministic. Returns run_id."
        ),
    )
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
        name="generate_docs",
        description=(
            "Queue documentation (module PRDs, feature docs, integration "
            "guides) for a SET of repositories. repo_slugs names them "
            "explicitly; owner selects everything under an 'owner/' prefix; "
            "missing_only restricts it to repositories that have none yet — "
            "which is usually what is meant, because without it the same "
            "request regenerates everything. Omit all three to mean the whole "
            "workspace. language and engine (api|claude_code) override the "
            "workspace defaults for this run only. Returns which repositories "
            "were queued and which were skipped, with a reason for each — a "
            "repository that is not indexed yet is refused rather than "
            "documented from its filenames."
        ),
    )
    async def _generate_docs(
        repo_slugs: list[str] | None = None,
        owner: str | None = None,
        missing_only: bool = False,
        language: str | None = None,
        engine: str | None = None,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, generate_docs

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **await _in_session(
                lambda s: generate_docs(
                    actor, s, repo_slugs=repo_slugs, owner=owner,
                    missing_only=missing_only, language=language, engine=engine,
                )
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="set_auto_review",
        description=(
            "Turn automatic pull-request review on or off for a SET of "
            "repositories, and optionally pin the branch. repo_slugs names "
            "them explicitly; owner selects everything under an 'owner/' "
            "prefix; omit both to mean the whole workspace. branch is not a "
            "filter — it is the ref every surface then reads, so setting it "
            "changes what indexing and dependency audits see too. Refuses "
            "above a cap: this changes what happens to every future pull "
            "request, so a misread 'turn it on everywhere' must not arm an "
            "estate."
        ),
    )
    async def _set_auto_review(
        repo_slugs: list[str] | None = None,
        owner: str | None = None,
        enabled: bool = True,
        branch: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, set_auto_review

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **set_auto_review(
                actor, repo_slugs=repo_slugs, owner=owner, enabled=enabled,
                branch=branch, mode=mode,
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="list_workspace_repos",
        description=(
            "OPERATIONAL state of every repository in the workspace: "
            "indexed, documented, automatic review on or off, and which "
            "branch it reads. Run this before any verb that takes "
            "repo_slugs — a slug invented rather than read is refused. "
            "Not the same question as list_accessible_repos, which answers "
            "what the CALLER is allowed to research; this one answers what "
            "the workspace has and what state it is in."
        ),
    )
    async def _list_workspace_repos() -> dict[str, Any]:
        from src.automation.actions import ActionError, list_repos

        try:
            return {"ok": True, **list_repos(_actor("mcp"))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="get_dep_audit",
        description=(
            "Status and summary of a dependency audit. Omit run_id for the "
            "latest run. status is queued|running|done|error — poll until it "
            "leaves queued/running, then read list_dep_findings."
        ),
    )
    async def _get_dep_audit(run_id: str | None = None) -> dict[str, Any]:
        from src.automation.actions import ActionError, get_dep_audit

        try:
            actor = _actor("mcp")
            return {"ok": True, **await _in_session(
                lambda s: get_dep_audit(actor, s, run_id)
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="list_dep_findings",
        description=(
            "Findings of one audit run, worst severity first: package, "
            "installed vs latest, severity, recommendation. Optional severity "
            "filter. This is the material a report is written from."
        ),
    )
    async def _list_dep_findings(
        run_id: str, severity: str | None = None, limit: int = 100,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError, list_dep_findings

        try:
            actor = _actor("mcp")
            rows = await _in_session(
                lambda s: list_dep_findings(
                    actor, s, run_id, severity=severity, limit=limit,
                )
            )
            return {"ok": True, "findings": rows, "count": len(rows)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

def _run_async(coro):
    """Run coroutine to completion from sync tool context.

    Historic bridge — kept only for backward-compat with callers that
    still pass a coroutine. New callers should be plain-sync (see the
    ``_sync_*`` helpers below). Falls back to a fresh loop in a worker
    thread so we don't inherit connections bound to a different loop.
    """
    import asyncio
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _sync_engine():
    """Cached sync engine — every MCP DB tool uses this to sidestep the
    async loop-affinity trap (asyncpg pool is bound to the loop that
    created it; MCP tools may run on a different loop / thread).
    """
    global _SYNC_ENGINE
    try:
        return _SYNC_ENGINE  # type: ignore[name-defined]
    except NameError:
        pass
    from sqlalchemy import create_engine

    from src.db.session import get_database_url
    url = get_database_url().replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"
    )
    _SYNC_ENGINE = create_engine(url, pool_pre_ping=True, pool_recycle=1800)  # noqa: F841
    return _SYNC_ENGINE


def _list_projects_impl(workspace_id: str) -> dict[str, Any]:
    """Projects of ONE tenant.

    This ran `select(Project)` with no filter while its REST twin
    (src/api/routers/projects.py:80) has always had
    `.where(Project.workspace_id == ws_id)`. So a token issued to one
    workspace listed every workspace's projects — names, descriptions and
    repository counts — and `get_project` then opened any of them by id.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import Project, ProjectRepo
    with Session(_sync_engine()) as s:
        rows = s.execute(
            select(Project).where(Project.workspace_id == workspace_id)
        ).scalars().all()
        out = []
        for p in rows:
            repos = s.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == p.id)
            ).scalars().all()
            out.append({
                "id": str(p.id),
                "name": p.name,
                "description": p.description,
                "repo_count": len(repos),
            })
        return {"projects": out, "count": len(out)}


def _get_project_impl(project_id: str, workspace_id: str) -> dict[str, Any]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import Project, ProjectRepo
    with Session(_sync_engine()) as s:
        p = s.get(Project, project_id)
        # Not found rather than forbidden: a tenant asking about a project it
        # does not own must not learn that the id exists.
        if p is None or p.workspace_id != workspace_id:
            return {"error": f"project {project_id!r} not found"}
        repos = s.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == p.id)
        ).scalars().all()
        return {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "repos": [
                {"repo_slug": r.repo_slug, "role": r.role}
                for r in repos
            ],
        }


def _project_repo_slugs(project_id: str) -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ProjectRepo
    with Session(_sync_engine()) as s:
        rows = s.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == project_id)
        ).scalars().all()
        return [r.repo_slug for r in rows]


# ── legacy-tool result normalizers ──────────────────────────────────
# tools.find_symbol → list[dict] (asdict of SymbolInfo + repo_slug):
#   keys name/kind/file/start_line/signature/... — NOT objects.
# tools.find_callers → dict {repo, target_id, callers: [dict], ...}:
#   caller dicts have keys id/name/kind/file/start_line.


def _legacy_find_symbol(name, slug, *, kind=None, limit=20):  # noqa: ANN001
    from src.mcp_server import tools as legacy
    rows = legacy.find_symbol(name=name, repo_slug=slug, limit=limit)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows


def _legacy_callers(symbol, slug):  # noqa: ANN001
    from src.mcp_server import tools as legacy
    res = legacy.find_callers(symbol_id=symbol, repo_slug=slug)
    return (res or {}).get("callers", []) if isinstance(res, dict) else []


def _boundary_note(blocked: list[str]) -> str:
    if not blocked:
        return ""
    names = ", ".join(sorted(set(blocked)))
    return (
        f"Part of this functionality lives in repositories [{names}], which "
        "you do not have research access to. Ask your administrators to "
        "grant access."
    )


def _search_symbols_impl(
    project_id: str, query: str, kind: str | None, limit: int,
) -> dict[str, Any]:
    repo_slugs = _project_repo_slugs(project_id)
    if not repo_slugs:
        return {"error": f"project {project_id!r} has no repos", "matches": []}
    # Stage 22: gate by the caller's research access.
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access(repo_slugs)

    matches: list[dict[str, Any]] = []
    blocked_repos: list[str] = []
    hidden = 0
    for slug in repo_slugs:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            blocked_repos.append(slug)
            continue
        try:
            for sym in _legacy_find_symbol(query, slug, kind=kind, limit=limit):
                fpath = sym.get("file") or ""
                if dec is not None and fpath and not dec.path_visible(fpath):
                    hidden += 1
                    continue
                matches.append({
                    "repo_slug": slug,
                    "name": sym.get("name"),
                    "kind": sym.get("kind"),
                    "file": fpath,
                    "line": sym.get("start_line"),
                    "signature": sym.get("signature"),
                })
                if len(matches) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("search_symbols_repo_failed repo=%s err=%s", slug, exc)
        if len(matches) >= limit:
            break
    out: dict[str, Any] = {"query": query, "matches": matches, "count": len(matches)}
    if blocked_repos:
        out["blocked_repos"] = sorted(set(blocked_repos))
        out["access_notice"] = _boundary_note(blocked_repos)
    if hidden:
        out["hidden_symbol_count"] = hidden
    return out


def _find_consumers_impl(project_id: str, symbol: str) -> dict[str, Any]:
    repo_slugs = _project_repo_slugs(project_id)
    if not repo_slugs:
        return {"error": f"project {project_id!r} has no repos"}
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access(repo_slugs)

    consumers: list[dict[str, Any]] = []
    blocked_repos: list[str] = []
    for slug in repo_slugs:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            blocked_repos.append(slug)
            continue
        try:
            for c in _legacy_callers(symbol, slug):
                fpath = c.get("file", "") or ""
                if dec is not None and fpath and not dec.path_visible(fpath):
                    continue
                consumers.append({
                    "repo_slug": slug,
                    "symbol": c.get("name"),
                    "file": fpath,
                    "line": c.get("start_line", 0),
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("find_consumers_repo_failed repo=%s err=%s", slug, exc)
    out: dict[str, Any] = {
        "symbol": symbol, "consumers": consumers, "count": len(consumers),
    }
    if blocked_repos:
        out["blocked_repos"] = sorted(set(blocked_repos))
        out["access_notice"] = _boundary_note(blocked_repos)
    return out


def _get_api_surface_impl(
    repo_slug: str, path_glob: str | None,
) -> dict[str, Any]:
    """Detect HTTP endpoints via existing symbol index. Heuristic — flags
    functions decorated with FastAPI/Flask/Express-like routers by name
    convention. Falls back to empty list if graph missing."""
    import fnmatch

    # Stage 22: block repos the caller may not research; hide denied files.
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access([repo_slug])
    dec = access.get(repo_slug)
    if dec is not None and not dec.researchable:
        return {
            "repo_slug": repo_slug, "endpoints": [], "count": 0,
            "blocked_repos": [repo_slug],
            "access_notice": _boundary_note([repo_slug]),
        }

    endpoints: list[dict[str, Any]] = []
    try:
        # Match common HTTP-handler prefixes.
        for prefix in ("get_", "post_", "put_", "delete_", "patch_", "route_"):
            for sym in _legacy_find_symbol(prefix, repo_slug, kind="function"):
                name = sym.get("name") or ""
                fpath = sym.get("file") or ""
                if dec is not None and fpath and not dec.path_visible(fpath):
                    continue
                path_hint = _infer_route_path(name)
                if path_glob and not fnmatch.fnmatch(path_hint, path_glob):
                    continue
                endpoints.append({
                    "path": path_hint,
                    "method": prefix.rstrip("_").upper(),
                    "handler": name,
                    "file": fpath,
                    "line": sym.get("start_line"),
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug("api_surface_failed repo=%s err=%s", repo_slug, exc)
    return {
        "repo_slug": repo_slug,
        "endpoints": endpoints,
        "count": len(endpoints),
    }


def _to_indexed_slug(candidate: str | None) -> str | None:
    """Map whatever identifies a repo to the slug the index actually uses.

    A PR ref yields ``owner/repo``; the graph/vault key it by the local slug
    (``gitlab_owner-name``). Comparing the two forms never matches, which
    turns an access check into a silent no-op — so resolve here instead.

    Order: exact indexed slug → canonical ``parse_repo_url`` form → a
    punctuation-insensitive suffix match. Returns None when nothing matches,
    which callers treat as "unknown repo", not "allowed".
    """
    if not candidate:
        return None
    try:
        from src.mcp_server import tools as legacy
        indexed = [r.slug for r in legacy.list_repos()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("indexed_slug_lookup_failed err=%s", exc)
        return candidate

    if candidate in indexed:
        return candidate

    try:
        from src.sync.git_providers import parse_repo_url
        canonical = parse_repo_url(candidate).slug
        if canonical in indexed:
            return canonical
    except Exception:  # noqa: BLE001
        canonical = None

    def norm(v: str) -> str:
        return "".join(ch for ch in v.lower() if ch.isalnum())

    want = norm(candidate)
    for slug in indexed:
        n = norm(slug)
        if n == want or n.endswith(want) or want.endswith(n):
            return slug
    return canonical or candidate


def _list_accessible_repos_impl() -> dict[str, Any]:
    """Every indexed repo the caller may research, with its visibility."""
    from src.mcp_server import tools as legacy
    from src.mcp_server.identity import caller_access

    try:
        repos = legacy.list_repos()
        slugs = [r.slug for r in repos]
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_accessible_repos_failed err=%s", exc)
        slugs = []
    caller, access = caller_access(slugs)
    out: list[dict[str, Any]] = []
    for slug in slugs:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            continue
        out.append(
            dec.to_dict() if dec is not None
            else {"repo_slug": slug, "visibility": "code"}
        )
    return {
        "repos": out,
        "count": len(out),
        "authenticated": caller.authenticated,
    }


def _infer_route_path(handler_name: str) -> str:
    parts = handler_name.split("_", 1)
    if len(parts) == 2 and parts[0] in {"get", "post", "put", "delete", "patch"}:
        return "/" + parts[1].replace("_", "/")
    return "/" + handler_name.replace("_", "/")


def _get_review_impl(pr_ref: str) -> dict[str, Any]:
    """Return the most-recent review run for pr_ref (URL or shorthand)."""
    import sqlite3

    from src.api.review_runs import get_review_run_store
    store = get_review_run_store()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM review_runs WHERE pr_ref LIKE ? "
            "ORDER BY started_at DESC LIMIT 1",
            (f"%{pr_ref}%",),
        ).fetchone()
    if row is None:
        return {"error": f"no review found for {pr_ref!r}"}

    # Stage 22: review findings embed code snippets + file:line, so gate by the
    # repo the run actually belongs to (resolved from the STORED row, not the
    # possibly-partial input ref) and drop findings on deny-globbed paths.
    keys = row.keys()
    from src.api.deps import slug_from_pr_ref
    raw_slug = (row["pr_repo"] if "pr_repo" in keys and row["pr_repo"]
                else slug_from_pr_ref(row["pr_ref"]))
    # `owner/repo` (what a PR ref carries) is NOT the indexed slug form
    # (`gitlab_owner-name`), so gating on it directly silently matched nothing
    # and let every run through. Normalise before asking about access.
    repo_slug = _to_indexed_slug(raw_slug)
    dec = None
    if repo_slug:
        from src.mcp_server.identity import caller_access
        _caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        if dec is not None and not dec.researchable:
            return {"pr_ref": row["pr_ref"], "blocked_repos": [repo_slug],
                    "access_notice": _boundary_note([repo_slug])}

    import json
    findings_json = row["findings_json"] if "findings_json" in keys else "[]"
    try:
        findings = json.loads(findings_json or "[]")
    except (json.JSONDecodeError, TypeError):
        findings = []
    # Drop findings whose file is concealed by a deny-glob / allow-list miss.
    hidden = 0
    if dec is not None:
        visible: list = []
        for fnd in findings:
            fp = (fnd.get("file") or fnd.get("file_path") or "") if isinstance(fnd, dict) else ""
            if fp and not dec.path_visible(fp):
                hidden += 1
                continue
            visible.append(fnd)
        findings = visible
    out = {
        "id": row["id"],
        "pr_ref": row["pr_ref"],
        "status": row["status"],
        "verdict": row["verdict"],
        "tokens_input": row["tokens_input"],
        "tokens_output": row["tokens_output"],
        "cost_usd": row["cost_usd"],
        "started_at": row["started_at"],
        "findings": findings[:20],
        "findings_total": len(findings),
    }
    if hidden:
        out["hidden_finding_count"] = hidden
    return out


def _migrate_consumers_impl(
    *,
    project_id: str,
    symbol: str,
    old_text: str,
    new_text: str,
    user_id: str,
    commit_message: str | None,
) -> dict[str, Any]:
    """Fan-out: for each repo in the project that calls `symbol`, apply
    the text replacement via the existing apply-fix pipeline. Returns
    per-repo status.

    Best-effort — provider must be github for now. Non-github repos are
    listed as `skipped` with a reason.
    """
    slugs = _project_repo_slugs(project_id)
    if not slugs:
        return {"error": f"project {project_id!r} has no repos", "results": []}

    # Stage 22: never enumerate/modify repos the caller may not research.
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access(slugs)

    results: list[dict[str, Any]] = []
    for slug in slugs:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            results.append({"repo_slug": slug, "status": "skipped",
                            "reason": "no research access to this repo"})
            continue
        try:
            nodes = _legacy_callers(symbol, slug)
        except Exception as exc:  # noqa: BLE001
            results.append({"repo_slug": slug, "status": "skipped",
                            "reason": f"caller lookup failed: {exc}"})
            continue
        if not nodes:
            continue
        # For every caller node — attempt one apply-fix per site.
        for node in nodes[:20]:  # cap per repo — bulk migrations get too broad otherwise
            path = node.get("file", "")
            line = node.get("start_line", 0)
            if dec is not None and path and not dec.path_visible(path):
                continue
            if not path or line < 1:
                results.append({"repo_slug": slug, "status": "skipped",
                                "reason": "no coordinates on node"})
                continue
            outcome = _apply_replacement_via_apply_fix(
                repo_slug=slug, file_path=path, line=line,
                old_text=old_text, new_text=new_text,
                symbol=symbol, user_id=user_id,
                commit_message=commit_message,
            )
            results.append({"repo_slug": slug, "file": path, "line": line, **outcome})
    return {
        "project_id": project_id,
        "symbol": symbol,
        "results": results,
        "attempted": len(results),
        "succeeded": sum(1 for r in results if r.get("status") == "ok"),
    }


def _apply_replacement_via_apply_fix(
    *,
    repo_slug: str, file_path: str, line: int,
    old_text: str, new_text: str, symbol: str,
    user_id: str, commit_message: str | None,
) -> dict[str, Any]:
    """Branch off default → replace text at (file, line) → commit → PR.

    Provider inference is best-effort. We probe each provider that has a
    saved credential; the first one whose repo lookup succeeds wins.
    """
    from src.api.routers.apply_fix import (
        apply_replacement_on_default_branch,
        apply_replacement_on_default_branch_bitbucket,
        apply_replacement_on_default_branch_gitlab,
    )
    branch = f"celmis-migrate/{symbol.replace('/', '-')[:40]}-{line}"
    msg = commit_message or f"Migrate {symbol}: {old_text[:40]} → {new_text[:40]}"
    if "/" not in repo_slug:
        return {"status": "skipped", "reason": f"unrecognized repo slug {repo_slug!r}"}

    provider = _infer_provider_for_repo(repo_slug, user_id=user_id)
    common = dict(
        repo_slug=repo_slug, file_path=file_path, line=line,
        old_text=old_text, new_text=new_text,
        user_id=user_id, branch_name=branch, commit_message=msg,
    )
    if provider == "github":
        return apply_replacement_on_default_branch(**common)
    if provider == "gitlab":
        return apply_replacement_on_default_branch_gitlab(**common)
    if provider == "bitbucket":
        return apply_replacement_on_default_branch_bitbucket(**common)
    return {"status": "skipped",
            "reason": f"no provider credentials found for {repo_slug!r}"}


def _infer_provider_for_repo(repo_slug: str, *, user_id: str) -> str | None:
    """Which provider hosts this repo? Prefer explicit stored bindings;
    fall back to whichever provider credentials the caller has."""
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError
    store = get_credential_store()
    for prov in ("github", "gitlab", "bitbucket"):
        try:
            if store.load(provider=prov, user_id=user_id, account_label="default"):
                return prov
        except CredentialStoreError:
            continue
    return None


def _bootstrap_client_impl(
    *,
    project_id: str,
    target_repo_slug: str,
    target_endpoint: str | None,
    language: str,
) -> dict[str, Any]:
    """Compose: API surface + usage examples + LLM-generated stub.

    Returns a structured payload so the caller LLM can reason about
    each section independently. The client stub is best-effort — it's
    a *starting point*, not a compilable artifact.
    """
    api = _get_api_surface_impl(target_repo_slug, path_glob=None)
    endpoints = api.get("endpoints", [])
    if target_endpoint:
        endpoints = [
            e for e in endpoints
            if target_endpoint in e.get("path", "") or target_endpoint == e.get("handler")
        ] or endpoints

    # Find usage examples: search for endpoint handler names across sibling
    # repos in the project. Stage 22: gate each sibling by research access —
    # never leak caller symbol/file/line from a repo the caller can't research.
    slugs = _project_repo_slugs(project_id)
    sibling_repos = [s for s in slugs if s != target_repo_slug]
    from src.mcp_server.identity import caller_access
    _caller, sib_access = caller_access(sibling_repos)
    blocked_siblings = [s for s in sibling_repos if sib_access.get(s) is not None
                        and not sib_access[s].researchable]
    usage_examples: list[dict[str, Any]] = []
    for e in endpoints[:5]:
        handler = e.get("handler") or ""
        if not handler:
            continue
        for slug in sibling_repos[:8]:
            dec = sib_access.get(slug)
            if dec is not None and not dec.researchable:
                continue
            try:
                callers = _legacy_callers(handler, slug)
            except Exception:  # noqa: BLE001
                continue
            for c in callers[:3]:
                fpath = c.get("file", "") or ""
                if dec is not None and fpath and not dec.path_visible(fpath):
                    continue
                usage_examples.append({
                    "target_endpoint": e,
                    "consumer_repo": slug,
                    "caller": c.get("name"),
                    "file": fpath,
                    "line": c.get("start_line", 0),
                })

    # Ownership hint — who to ask if things go wrong.
    from src.ownership.builder import load_snapshot
    snap = load_snapshot(target_repo_slug) or {}
    top_owners = (snap.get("stats") or {}).get("top_owners", [])[:3]

    stub = _stub_for(language, endpoints, target_repo_slug)

    out: dict[str, Any] = {
        "target_repo_slug": target_repo_slug,
        "endpoints": endpoints[:20],
        "usage_examples": usage_examples[:20],
        "top_owners": top_owners,
        "suggested_stub": stub,
        "language": language,
        "note": (
            "This stub is a starting point derived from the discovered "
            "API surface. Validate against real request/response shapes "
            "before shipping."
        ),
    }
    # Propagate target-repo boundary + any sibling repos we couldn't research.
    boundary = sorted(set(api.get("blocked_repos", [])) | set(blocked_siblings))
    if boundary:
        out["blocked_repos"] = boundary
        out["access_notice"] = _boundary_note(boundary)
    return out


def _stub_for(language: str, endpoints: list[dict[str, Any]], slug: str) -> str:
    lang = language.lower()
    lines: list[str] = []
    if lang in {"typescript", "ts"}:
        lines.append(f"// Generated client stub for {slug}")
        lines.append("export class Client {")
        lines.append("  constructor(private baseUrl: string, private token?: string) {}")
        for e in endpoints[:10]:
            method = (e.get("method") or "GET").lower()
            path = e.get("path") or "/"
            fn = _camel(e.get("handler") or "call")
            lines.append(f"  async {fn}(): Promise<unknown> {{")
            lines.append(f"    const r = await fetch(`${{this.baseUrl}}{path}`, {{ method: '{method.upper()}', headers: this.token ? {{ Authorization: `Bearer ${{this.token}}` }} : undefined }});")
            lines.append("    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);")
            lines.append("    return r.json();")
            lines.append("  }")
        lines.append("}")
    elif lang == "python":
        lines.append(f"# Generated client stub for {slug}")
        lines.append("import httpx")
        lines.append("class Client:")
        lines.append("    def __init__(self, base_url: str, token: str | None = None):")
        lines.append("        self.base_url = base_url; self.token = token")
        for e in endpoints[:10]:
            method = (e.get("method") or "GET").lower()
            path = e.get("path") or "/"
            fn = _snake(e.get("handler") or "call")
            lines.append(f"    def {fn}(self):")
            lines.append("        h = {'Authorization': f'Bearer {self.token}'} if self.token else {}")
            lines.append(f"        r = httpx.{method}(self.base_url + '{path}', headers=h, timeout=30)")
            lines.append("        r.raise_for_status()")
            lines.append("        return r.json()")
    else:
        lines.append(f"# language={language!r} not supported yet; endpoints listed above.")
    return "\n".join(lines)


def _camel(s: str) -> str:
    parts = s.replace("-", "_").split("_")
    return parts[0] + "".join(p.title() for p in parts[1:]) if parts else s


def _snake(s: str) -> str:
    import re as _re
    return _re.sub(r"([A-Z])", r"_\1", s).lower().lstrip("_")


def _get_review_policy_impl(repo_slug: str) -> dict[str, Any]:
    # Stage 22: review policy (rules, prompts) is repo-specific intel — gate it.
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access([repo_slug])
    dec = access.get(repo_slug)
    if dec is not None and not dec.researchable:
        return {"repo_slug": repo_slug, "exists": False,
                "blocked_repos": [repo_slug],
                "access_notice": _boundary_note([repo_slug])}
    from sqlalchemy.orm import Session

    from src.db.models import RepoReviewPolicy
    with Session(_sync_engine()) as s:
        row = s.get(RepoReviewPolicy, repo_slug)
        if row is None:
            return {
                "repo_slug": repo_slug,
                "exists": False,
                "note": "no explicit policy — defaults apply",
            }
        return {
            "repo_slug": row.repo_slug,
            "exists": True,
            "enabled": row.enabled,
            "target_branches": list(row.target_branches or []),
            "prompt_template": row.prompt_template or "",
            "folder_rules": list(row.folder_rules or []),
            "architect_model": row.architect_model,
            "security_model": row.security_model,
            "quality_model": row.quality_model,
            "tests_model": row.tests_model,
            "verifier_model": row.verifier_model,
            "agent_prompt_overrides": dict(row.agent_prompt_overrides or {}),
        }


# ─── FastAPI mount helper ────────────────────────────────────────────

def mount_mcp(app: FastAPI, *, path: str = "/mcp") -> bool:
    """Attach the FastMCP Streamable-HTTP ASGI app under ``path``.

    FastMCP's session manager requires an async task group that lives for
    the process lifetime. We attach it to the FastAPI lifespan so it
    starts on app startup and shuts down on teardown — otherwise the
    first request errors with "Task group is not initialized".

    Returns True on success, False if the ``mcp`` package isn't available
    (non-fatal — the rest of the API still works).
    """
    try:
        mcp = _build_mcp()
    except ImportError as exc:
        logger.warning("mcp_mount_skipped_missing_pkg err=%s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("mcp_build_failed err=%s", exc, exc_info=True)
        return False

    try:
        sub_app = mcp.streamable_http_app()
        # Wrap FastAPI's existing lifespan (if any) so both MCP's session
        # manager AND the app's own startup logic run. Router.lifespan_context
        # returns an async CM; if none set it's a no-op.
        from contextlib import asynccontextmanager

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def combined_lifespan(app_):
            async with mcp.session_manager.run(), original_lifespan(app_):
                yield

        app.router.lifespan_context = combined_lifespan
        # Older/newer FastMCP may ignore the constructor kwarg — force it.
        # Read-only on some FastMCP builds; the mount below is what matters.
        with contextlib.suppress(Exception):
            mcp.settings.streamable_http_path = "/"
        app.mount(path, sub_app)
        # Assert the endpoint is where we claim: a silent 404 here costs the
        # agent every mcp__celmis__* tool with no error anywhere.
        try:
            inner = [getattr(r, "path", "") for r in sub_app.routes]
            if "/" not in inner and "" not in inner:
                logger.error(
                    "mcp_mount_path_mismatch mounted=%s inner_routes=%s — "
                    "clients calling %s will 404", path, inner, path,
                )
            else:
                logger.info("mcp_http_mounted path=%s inner=%s", path, inner)
        except Exception:  # noqa: BLE001
            logger.info("mcp_http_mounted path=%s", path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("mcp_mount_failed err=%s", exc, exc_info=True)
        return False


__all__ = ["mount_mcp"]
