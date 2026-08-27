"""Celmis REST API — FastAPI application factory.

Composition:
    /api/auth/*         — login / signup / google / me
    /api/connections/*  — provider tokens (GitHub / GitLab / Bitbucket)
    /api/repos/*        — list / add / remove + auto-review toggle + browse + PR list
    /api/reviews/*      — manual trigger + history
    /webhook/*          — provider webhooks (mounted from src.review.webhook)
    /healthz, /readyz   — service status

Run:
    uvicorn src.api.main:app --port 8000           # via uvicorn directly
    celmis serve                                  # via CLI wrapper
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api.routers import access as access_router
from src.api.routers import agents as agents_router
from src.api.routers import alerts as alerts_router
from src.api.routers import apply_fix as apply_fix_router

# Stage 21
from src.api.routers import audit as audit_router
from src.api.routers import auth as auth_router
from src.api.routers import capabilities as capabilities_router
from src.api.routers import chats as chats_router
from src.api.routers import claude_code as claude_code_router
from src.api.routers import compliance as compliance_router
from src.api.routers import connections as connections_router
from src.api.routers import deps as deps_router
from src.api.routers import docs as docs_router
from src.api.routers import feedback as feedback_router
from src.api.routers import gdpr as gdpr_router
from src.api.routers import groups as groups_router
from src.api.routers import intel as intel_router_mod
from src.api.routers import invites as invites_router
from src.api.routers import jobs as jobs_router
from src.api.routers import llm as llm_router
from src.api.routers import models as models_router
from src.api.routers import oauth as oauth_router
from src.api.routers import oauth_metadata as oauth_metadata_router
from src.api.routers import ops_gateway as ops_gateway_router
from src.api.routers import ops_metrics as ops_metrics_router
from src.api.routers import projects as projects_router
from src.api.routers import push as push_router
from src.api.routers import qa as qa_router
from src.api.routers import repos as repos_router
from src.api.routers import review_policies as review_policies_router
from src.api.routers import reviews as reviews_router
from src.api.routers import search as search_router_mod
from src.api.routers import spend as spend_router
from src.api.routers import teams as teams_router
from src.api.routers import usage as usage_router
from src.api.routers import users as users_router
from src.api.routers import vector_store as vector_store_router
from src.api.routers import webhooks as webhooks_router
from src.api.routers import workspaces as workspaces_router

logger = logging.getLogger(__name__)

# Before anything in this process logs. Under uvicorn the handlers are already
# configured by the time this module is imported, so this catches them; the
# startup hook runs it again for anything added since. See
# `src.security.log_filter` for what a call site cannot be trusted to do.
from src.security.log_filter import install_log_redaction as _install_log_redaction  # noqa: E402

_install_log_redaction()



def _version() -> str:
    """The running version, and — where it is known — the running BUILD.

    Read rather than hardcoded: the string was written out in several files
    and they had already begun to disagree.

    Two parts, answering two different questions:

      * the release number comes from `src.__version__`, a literal readable
        without an install — which is the whole reason `pyproject.toml` points
        at it. A literal is also exactly what drifts: eight tags were cut while
        it still said 0.1.0, so an install of v0.1.7 reported itself as 0.1.0.
        The release workflow now refuses to build a tag that disagrees with it,
        which is the only place that can tell;
      * the git sha follows as a local version segment when the deploy stamped
        one. It answers the narrower question — not "which release" but "which
        build of it" — and it is what the AGPL footer links to, so a deploy
        that skips the stamp offers source at a version that does not exist.
    """
    from src import __version__ as literal

    base = literal
    try:
        from importlib.metadata import PackageNotFoundError, version

        from src import DISTRIBUTIONS

        installed = None
        try:
            for _dist in DISTRIBUTIONS:
                try:
                    installed = version(_dist)
                    break
                except PackageNotFoundError:
                    continue
            # Ignore the fixed point this function used to produce: a
            # distribution built before the loop was broken carries
            # "0.0.0+unknown" in its metadata, and reading it back would
            # reinstate the bug on any container that predates the fix.
            if installed and not installed.startswith("0.0.0+unknown"):
                base = installed
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001
        pass

    try:
        from src.ops.build import build_info

        sha = (build_info().get("git_sha_short") or "").strip()
    except Exception:  # noqa: BLE001 — a version must not take the app down
        sha = ""
    return f"{base}+{sha}" if sha else base


def _mount_private_docs(app: FastAPI) -> None:
    """Swagger and the schema, behind a session.

    Same two URLs as before, so a bookmark still works and an operator sees a
    login prompt rather than a 404 they have to interpret. Anonymous callers
    get 401 — not 404: pretending the route does not exist would be a lie the
    next version has to keep telling.
    """
    from fastapi import Depends
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.openapi.utils import get_openapi

    from src.api.deps import get_current_user
    from src.users import User

    @app.get("/openapi.json", include_in_schema=False)
    def private_openapi(_user: User = Depends(get_current_user)) -> JSONResponse:
        return JSONResponse(get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        ))

    @app.get("/docs", include_in_schema=False)
    def private_docs(_user: User = Depends(get_current_user)):  # noqa: ANN201
        return get_swagger_ui_html(openapi_url="/openapi.json",
                                   title=f"{app.title} — docs")



#: Field names whose value must never leave the process in an error body.
#: Matched on the last path element and case-insensitively, so `token`,
#: `access_token`, `client_secret` and `api_key` are all covered.
_SECRET_FIELD_MARKERS = (
    "token", "password", "secret", "api_key", "apikey",
    "credential", "private_key", "passphrase",
)


def _looks_secret(name: object) -> bool:
    n = str(name).lower()
    return any(marker in n for marker in _SECRET_FIELD_MARKERS)


def _redact(value: object, *, key: object = None) -> object:
    """A validation error's `input`, with every secret-shaped value removed."""
    if key is not None and _looks_secret(key) and isinstance(value, str):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _install_validation_redaction(app) -> None:
    """Stop 422 bodies from echoing the credential that was just submitted.

    FastAPI's default handler puts the offending value in `input`, and for a
    MISSING field that value is the whole request body. Saving a GitHub
    connection with `provider` omitted therefore returned:

        {"detail":[{"type":"missing","loc":["body","provider"],
                    "msg":"Field required",
                    "input":{"token":"ghp_…"}}]}

    — the personal access token, in the response, and from there in every
    access log, proxy log and browser devtools panel that saw it. Observed on
    production while testing the connections endpoint; the token had to be
    treated as compromised.

    The rest of the error is untouched. `loc`, `msg` and `type` are what makes
    a 422 useful, and none of them carries the value.
    """
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def _redacted_validation_error(request, exc):  # noqa: ANN001
        errors = []
        for err in exc.errors():
            e = dict(err)
            if "input" in e:
                loc = e.get("loc") or ()
                last = loc[-1] if loc else None
                e["input"] = _redact(e["input"], key=last)
            # Pydantic can attach the offending value here too.
            #
            # AND IT CAN ATTACH AN EXCEPTION OBJECT. A `model_validator` that
            # raises ValueError arrives with `ctx={"error": ValueError(...)}`,
            # which `JSONResponse` cannot serialise — so this handler, whose
            # whole job is to keep a token out of a 422, raised inside itself
            # and Starlette answered 500 with no body at all.
            #
            # Every field-level error survived (their ctx holds numbers), so
            # the failure was invisible until a MODEL-level rule fired:
            # `POST /api/agent-sessions` without a repo answered 500 instead
            # of naming the missing field. A client could not tell "I sent the
            # wrong thing" from "the server fell over".
            #
            # `str()` on the way out, after redaction: the message is the
            # sentence the validator wrote, and it is the part a caller needs.
            ctx = e.get("ctx")
            if isinstance(ctx, dict):
                e["ctx"] = {
                    k: _jsonable(_redact(v, key=k)) for k, v in ctx.items()
                }
            errors.append(e)
        return JSONResponse(status_code=422, content={"detail": errors})



