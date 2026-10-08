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


def _security_kwargs() -> dict[str, Any]:
    """`transport_security=` for a FastMCP constructor, or nothing."""
    sec = _transport_security()
    return {"transport_security": sec} if sec else {}


def _explain_invalid_host(host: str) -> str:
    """What the SDK's refusal should have said.

    `Invalid Host header` names neither the host it rejected nor the setting
    that would admit it, so it reads like a client bug. It is not: the guard
    is DNS-rebinding protection and it is correct to refuse a host nobody
    declared. Only the operator can say which host is theirs, so the message
    has to ask them — by name, with the line already written out.
    """
    return (
        "Invalid Host header: this server was reached at "
        f"'{host}', which is not in its allowed list.\n"
        "\n"
        "That list is localhost plus whatever you have declared, because a "
        "host nobody declared is what DNS-rebinding protection exists to "
        "refuse. Declare yours in .env and restart:\n"
        "\n"
        f"    MCP_ALLOWED_HOSTS={host}\n"
        "\n"
        "PUBLIC_BASE_URL is read the same way if you already set it to the "
        "address people reach this instance at."
    )


class _ExplainInvalidHost:
    """Rewrite the transport guard's 421 body, and nothing else.

    A wrapper rather than a change to the guard: the refusal is right and
    stays exactly as strict. Only the sentence changes, and only for 421 —
    every other status, including the 401 that hides this one until a token
    is correct, passes through byte for byte.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        replaced = False

        async def _send(message):
            nonlocal replaced
            if message["type"] == "http.response.start" and message["status"] == 421:
                replaced = True
                headers = {k.lower(): v for k, v in scope.get("headers") or []}
                host = (headers.get(b"x-forwarded-host")
                        or headers.get(b"host") or b"?").decode("latin-1")
                body = _explain_invalid_host(host).encode("utf-8")
                await send({
                    "type": "http.response.start", "status": 421,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
            if replaced and message["type"] == "http.response.body":
                return
            await send(message)

        await self.app(scope, receive, _send)


def _scope_ip(scope) -> str:  # noqa: ANN001
    """The caller's address for the token's last-used stamp: evidence, never a
    decision (the leftmost X-Forwarded-For entry is the one a client can forge)."""
    try:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        fwd = headers.get("x-forwarded-for", "").split(",")[0].strip()
        if fwd:
            return fwd[:64]
        client = scope.get("client")
        return str(client[0])[:64] if client else ""
    except Exception:  # noqa: BLE001
        return ""


class _ExplainRefusal:
    """Turn the SDK's bare 401 into a 403 that says why, when we know why.

    The token verifier refuses a token whose workspace its holder has left
    (see :func:`src.mcp_server.auth._workspace_problem`). All the SDK can do
    with that is answer "Authentication required", which sends people to look
    for a typo in a token that is perfectly valid. The verifier leaves the
    real reason in a per-request slot; this wrapper owns the slot and, only
    when a reason is in it, replaces the 401 with a 403 carrying the sentence.
    Every other response — including a 401 for a forged or expired token —
    passes through untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        import json

        from src.mcp_server.auth import _REFUSAL

        holder: dict = {"ip": _scope_ip(scope)}
        reset = _REFUSAL.set(holder)
        replaced = False

        async def _send(message):
            nonlocal replaced
            if (message["type"] == "http.response.start"
                    and message["status"] == 401 and holder.get("reason")):
                replaced = True
                reason = str(holder["reason"])
                body = json.dumps({"error": "access_denied",
                                   "error_description": reason}).encode("utf-8")
                header = 'Bearer error="access_denied", error_description="{}"'.format(
                    reason.replace('"', "'"))
                await send({
                    "type": "http.response.start", "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                        (b"www-authenticate", header.encode("latin-1", "replace")),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
            if replaced and message["type"] == "http.response.body":
                return
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            _REFUSAL.reset(reset)


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
        verifier = JwtTokenVerifier(accept_project_tokens=True)
        auth_settings = AuthSettings(
            issuer_url=f"https://{verifier.config.issuer}",
            resource_server_url=f"https://{verifier.config.audience}",
            required_scopes=[],
        )
        mcp = FastMCP(
            "celmis",
            instructions=(
                "Celmis code intelligence: projects and their repos, "
                "cross-repo symbol search (search_symbols), callers "
                "(find_consumers), HTTP routes (get_api_surface) and review "
                "findings. Use it for definitions, callers and questions that "
                "span repositories; use local Grep/Read for files you are "
                "editing and for literals inside the current repo. Results "
                "describe the last indexed revision, which may lag your "
                "checkout. For day-to-day coding prefer the compact /mcp/dev "
                "profile if your token has the read:code scope."
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
    _install_call_scope_gate(mcp)
    # Defence in depth behind the verifier: every tool refuses a refused
    # caller itself (see src/mcp_server/guard.py).
    from src.mcp_server.guard import guard_every_tool
    guard_every_tool(mcp)
    # One wrapper around every call: record it, redact it, audit it.
    from src.mcp_server.call_envelope import install_call_envelope
    install_call_envelope(mcp, profile="full")
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
    # Reviews, issues, indexing and questions about the code
    # (src/automation/actions_reviews.py). The three writes spend model or
    # clone time, so they sit behind the same `write:repos` as the rest.
    "review_pr": "write:repos",
    "list_reviews": "read:reviews",
    "get_review_run": "read:reviews",
    "index_repo": "write:repos",
    "list_issues": "read:reviews",
    "update_issue": "write:repos",
    "ask_code": "read:graph",
    "search_code": "read:graph",
    # Project-scoped tokens (`cmcp_…`) see exactly these two and nothing else.
    "search_project": "read:project_search",
    "ask_project": "read:project_search",
    # lane:howto: cross-repo patterns without secret values
    "howto": "read:graph",
}

# Operations + review-configuration tools: scopes are defined next to the tools.
from src.mcp_server.ops_tools import OPS_TOOL_SCOPES  # noqa: E402
from src.mcp_server.project_tokens import SCOPE as PROJECT_SCOPE  # noqa: E402
from src.mcp_server.project_tokens import TOOLS as PROJECT_TOOLS  # noqa: E402

_TOOL_SCOPES.update(OPS_TOOL_SCOPES)


def _install_call_scope_gate(mcp) -> None:  # noqa: ANN001
    """Refuse a CALL to a tool the token's scopes do not cover.

    ``tools/list`` hides such tools, but a hidden tool can be called by name:
    a dev-profile token (``read:code`` only) must not reach the full profile's
    read tools by naming them on this endpoint. Same rule as the listing: a
    token with no scopes (legacy) passes, ``admin`` passes, a tool absent from
    the table is not gated here."""
    import mcp.types as types

    from src.mcp_server.call_envelope import _error_result
    from src.mcp_server.scopes import ADMIN_SCOPE, ScopeError

    inner = mcp._mcp_server
    key = types.CallToolRequest
    original = inner.request_handlers.get(key)
    if original is None:
        raise RuntimeError("the MCP SDK registered no tool-call handler to gate")

    async def gated(req):  # noqa: ANN001, ANN202
        name = getattr(req.params, "name", "") or ""
        required = _TOOL_SCOPES.get(name)
        try:
            from mcp.server.auth.middleware.auth_context import get_access_token

            token = get_access_token()
            scopes = list(token.scopes or []) if token else []
        except Exception:  # noqa: BLE001 — no auth context: nothing to gate
            scopes = []
        if PROJECT_SCOPE in scopes and name not in PROJECT_TOOLS:
            # A project token reaches its two tools only — a tool missing from
            # the scope table is refused too, not waved through.
            from src.mcp_server import callctx

            callctx.set_status("denied")
            return _error_result("This token can only search its own project.")
        if required and scopes and ADMIN_SCOPE not in scopes and required not in scopes:
            from src.mcp_server import callctx

            callctx.set_status("denied")
            return _error_result(str(ScopeError((required,), scopes)))
        return await original(req)

    inner.request_handlers[key] = gated


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
            if PROJECT_SCOPE in scopes:
                tools_result.tools = [t for t in tools_result.tools if t.name in PROJECT_TOOLS]
                return result
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
    from src.mcp_server.ops_tools import enforcing, register_ops_tools

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
            "Search a function/class by name across the repos of a "
            "project (or all your repos when project_id is omitted). "
            "mode: auto (exact, then substring), exact, prefix, fuzzy. "
            "Returns repo, file, start/end line, kind. Use before writing "
            "new code so you do not reimplement an existing symbol."
        ),
    )
    def _search_symbols(
        query: str,
        project_id: str | None = None,
        kind: str | None = None,
        limit: int = 20,
        mode: str = "auto",
    ) -> dict[str, Any]:
        return _without_boundary_fields(
            _search_symbols_impl(project_id, query, kind, limit, mode))

    @mcp.tool(
        name="find_consumers",
        description=(
            "Find which repos in the project call a symbol. The safety "
            "net when changing a shared endpoint or schema: the list is "
            "who breaks."
        ),
    )
    def _find_consumers(project_id: str, symbol: str) -> dict[str, Any]:
        return _find_consumers_impl(project_id, symbol)

    # ─── API surface (endpoints/handlers) ─────────────────────────────

    @mcp.tool(
        name="get_api_surface",
        description=(
            "HTTP routes of a repo found from framework syntax (FastAPI, "
            "Flask, Express, Laravel, Symfony, Go): method, path, handler, "
            "file:line. Optional path_glob. Heuristic; supported=false "
            "means it could not look."
        ),
    )
    def _get_api_surface(
        repo_slug: str,
        path_glob: str | None = None,
    ) -> dict[str, Any]:
        return _without_boundary_fields(_get_api_surface_impl(repo_slug, path_glob))

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
            "findings (severity, agent, file:line, suggestion), and — for the "
            "workspace owner or admin only — cost. "
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
            return _not_accessible()
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
            return _not_accessible()
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
        from src.mcp_server import tenancy
        from src.mcp_server.identity import caller_access, resolve_caller
        with Session(_sync_engine()) as s:
            stmt = select(DeprecatedSymbol)
            if repo_slug:
                stmt = stmt.where(DeprecatedSymbol.repo_slug == repo_slug)
            if tenancy.enforced():
                # Rows carry their workspace (the REST twin filters on it);
                # this listing did not, so every tenant's deprecations — and
                # the consumer files recorded on them — were one call away.
                caller = resolve_caller()
                ws = caller.workspace_id if tenancy.caller_may_bind(caller) else ""
                stmt = stmt.where(DeprecatedSymbol.workspace_id == ws)
            rows = s.execute(stmt).scalars().all()
            # Research access in both modes: a repository the caller may not
            # research contributes no deprecations (single_tenant included).
            if rows:
                _c, access = caller_access(sorted({r.repo_slug for r in rows}))
                rows = [r for r in rows
                        if access.get(r.repo_slug) is not None
                        and access[r.repo_slug].researchable]
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
            'Given a project and a target repo, return its API surface, usage '
            'examples in sibling repos and a suggested client stub. '
            '`target_endpoint` narrows to one endpoint. Use it to integrate '
            "with another team's service without asking them."
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
            'Ordered checklist for a cross-team integration against '
            '`target_repo_slug`: read the architecture summary, list owners, '
            'pick endpoints, draft a client. Use at the start of an integration'
            ' task instead of ten separate calls.'
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
            'Bulk-apply a text replacement across all consumers of `symbol` in '
            'the project: opens branch `celmis-migrate/<symbol>-<n>` per '
            "consumer and commits old_text to new_text at the consumer's line. "
            'Repos whose provider cannot apply fixes (only github can) are '
            'skipped.'
        ),
    )
    @enforcing("write:reviews")
    def _migrate_consumers(
        project_id: str,
        symbol: str,
        old_text: str,
        new_text: str,
        commit_message: str | None = None,
    ) -> dict[str, Any]:
        # No `user_id` argument: the branch, the commit and the PR are made with
        # the git credentials of the CALLER, whoever the token says that is.
        return _migrate_consumers_impl(
            project_id=project_id, symbol=symbol,
            old_text=old_text, new_text=new_text,
            commit_message=commit_message,
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
            return _not_accessible()
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
        from src.mcp_server.identity import actor_for

        return actor_for(label, writing=writing)


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
            "Register a repository in the caller's workspace for indexing, "
            "audits and review. Accepts a URL, 'owner/name' or "
            "'provider:owner/name'; empty branch = provider default. "
            'Idempotent. index (default true) queues the graph build; the '
            "reply's index_status says what happened."
        ),
    )
    @enforcing("write:repos")
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
            'Queue a dependency and vulnerability audit. repo_slugs empty = '
            "every workspace repo; owner filters by 'owner/' prefix; branch "
            'overrides for this run. report_engine none|api|claude_code picks '
            'who writes the prose. Returns run_id.'
        ),
    )
    @enforcing("write:repos")
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
            "Queue docs (module PRDs, feature docs, guides) for repo_slugs, "
            "an owner prefix or the whole workspace. missing_only skips "
            "repos that already have docs; without it a rerun "
            "regenerates everything. Replies queued and skipped repos; a "
            "repo not indexed is refused."
        ),
    )
    @enforcing("write:repos")
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
            'Turn automatic PR review on or off for a set of repos (repo_slugs,'
            ' owner prefix, or none for the whole workspace) and optionally pin'
            ' the branch every surface then reads. Refuses above a cap, since '
            'it changes every future pull request.'
        ),
    )
    @enforcing("write:repos")
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
            'Operational state of every workspace repo: indexed, documented, '
            'auto review on/off, branch. Run before any verb that takes '
            'repo_slugs. Differs from list_accessible_repos, which says what '
            'the CALLER may research.'
        ),
    )
    async def _list_workspace_repos() -> dict[str, Any]:
        from src.automation.actions import ActionError, list_repos

        try:
            return {"ok": True, **(await list_repos(_actor("mcp")))}
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

    # ─── Reviews, issues, indexing and questions about the code ───────
    #
    # The daily work an agent was missing: start a review of a pull request,
    # see how the last ones went, read what a run found, close an issue,
    # re-index, ask the code a question, find who owns a file. Bodies live in
    # src.automation.actions_reviews — the same functions the in-app agent
    # runs — so every gate (the team's `review` / `read` grant, the member
    # role, the research-access resolver) is the page's.

    @mcp.tool(
        name="review_pr",
        description=(
            'Queue an AI review of ONE pull request (repo_slug + number), or '
            'all_open=true for up to 25 open ones (numbers, branch narrow it). '
            'post_comments (default true) posts findings. Needs `review` on the'
            ' repo. Returns run ids for get_review_run.'
        ),
    )
    @enforcing("write:repos")
    async def _review_pr(
        repo_slug: str,
        number: int | None = None,
        all_open: bool = False,
        numbers: list[int] | None = None,
        branch: str | None = None,
        post_comments: bool = True,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import review_pr

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **await review_pr(
                actor, None, repo_slug=repo_slug, number=number,
                all_open=all_open, numbers=numbers, branch=branch,
                post_comments=post_comments,
            )}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="list_reviews",
        description=(
            "The newest review runs of one repository (repo_slug) or of the "
            "whole workspace: pull request, status, verdict, finding counts "
            "by severity, when. status filters (complete|failed|running|"
            "queued|partial|skipped); limit defaults to 10, at most 25. Each "
            "row carries the run_id for get_review_run."
        ),
    )
    async def _list_reviews(
        repo_slug: str | None = None, status: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import list_reviews

        try:
            actor = _actor("mcp")
            return {"ok": True, **await list_reviews(
                actor, None, repo_slug=repo_slug, status=status, limit=limit)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="get_review_run",
        description=(
            'One review run: summary, verdict, agents and findings (severity, '
            'file, line, title), bounded by limit (default 20, max 40; '
            '`truncated` says it was cut). Give run_id, or repo_slug + number '
            'for the latest run of that pull request.'
        ),
    )
    async def _get_review_run(
        run_id: str | None = None, repo_slug: str | None = None,
        number: int | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import get_review_run

        try:
            actor = _actor("mcp")
            return {"ok": True, **await get_review_run(
                actor, None, run_id=run_id, repo_slug=repo_slug, number=number,
                limit=limit)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="index_repo",
        description=(
            'Queue a (re-)index of repos, the code graph that search, Q&A and '
            'reviews read. repo_slugs names them; omit for the workspace (owner'
            ' narrows; existing graphs are skipped unless force=true). At most '
            '50. Needs `review` on each repo.'
        ),
    )
    @enforcing("write:repos")
    async def _index_repo(
        repo_slugs: list[str] | None = None, owner: str | None = None,
        force: bool | None = None,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import index_repo

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **await index_repo(
                actor, None, repo_slugs=repo_slugs, owner=owner, force=force)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="list_issues",
        description=(
            'Tracked review issues, worst severity first, with counts per '
            'status. status open|fixed|dismissed|resolved, comma-separated '
            '(default open); optional severity, repo_slug, pr number, text q; '
            "limit default 15, max 25. A row's id is what update_issue takes."
        ),
    )
    async def _list_issues(
        status: str | None = "open", severity: str | None = None,
        repo_slug: str | None = None, pr: int | None = None,
        q: str | None = None, limit: int = 15,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import list_issues

        try:
            actor = _actor("mcp")
            return {"ok": True, **await _in_session(
                lambda s: list_issues(
                    actor, s, status=status, severity=severity,
                    repo_slug=repo_slug, pr=pr, q=q, limit=limit))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="update_issue",
        description=(
            "Set the status of one or several review issues (issue_ids, at "
            "most 25): open | fixed | dismissed | resolved. Needs the member "
            "role or above on the workspace. Reopening clears the close "
            "marks; any other status records a manual resolution."
        ),
    )
    @enforcing("write:repos")
    async def _update_issue(issue_ids: list[str], status: str) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import update_issue

        try:
            actor = _actor("mcp", writing=True)
            return {"ok": True, **await _in_session(
                lambda s: update_issue(actor, s, ids=issue_ids, status=status))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="ask_code",
        description=(
            'Ask a question about the code of one or several repos (repo_slugs,'
            ' max 8; omit for all you can read); the answer is built from vault'
            ' notes, the graph and files read (listed in `files`). Costs one '
            'model call against the workspace budget. For symbols or owners use'
            ' search_code, which is free.'
        ),
    )
    async def _ask_code(
        question: str, repo_slugs: list[str] | None = None,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import ask_code

        try:
            actor = _actor("mcp")
            return {"ok": True, **await ask_code(
                actor, None, question=question, repo_slugs=repo_slugs)}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="search_code",
        description=(
            'Find things in code. kind=search (default): symbols and doc notes '
            'matching `query`, optionally in repo_slug. kind=usages: callers of'
            ' symbol `query`. kind=owner: owners of `path`. kind=architecture: '
            'cached summary. Bounded by limit (default 15, max 50).'
        ),
    )
    async def _search_code(
        kind: str = "search", query: str | None = None,
        repo_slug: str | None = None, path: str | None = None,
        limit: int = 15,
    ) -> dict[str, Any]:
        from src.automation.actions import ActionError
        from src.automation.actions_reviews import search_code

        try:
            actor = _actor("mcp")
            return {"ok": True, **await _in_session(
                lambda s: search_code(
                    actor, s, kind=kind, query=query, repo_slug=repo_slug,
                    path=path, limit=limit))}
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}

    # Operations (spend, alerts, jobs, audit extras, members) and review
    # configuration — one registration shared with the stdio server. Scopes
    # hide the tools in tools/list (`_TOOL_SCOPES`) AND refuse the call itself
    # (`enforcing`): a hidden tool can otherwise still be called by name.
    register_ops_tools(mcp, _actor, _in_session, enforcing)

    # Search/ask inside ONE project, for project-scoped tokens (cmcp_…).
    from src.mcp_server.project_tools import register_project_tools

    register_project_tools(mcp)

    # "Do it like service X": code slices + the NAMES of env vars and where
    # their values come from, never a value (src/mcp_server/howto/).
    from src.mcp_server.howto import register_howto

    register_howto(mcp, scoped=enforcing)


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


def _readable_slugs(slugs) -> set[str]:  # noqa: ANN001
    """Which of ``slugs`` the current caller may research (one resolution)."""
    from src.mcp_server.identity import caller_access

    wanted = sorted(set(slugs))
    if not wanted:
        return set()
    _caller, access = caller_access(wanted)
    return {s for s, d in access.items() if d is not None and d.researchable}


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
        per_project = {
            p.id: [r.repo_slug for r in s.execute(
                select(ProjectRepo).where(ProjectRepo.project_id == p.id)
            ).scalars().all()]
            for p in rows
        }
        # The count covers only the repositories the caller may read: a total
        # would say how many it is not being shown.
        readable = _readable_slugs({x for v in per_project.values() for x in v})
        out = []
        for p in rows:
            if per_project[p.id] and not any(x in readable for x in per_project[p.id]):
                # A project whose repositories the caller can read none of is
                # not shown: its name and description are not theirs to see.
                continue
            out.append({
                "id": str(p.id),
                "name": p.name,
                "description": p.description,
                "repo_count": sum(1 for x in per_project[p.id] if x in readable),
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
        readable = _readable_slugs({r.repo_slug for r in repos})
        if repos and not readable:
            return {"error": f"project {project_id!r} not found"}
        return {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "repos": [
                {"repo_slug": r.repo_slug, "role": r.role}
                for r in repos if r.repo_slug in readable
            ],
        }


def _project_repo_slugs(project_id: str) -> list[str]:
    """The repos of ``project_id`` — under multi_tenant only when the project
    belongs to the caller's workspace.

    A foreign project reads exactly like a missing one (no repos): naming its
    repos in ``blocked_repos`` / ``access_notice`` would hand another tenant's
    repository names to anyone holding the project's id, and "has no repos"
    versus "blocked" would confirm the id exists.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import Project, ProjectRepo
    from src.mcp_server import tenancy

    stmt = select(ProjectRepo).where(ProjectRepo.project_id == project_id)
    if tenancy.enforced():
        from src.mcp_server.identity import resolve_caller

        caller = resolve_caller()
        if not tenancy.caller_may_bind(caller):
            return []
        stmt = stmt.join(Project, Project.id == ProjectRepo.project_id).where(
            Project.workspace_id == caller.workspace_id)
    with Session(_sync_engine()) as s:
        rows = s.execute(stmt).scalars().all()
        return [r.repo_slug for r in rows]


# ── legacy-tool result normalizers ──────────────────────────────────
# tools.find_symbol → list[dict] (asdict of SymbolInfo + repo_slug):
#   keys name/kind/file/start_line/signature/... — NOT objects.
# tools.find_callers → dict {repo, target_id, callers: [dict], ...}:
#   caller dicts have keys id/name/kind/file/start_line.


def _legacy_callers(symbol, slug):  # noqa: ANN001
    from src.mcp_server import tools as legacy
    res = legacy.find_callers(symbol_id=symbol, repo_slug=slug)
    return (res or {}).get("callers", []) if isinstance(res, dict) else []


def _boundary_note(blocked: list[str]) -> str:
    """What a caller is told about repositories it may not read: nothing that
    names them. A name, a count or a "blocked" flag would confirm that the
    repository exists to somebody who has no business knowing, so a denied
    repo in a fan-out is omitted without a trace and a named one reads exactly
    like one that was never registered (:data:`src.access.effective.NOT_ACCESSIBLE`).
    Kept (returning the empty string) so call sites need no second code path."""
    return ""


def _not_accessible() -> dict[str, Any]:
    """The one answer for a named repository the caller may not read — the
    same bytes whether it is denied or does not exist."""
    from src.access.effective import NOT_ACCESSIBLE

    return {"error": NOT_ACCESSIBLE}


def _search_symbols_impl(
    project_id: str | None, query: str, kind: str | None, limit: int,
    mode: str = "auto",
) -> dict[str, Any]:
    """Name search over the repos of a project, or over every repo the caller
    may research when no project is given.

    `kind` is applied inside the graph query (before the LIMIT), matching is
    ranked (exact, prefix, token, substring, fuzzy), and repos are interleaved
    so one large repo cannot fill the page. Repos the caller cannot research
    are skipped without a trace: naming them would confirm they exist.
    """
    from src.mcp_server.dev_profile import common, rank
    from src.mcp_server.identity import caller_access

    limit = max(1, min(int(limit or 20), 100))
    if mode not in ("auto", "exact", "prefix", "fuzzy"):
        mode = "auto"
    if project_id:
        repo_slugs = _project_repo_slugs(project_id)
        if not repo_slugs:
            return {"error": f"project {project_id!r} has no repos", "matches": []}
    else:
        from src.mcp_server.dev_profile.access import indexed_slugs
        repo_slugs = indexed_slugs()
    _caller, access = caller_access(repo_slugs)

    def one(slug: str) -> list[dict[str, Any]]:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            return []  # silently omitted: naming it would confirm it exists
        with common.open_store(slug) as store:
            rows = store.find_symbols(query, mode=mode, kind=kind or None, limit=200)
        out = []
        for row in rows:
            fpath = str(row.get("file") or "")
            if dec is not None and fpath and not dec.path_visible(fpath):
                continue
            sc = rank.score(row, query)
            if sc <= 0:
                continue
            match: dict[str, Any] = {
                "repo_slug": slug, "name": row.get("name"), "kind": row.get("kind"),
                "file": fpath, "line": row.get("start_line"),
                "end_line": row.get("end_line"),
            }
            if row.get("signature"):
                match["signature"] = row["signature"]
            out.append((sc, match))
        out.sort(key=lambda t: -t[0])
        return [m for _s, m in out]

    per_repo: list[list[dict[str, Any]]] = []
    try:
        per_repo = [v for _k, v in sorted(common.map_repos(repo_slugs, one).items()) if v]
    except Exception as exc:  # noqa: BLE001
        logger.debug("search_symbols_failed err=%s", exc)
    matches: list[dict[str, Any]] = []
    depth = 0
    while len(matches) < limit and any(depth < len(v) for v in per_repo):
        for v in per_repo:
            if depth < len(v) and len(matches) < limit:
                matches.append(v[depth])
        depth += 1
    return {"query": query, "matches": matches, "count": len(matches)}


def _find_consumers_impl(project_id: str, symbol: str) -> dict[str, Any]:
    repo_slugs = _project_repo_slugs(project_id)
    if not repo_slugs:
        return {"error": f"project {project_id!r} has no repos"}
    from src.mcp_server.identity import caller_access
    _caller, access = caller_access(repo_slugs)

    consumers: list[dict[str, Any]] = []
    for slug in repo_slugs:
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            continue  # silently omitted: naming it would confirm it exists
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
    return {"symbol": symbol, "consumers": consumers, "count": len(consumers)}


def _get_api_surface_impl(
    repo_slug: str, path_glob: str | None,
) -> dict[str, Any]:
    """HTTP routes of `repo_slug`, read from source at the indexed revision.

    Heuristic (see :mod:`src.indexing.routes`): FastAPI/Flask decorators,
    Express, Laravel, Symfony and Go registrations. `supported` is false when
    the repository has no readable revision, so "nothing found" is never
    confused with "could not look".
    """
    import fnmatch
    from functools import partial

    from src.indexing.routes import extract_routes
    from src.mcp_server.dev_profile import git_io
    from src.mcp_server.dev_profile.freshness import read_freshness
    from src.mcp_server.identity import caller_access

    _caller, access = caller_access([repo_slug])
    dec = access.get(repo_slug)
    if dec is not None and not dec.researchable:
        return _not_accessible()

    fresh = read_freshness([repo_slug]).get(repo_slug)
    sha = fresh.sha if fresh is not None else (git_io.head_sha(repo_slug) or "")
    if not sha or not git_io.has_commit(repo_slug, sha):
        return {"repo_slug": repo_slug, "supported": False, "endpoints": [], "count": 0,
                "reason": "no readable revision of this repository"}

    def visible(path: str) -> bool:
        return dec is None or dec.path_visible(path)

    def show(path: str) -> list[str] | None:
        return git_io.show_file(repo_slug, sha, path)

    grep = partial(git_io.grep, repo_slug, sha, regex=True, max_hits=600, per_file=120)
    endpoints: list[dict[str, Any]] = []
    try:
        for r in extract_routes(grep, show, path_filter=visible):
            if path_glob and not fnmatch.fnmatch(r.path, path_glob):
                continue
            endpoints.append({
                "path": r.path, "method": r.method, "handler": r.handler,
                "file": r.file, "line": r.line, "framework": r.framework,
            })
    except Exception as exc:  # noqa: BLE001
        logger.debug("api_surface_failed repo=%s err=%s", repo_slug, exc)
    return {
        "repo_slug": repo_slug,
        "endpoints": endpoints,
        "count": len(endpoints),
        "note": "heuristic: routes found from framework syntax at the indexed revision",
    }


def _without_boundary_fields(out: dict[str, Any]) -> dict[str, Any]:
    """The public tools never say WHICH repos were withheld: a name in
    `blocked_repos` confirms the repository exists to someone who may not
    know it does."""
    return {k: v for k, v in out.items()
            if k not in ("blocked_repos", "access_notice", "hidden_symbol_count")}


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
        indexed = legacy.list_repo_slugs()
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
        slugs = legacy.list_repo_slugs()
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


def _get_review_impl(pr_ref: str) -> dict[str, Any]:
    """Return the most-recent review run for pr_ref (URL or shorthand)."""
    import sqlite3

    from src.api.review_runs import get_review_run_store
    from src.mcp_server import tenancy
    store = get_review_run_store()
    sql = "SELECT * FROM review_runs WHERE pr_ref LIKE ? "
    params: tuple = (f"%{pr_ref}%",)
    if tenancy.enforced():
        # The LIKE spans every tenant's runs; the newest match could be
        # anybody's. Search only the caller's own.
        from src.mcp_server.identity import resolve_caller
        caller = resolve_caller()
        if not tenancy.caller_may_bind(caller):
            return {"error": f"no review found for {pr_ref!r}"}
        sql += "AND workspace_id = ? "
        params = (*params, caller.workspace_id)
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            sql + "ORDER BY started_at DESC LIMIT 1", params,
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
    if not repo_slug:
        # A run whose repository cannot be named cannot be checked against
        # the access rules — and unchecked used to mean "shown". In every
        # deployment mode: default-deny has no "unnamed, so open" reading.
        return {"error": f"no review found for {pr_ref!r}"}
    if repo_slug:
        from src.mcp_server.identity import caller_access
        _caller, access = caller_access([repo_slug])
        dec = access.get(repo_slug)
        if dec is not None and not dec.researchable:
            # The same answer as "no such review": the run's repository is not
            # named, and neither is the fact that a run exists.
            return {"error": f"no review found for {pr_ref!r}"}

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
        "started_at": row["started_at"],
        "findings": findings[:20],
        "findings_total": len(findings),
    }
    if hidden:
        out["hidden_finding_count"] = hidden
    if _caller_sees_review_cost():
        out["cost_usd"] = row["cost_usd"]
    return out


def _caller_sees_review_cost() -> bool:
    """What a review cost: the token owner's role in the token's workspace —
    owner/admin, or a global admin; the same answer as the review routes."""
    from src.mcp_server.identity import resolve_caller

    caller = resolve_caller()
    if caller.refused:
        return False
    if caller.is_admin:
        return True
    from src.api.deps import workspace_role
    from src.users.roles import WORKSPACE_ADMIN_ROLES

    return workspace_role(caller.user_id, caller.workspace_id) in WORKSPACE_ADMIN_ROLES


def _migrate_consumers_impl(
    *,
    project_id: str,
    symbol: str,
    old_text: str,
    new_text: str,
    commit_message: str | None,
) -> dict[str, Any]:
    """Fan-out: for each repo in the project that calls `symbol`, apply
    the text replacement via the existing apply-fix pipeline. Returns
    per-repo status.

    Best-effort — provider must be github for now. Non-github repos are
    listed as `skipped` with a reason.

    Who acts is always the authenticated caller. Changing code takes more than
    reading it, so this needs the owner/admin role of the workspace (or a
    global admin) AND `code` access to every repository it touches; the call is
    audited by the envelope (the repositories are noted below).
    """
    from src.mcp_server.identity import caller_access, resolve_caller

    caller = resolve_caller()
    if caller.refused:
        return {"error": caller.refused, "results": []}
    if not _may_change_code(caller):
        return {"error": "migrate_consumers needs the owner or admin role of the workspace",
                "results": []}
    user_id = caller.user_id
    slugs = _project_repo_slugs(project_id)
    if not slugs:
        return {"error": f"project {project_id!r} has no repos", "results": []}

    # Stage 22: never enumerate/modify repos the caller may not research.
    _caller, access = caller_access(slugs)

    from src.mcp_server import callctx

    results: list[dict[str, Any]] = []
    for slug in slugs:
        dec = access.get(slug)
        if dec is not None and not dec.code_visible:
            continue  # not named: a repo the caller cannot read is omitted without a trace
        callctx.note_repos(slug)
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


def _may_change_code(caller) -> bool:  # noqa: ANN001
    """Global admin, or owner/admin of the workspace the token answers for."""
    if getattr(caller, "is_admin", False):
        return True
    try:
        from src.api.deps import workspace_role
        from src.users.roles import WORKSPACE_ADMIN_ROLES

        return workspace_role(caller.user_id, caller.workspace_id) in WORKSPACE_ADMIN_ROLES
    except Exception:  # noqa: BLE001 — cannot tell: no
        return False


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

    # Ownership hint — who to ask if things go wrong. Only for a target the
    # caller may research: the snapshot is looked up by slug alone, so without
    # this gate any slug (another tenant's included) yielded its top committers.
    top_owners: list[Any] = []
    _c, target_access = caller_access([target_repo_slug])
    target_dec = target_access.get(target_repo_slug)
    if "error" not in api and target_dec is not None and target_dec.researchable:
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
    # A target or sibling the caller may not research contributes nothing and
    # is not mentioned: no name, no count, no "blocked" marker.
    if "error" in api:
        return _not_accessible()
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
        return _not_accessible()
    from sqlalchemy.orm import Session

    from src.db.models import RepoReviewPolicy
    with Session(_sync_engine()) as s:
        row = s.get(RepoReviewPolicy, repo_slug)
        from src.mcp_server import tenancy
        if row is not None and tenancy.enforced():
            from src.mcp_server.identity import resolve_caller
            if row.workspace_id != resolve_caller().workspace_id:
                row = None  # another tenant's policy — the REST twin hides it too
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
            # Team guidelines — ADDED to each agent's prompt, unlike the
            # overrides above, which replace it.
            "agent_prompt_guidelines": dict(
                getattr(row, "agent_prompt_guidelines", None) or {}),
            "agent_guidelines_extend": list(
                getattr(row, "agent_guidelines_extend", None) or []),
        }


# ─── FastAPI mount helper ────────────────────────────────────────────

def mount_mcp(app: FastAPI, *, path: str = "/mcp") -> bool:
    """Attach the FastMCP Streamable-HTTP ASGI apps under ``path``.

    Two servers are mounted: the full one at ``path`` and the compact,
    read-only developer profile at ``path`` + ``/dev``
    (:mod:`src.mcp_server.dev_profile`). The dev profile is mounted FIRST:
    Starlette matches mounts by prefix in registration order, so ``/mcp``
    registered first would swallow ``/mcp/dev``. A failure to build the dev
    profile never takes the full server down with it.

    FastMCP's session manager requires an async task group that lives for
    the process lifetime. We attach each to the FastAPI lifespan so it
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

    dev = None
    try:
        from src.mcp_server.dev_profile import build_dev_mcp

        dev = build_dev_mcp()
    except Exception as exc:  # noqa: BLE001
        logger.error("mcp_dev_build_failed err=%s", exc, exc_info=True)

    try:
        sub_app = mcp.streamable_http_app()
        dev_app = dev.streamable_http_app() if dev is not None else None
        # Wrap FastAPI's existing lifespan (if any) so both MCP's session
        # managers AND the app's own startup logic run. Router.lifespan_context
        # returns an async CM; if none set it's a no-op.
        from contextlib import AsyncExitStack, asynccontextmanager

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def combined_lifespan(app_):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.session_manager.run())
                if dev is not None and dev_app is not None:
                    await stack.enter_async_context(dev.session_manager.run())
                await stack.enter_async_context(original_lifespan(app_))
                yield

        app.router.lifespan_context = combined_lifespan
        # Older/newer FastMCP may ignore the constructor kwarg — force it.
        # Read-only on some FastMCP builds; the mount below is what matters.
        with contextlib.suppress(Exception):
            mcp.settings.streamable_http_path = "/"
        if dev is not None and dev_app is not None:
            with contextlib.suppress(Exception):
                dev.settings.streamable_http_path = "/"
            app.mount(f"{path.rstrip('/')}/dev", _ExplainInvalidHost(_ExplainRefusal(dev_app)))
        app.mount(path, _ExplainInvalidHost(_ExplainRefusal(sub_app)))
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
                logger.info("mcp_http_mounted path=%s inner=%s dev=%s", path, inner,
                            dev is not None)
        except Exception:  # noqa: BLE001
            logger.info("mcp_http_mounted path=%s", path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("mcp_mount_failed err=%s", exc, exc_info=True)
        return False


__all__ = ["mount_mcp"]