def _jsonable(value):
    """Whatever `JSONResponse` can encode, or its `str()`.

    Guards the validation handler against the one thing it is guaranteed to
    meet: pydantic putting a live exception object in `ctx`.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _workspaces_with_llm_config() -> list[str]:
    """Workspace slots that hold an LLM config, for the readiness probe.

    Presence only — no secret is read and no provider is pinged. Returns [] on
    any failure, because a readiness check must never be the thing that breaks
    readiness.
    """
    try:
        from src.api.routers.llm import _WORKSPACE_LABEL, _WORKSPACE_PROVIDER_TAG
        from src.credentials import get_credential_store

        store = get_credential_store()
        return [
            slot for slot in store.slots_with(
                provider=_WORKSPACE_PROVIDER_TAG,
                account_label=_WORKSPACE_LABEL,
            )
            if slot.startswith("ws:")
        ]
    except Exception:  # noqa: BLE001
        return []


def build_app() -> FastAPI:
    # The interactive docs and the schema behind them are NOT public.
    #
    # Measured on production before this changed: GET /openapi.json returned
    # 200 to anybody, 250 KB describing all 180 routes — including the ops,
    # audit and GDPR endpoints, with their parameters and payload shapes. No
    # credential leaks that way, but it is a complete map of the installation
    # handed to an unauthenticated stranger, and this product is sold to
    # people whose own auditors ask about exactly that.
    #
    # FastAPI's built-ins cannot be gated, so they are switched off and
    # re-served below behind a session. The operator keeps the tool; the
    # internet loses the map. CELMIS_PUBLIC_API_DOCS=1 restores the old
    # behaviour for anyone who wants it back deliberately.
    _public_docs = os.environ.get("CELMIS_PUBLIC_API_DOCS", "").strip() == "1"
    app = FastAPI(
        title="Celmis API",
        version=_version(),
        description="Code intelligence + auto PR review backend.",
        docs_url="/docs" if _public_docs else None,
        redoc_url="/redoc" if _public_docs else None,
        openapi_url="/openapi.json" if _public_docs else None,
    )

    if not _public_docs:
        _mount_private_docs(app)

    _install_validation_redaction(app)

    # CORS — Next.js dev server runs on :3000 by default
    cors_origins = [
        o.strip() for o in os.environ.get(
            "CELMIS_CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Stage 21 — rate limiting + body caps, then Prometheus counters.
    # Starlette applies middleware in reverse registration order, so
    # metrics wraps rate-limit: 429s are counted too.
    try:
        from src.api.middleware import RateLimitMiddleware
        app.add_middleware(RateLimitMiddleware)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rate_limit_middleware_failed err=%s", exc)
    try:
        from src.api.metrics import MetricsMiddleware
        app.add_middleware(MetricsMiddleware)
    except Exception as exc:  # noqa: BLE001
        logger.warning("metrics_middleware_failed err=%s", exc)

    # Registered LAST so it runs FIRST — reverse order, as above. The point of
    # the guard is to answer before routing, because the route it closes is
    # /healthz, which has no dependencies to hang a check on.
    #
    # api and the sandbox share a network by necessity (api has to reach it,
    # and a Docker network carries traffic both ways) and the sandbox runs the
    # code under review. Every other route already needs a token that
    # container never receives; this one did not.
    try:
        from src.api.sandbox_guard import SandboxNetworkGuard, from_environment
        _sandbox_nets = from_environment()
        if _sandbox_nets:
            app.add_middleware(SandboxNetworkGuard, networks=_sandbox_nets)
            logger.info("sandbox_guard_active nets=%s",
                        ",".join(str(n) for n in _sandbox_nets))
        else:
            logger.warning(
                "sandbox_guard_inactive — SANDBOX_NET_SUBNET is unset, so the "
                "execution sandbox can still reach this API")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sandbox_guard_failed err=%s", exc)

    # Routers
    # Said, or refused, before a single request is served. These checks
    # existed and nothing called them: a guard with no call site is a comment
    # that takes longer to read.
    #
    # It refuses on exactly one thing — a JWT signing secret that is one of
    # the placeholders shipped in .env.example and docker-compose.yml, which
    # are public files. Verified against the production value before wiring
    # this: 64 characters, accepted. A stack that comes up on the compose
    # fallback now stops with an instruction instead of quietly signing
    # tokens anyone can forge.
    try:
        from src.deployment import run_startup_checks

        report = run_startup_checks()
        logger.info("startup_checks %s", report)
    except ImportError:  # the module is optional in a trimmed build
        logger.debug("deployment_checks_unavailable")

    app.include_router(auth_router.router)
    app.include_router(capabilities_router.router)
    app.include_router(connections_router.router)
    app.include_router(repos_router.router)
    app.include_router(reviews_router.router)
    app.include_router(webhooks_router.router)
    app.include_router(review_policies_router.router)
    app.include_router(models_router.router)
    app.include_router(agents_router.router)
    app.include_router(llm_router.router)
    # There is no byok router. `src/api/routers/byok.py` was unmounted for a
    # year — its web UI redirected to /settings/llm, no frontend called it, and
    # it wrote keys to a non-workspace slot under the old global require_admin
    # gate, which multi-tenant would have leaked or locked. Provider keys live
    # on `llm_router` above. The file has now been deleted; this note stays so
    # the next reader of an old branch knows where it went.
    app.include_router(usage_router.router)
    app.include_router(docs_router.router)
    # Stage 14
    app.include_router(compliance_router.router)
    app.include_router(teams_router.router)
    app.include_router(apply_fix_router.router)
    # Stage 15 — intel + notifications
    app.include_router(intel_router_mod.intel_router)
    app.include_router(intel_router_mod.notif_router)
    app.include_router(oauth_metadata_router.router)
    app.include_router(oauth_router.router)
    app.include_router(workspaces_router.router)
    app.include_router(jobs_router.router)
    # Embedded Claude Code agent (subscription auth)
    app.include_router(claude_code_router.router)
    # Monitoring alert ingest + "Fix with Claude"
    app.include_router(alerts_router.router)
    # Dependency audit (versions + vulnerabilities)
    app.include_router(deps_router.router)
    # Vector-store configuration (local/cloud)
    app.include_router(vector_store_router.router)
    # Resource-usage history (RAM/CPU/parallelism/LLM calls)
    app.include_router(ops_metrics_router.router)
    app.include_router(ops_gateway_router.router)
    app.include_router(push_router.router)
    # Stage 21
    app.include_router(audit_router.router)
    app.include_router(gdpr_router.router)
    app.include_router(groups_router.router)
    app.include_router(search_router_mod.search_router)
    app.include_router(search_router_mod.health_router)
    # Phase 2 — multi-repo Q&A
    app.include_router(projects_router.router)
    app.include_router(chats_router.router)
    app.include_router(qa_router.router)
    # Stage 22 — fine-grained research access + user directory
    app.include_router(access_router.router)
    app.include_router(users_router.router)
    # Stage 23 — LLM spend ledger + workspace budgets
    app.include_router(spend_router.router)
    app.include_router(feedback_router.router)
    app.include_router(invites_router.router)
    from src.api.routers import mcp_access as mcp_access_router
    app.include_router(mcp_access_router.router)
    from src.api.routers import automation as automation_router
    app.include_router(automation_router.router)

    # Mount webhook endpoints (already a FastAPI sub-app in webhook.py).
    # We import the existing routes directly so they live on the same host.
    try:
        from src.review.webhook import build_webhook_app
        webhook_app = build_webhook_app()
        # Only the webhook's OWN routes. Copying the list wholesale brought
        # the sub-app's FastAPI-generated /docs, /redoc and /openapi.json
        # along with it — which is how /redoc kept answering 200 to anybody
        # after the main app had switched all three off. A route that arrives
        # by being copied from somewhere else is exactly the kind that no
        # audit of THIS file would ever find.
        _generated = {"/docs", "/redoc", "/openapi.json",
                      "/docs/oauth2-redirect"}
        for route in webhook_app.router.routes:
            if getattr(route, "path", None) in _generated:
                continue
            app.router.routes.append(route)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_mount_failed err=%s — webhooks disabled", exc)

    # Mount MCP server at /mcp/ — makes Celmis queryable by Claude
    # Code / Cursor / any MCP client without a separate process.
    try:
        from src.mcp_server.http_app import mount_mcp
        mount_mcp(app, path="/mcp")
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_mount_top_level_failed err=%s", exc)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "celmis"}

    @app.get("/metrics")
    def metrics():  # noqa: ANN201
        from src.api.metrics import metrics_endpoint
        return metrics_endpoint()

    @app.get("/readyz")
    async def readyz() -> Any:
        """Deep readiness (Stage 21) — checks every hard dependency.

        Returns per-dependency status; 503 when any CRITICAL dep is down
        (Postgres, user store). Qdrant and LLM-key presence are reported
        but non-fatal — Q&A degrades, reviews may still work.
        """
        checks: dict[str, dict[str, Any]] = {}
        critical_down = False

        # user store (SQLite)
        try:
            from src.users import get_user_store
            checks["user_store"] = {"ok": True, "users": get_user_store().count()}
        except Exception as exc:  # noqa: BLE001
            checks["user_store"] = {"ok": False, "error": str(exc)[:200]}
            critical_down = True

        # Postgres
        try:
            from sqlalchemy import text

            from src.db.session import async_engine
            async with async_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = {"ok": False, "error": str(exc)[:200]}
            critical_down = True

        # review-run store (SQLite)
        try:
            from src.api.review_runs import get_review_run_store
            get_review_run_store()
            checks["review_store"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["review_store"] = {"ok": False, "error": str(exc)[:200]}
            critical_down = True

        # Qdrant (non-fatal)
        try:
            from src.retrieval.vector_store import get_vector_client
            client = get_vector_client()
            cols = client.get_collections()
            checks["qdrant"] = {"ok": True, "collections": len(cols.collections)}
        except Exception as exc:  # noqa: BLE001
            checks["qdrant"] = {"ok": False, "error": str(exc)[:200]}

        # LLM config presence (non-fatal, no paid ping)
        #
        # Readiness is an INSTALLATION question and this probed exactly one
        # tenant. `_load_workspace_config()` takes `workspace_id="default"`,
        # and on a multi-tenant deployment every real key lives in a
        # `ws:{id}` slot — so on production this reported
        # `{"ok": false, "provider": null}` on a system that had just spent
        # $0.94 on model calls. A readiness field that is false forever is a
        # field an operator learns to ignore, which is worse than not having
        # it.
        #
        # It now answers the question readiness actually asks: can ANY tenant
        # here reach a model. `scope` says which answer you are reading, so a
        # single-tenant install still sees its own provider name and a
        # multi-tenant one is not told a number it must then interpret.
        try:
            from src.api.routers.llm import _load_workspace_config
            cfg = _load_workspace_config()
            if cfg.get("provider"):
                checks["llm_config"] = {
                    "ok": True, "scope": "default",
                    "provider": cfg.get("provider"), "model": cfg.get("model"),
                }
            else:
                configured = _workspaces_with_llm_config()
                checks["llm_config"] = {
                    "ok": bool(configured),
                    "scope": "any_workspace",
                    "configured_workspaces": len(configured),
                }
        except Exception as exc:  # noqa: BLE001
            checks["llm_config"] = {"ok": False, "error": str(exc)[:200]}

        body = {"ok": not critical_down, "checks": checks}
        if critical_down:
            return JSONResponse(status_code=503, content=body)
        return body

    @app.on_event("startup")
    async def _startup() -> None:
        # Again: uvicorn reconfigures logging around app startup, and a handler
        # added after import would otherwise print unredacted.
        _install_log_redaction()

        # Touch user store so default user exists on cold start
        from src.users import get_user_store
        get_user_store()
        logger.info("celmis_api_started cors=%s", cors_origins)

        # Start polling background task if not disabled
        if os.environ.get("CELMIS_DISABLE_POLLER", "").strip() != "1":
            try:
                from src.review.poller import start_background_poller
                start_background_poller()
            except Exception as exc:  # noqa: BLE001
                logger.warning("poller_start_failed err=%s", exc)

        # Sync worker (Stage 18) — drains sync_jobs table.
        if os.environ.get("CELMIS_DISABLE_SYNC_WORKER", "").strip() != "1":
            try:
                from src.sync.worker import start_worker
                start_worker()
            except Exception as exc:  # noqa: BLE001
                logger.warning("sync_worker_start_failed err=%s", exc)

        # Ownership nightly rebuild (Stage 17). Disabled with
        # CELMIS_DISABLE_OWNERSHIP_SCHED=1.
        if os.environ.get("CELMIS_DISABLE_OWNERSHIP_SCHED", "").strip() != "1":
            try:
                from src.ownership.scheduler import start_ownership_scheduler
                start_ownership_scheduler()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ownership_scheduler_start_failed err=%s", exc)

        # Debug log ring buffer — makes /api/ops/logs work when the box is
        # not SSH-reachable. Cheap (in-memory, bounded).
        try:
            from src.ops.logbuf import install as install_log_buffer
            install_log_buffer()
        except Exception as exc:  # noqa: BLE001
            logger.warning("log_buffer_install_failed err=%s", exc)

        # First line in the fresh ring buffer: which commit this is. A restart
        # clears the buffer, so this doubles as the "the deploy landed" marker.
        try:
            from src.ops.build import build_info
            info = build_info()
            logger.info("celmis_build sha=%s deployed_at=%s source=%s",
                        info["git_sha_short"], info["deployed_at"], info["source"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("build_info_failed err=%s", exc)

        # Resource sampler — RAM/CPU/parallelism history for sizing docs.
        if os.environ.get("CELMIS_DISABLE_SAMPLER", "").strip() != "1":
            try:
                from src.ops.sampler import start_sampler
                start_sampler()
            except Exception as exc:  # noqa: BLE001
                logger.warning("resource_sampler_start_failed err=%s", exc)

        # Embedded Claude Code agent: mark stale-heartbeat sessions as orphaned
        # (deploy-safe — live sessions on another instance keep heartbeating)
        # and sweep leftover agent workspaces from crashed runs.
        try:
            from src.agent.runner import mark_orphaned_sessions
            from src.agent.workspace import sweep_stale_workspaces
            orphaned = await mark_orphaned_sessions()
            swept = sweep_stale_workspaces()
            if orphaned or swept:
                logger.info("agent_sessions_cleanup orphaned=%d swept=%d", orphaned, swept)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_sessions_cleanup_failed err=%s", exc)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        try:
            from src.agent.runner import shutdown_all
            await shutdown_all()
        except Exception:  # noqa: BLE001
            pass

    return app


app = build_app()
