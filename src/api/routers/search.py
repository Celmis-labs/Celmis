"""Cross-repo search + integration health (Stage 21).

    GET /api/search?q=...&repo=...&limit=...
        Combined search: exact/prefix symbol lookup across every indexed
        repo graph + semantic vault-note search (Qdrant). Two result
        sections so the UI can render "Code symbols" and "Docs" blocks.

    GET /api/health/integrations
        Ops dashboard payload: git connections, LLM providers, MCP
        sources, Qdrant reachability, notification channels — each with
        a status + last-activity hint. Read-only aggregation, no probes
        that cost money (LLM providers are NOT pinged — we report key
        presence + last verification timestamp instead).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

search_router = APIRouter(prefix="/api/search", tags=["search"])
health_router = APIRouter(prefix="/api/health", tags=["health"])


# ═══ Search ══════════════════════════════════════════════════════════


@search_router.get("")
def search(
    q: str = Query(min_length=2, max_length=200),
    repo: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    # Workspace boundary: only the active workspace's registered repos —
    # both for tenant isolation and to keep results relevant.
    from src.api.auto_review import get_auto_review_store
    cfgs = {c.repo_slug: c
            for c in get_auto_review_store().list_for_workspace(workspace_id)}
    symbols, symbols_error = _search_symbols(q, repo=repo, limit=limit, cfgs=cfgs)
    notes, notes_error = _search_notes(
        q, repo=repo, limit=min(limit, 10), cfgs=cfgs, workspace_id=workspace_id)
    return {
        "query": q,
        "symbols": symbols,
        "notes": notes,
        "symbol_count": len(symbols),
        "note_count": len(notes),
        "symbols_error": symbols_error,
        "notes_error": notes_error,
    }


def _web_file_url(cfg, file: str | None, line: int | None) -> str | None:
    """Best-effort provider file URL built from the stored repo URL, the
    clone's checked-out branch, and the indexed path."""
    if cfg is None or not file:
        return None
    base = (cfg.url or "").rstrip("/").removesuffix(".git")
    if not base.startswith("http"):
        return None
    branch = _repo_branch(cfg.repo_slug)
    if cfg.provider == "gitlab":
        url = f"{base}/-/blob/{branch}/{file}"
        return url + (f"#L{line}" if line else "")
    if cfg.provider == "bitbucket":
        url = f"{base}/src/{branch}/{file}"
        return url + (f"#lines-{line}" if line else "")
    url = f"{base}/blob/{branch}/{file}"
    return url + (f"#L{line}" if line else "")


def _repo_branch(repo_slug: str) -> str:
    """Checked-out branch of the local clone — the branch that was indexed,
    so line anchors match. Cached per process."""
    cached = _BRANCH_CACHE.get(repo_slug)
    if cached:
        return cached
    import subprocess

    from src.config import get_settings
    path = get_settings().repo_path(repo_slug)
    branch = "main"
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            branch = r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    _BRANCH_CACHE[repo_slug] = branch
    return branch


_BRANCH_CACHE: dict[str, str] = {}


def _search_symbols(
    q: str, *, repo: str | None, limit: int, cfgs: dict,
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        from src.mcp_server import tools as legacy
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)[:200]
    out: list[dict[str, Any]] = []
    for slug, cfg in cfgs.items():
        if repo and slug != repo:
            continue
        try:
            # Substring match (exact=False) so "models" also finds Model,
            # UserModel, models.py-level symbols.
            for sym in legacy.find_symbol(
                    name=q, repo_slug=slug, limit=limit, exact=False):
                out.append({
                    "repo_slug": sym.get("repo_slug", slug),
                    "name": sym.get("name"),
                    "kind": sym.get("kind"),
                    "file": sym.get("file"),
                    "line": sym.get("start_line"),
                    "language": sym.get("language"),
                    "web_url": _web_file_url(
                        cfg, sym.get("file"), sym.get("start_line")),
                })
                if len(out) >= limit:
                    return out, None
        except Exception as exc:  # noqa: BLE001
            logger.warning("search_symbols_repo_failed repo=%s err=%s", slug, exc)
    return out, None


# Sentinel the web app matches on — keep in sync with search/page.tsx.
VAULT_NOT_GENERATED = "vault-not-generated"


def _search_notes(
    q: str, *, repo: str | None, limit: int, cfgs: dict, workspace_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Semantic vault search — costs one embedding call per query."""
    from src.retrieval.tier1_vault import CollectionMissing

    try:
        from src.config import get_settings
        from src.retrieval.tier1_vault import VaultRetriever
        retriever = VaultRetriever(get_settings(), workspace_id=workspace_id)
        hits = retriever.search(q, repo=repo, top_k=limit)
        out = []
        for h in hits:
            if h.repo and cfgs and h.repo not in cfgs:
                continue  # a different workspace — do not show it
            cfg = cfgs.get(h.repo)
            out.append({
                "note_path": h.note_path,
                "score": round(float(h.score), 4),
                "type": h.type,
                "module": h.module,
                "repo": h.repo,
                "keywords": list(h.keywords or [])[:8],
                "path": getattr(h, "path", None),
                "web_url": _web_file_url(cfg, getattr(h, "path", None), None),
            })
        return out, None
    except CollectionMissing:
        # Not a failure — a workspace whose vault has never been generated.
        # Reporting the raw Qdrant 404 body told the user their search broke
        # when the honest answer is "nothing indexed yet"; the UI maps this
        # code to that sentence.
        #
        # Raised now BEFORE the embedding call rather than caught after it, so
        # a search on a workspace with no vault costs nothing. It used to
        # embed the question, ask Qdrant, and bill the call for a 404 — on
        # every keystroke-driven search, in the one state where the user can
        # do nothing but try again.
        return [], VAULT_NOT_GENERATED
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_notes_failed err=%s", exc)
        text = str(exc)
        # Back-compat: a Qdrant that 404s for some other reason, and any
        # caller still on the old path.
        if "doesn't exist" in text or "Not found: Collection" in text:
            return [], VAULT_NOT_GENERATED
        return [], text[:200]


# ═══ Integration health ══════════════════════════════════════════════


@health_router.get("/integrations")
async def integrations_health(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    cards: list[dict[str, Any]] = []

    # ── Git provider connections
    try:
        from src.credentials import get_credential_store
        from src.credentials.git_keys import git_workspace_slot
        store = get_credential_store()
        # The WORKSPACE slot, which is where connections are actually stored.
        #
        # This read `user_id=user.id` — a slot nothing writes to. Every git
        # connection saved through the UI lands in `ws:{workspace_id}` (see
        # `connections._slot_for`), so the health page reported "not
        # configured / no token saved" for providers that were connected and
        # working. Observed on production: `/api/connections` returned github
        # connected as `celmis-codereviewer`, and this endpoint called the
        # same provider not_configured in the same second. The card was
        # structurally incapable of ever saying "connected".
        slot = git_workspace_slot(ws_id)
        for prov in ("github", "gitlab", "bitbucket"):
            row = None
            # An unreadable slot is a card that says "not configured",
            # not a 500 for the whole health page.
            with contextlib.suppress(Exception):
                row = store.load(provider=prov, user_id=slot, account_label="default")
            if row is None:
                # Back-compat: tokens saved before connections moved to the
                # workspace slot still live under the user's own id. Reading
                # only the new slot would report a working legacy connection
                # as missing — the same lie, one migration later.
                with contextlib.suppress(Exception):
                    row = store.load(provider=prov, user_id=user.id,
                                     account_label="default")
            cards.append({
                "kind": "git", "name": prov,
                "status": "connected" if row else "not_configured",
                "detail": f"updated {row.updated_at}" if row else "no token saved",
            })
    except Exception as exc:  # noqa: BLE001
        cards.append({"kind": "git", "name": "credentials-store",
                      "status": "error", "detail": str(exc)[:200]})

    # ── LLM providers (BYOK keys; presence only — no paid pings)
    try:
        from src.credentials import get_credential_store
        store = get_credential_store()
        # Same slot as the git cards above: `connections._slot_for` puts
        # every provider, git AND llm, in `ws:{workspace_id}`. This block had
        # the identical defect and would have reported every BYOK key as
        # absent.
        for prov in ("anthropic", "openai", "google", "mistral", "groq", "openrouter"):
            row = None
            # An unreadable slot is a card that says "not configured",
            # not a 500 for the whole health page.
            with contextlib.suppress(Exception):
                row = store.load(provider=prov, user_id=slot, account_label="default")
            if row is None:
                with contextlib.suppress(Exception):
                    row = store.load(provider=prov, user_id=user.id,
                                     account_label="default")
            if row:
                cards.append({
                    "kind": "llm", "name": prov, "status": "connected",
                    "detail": (row.metadata or {}).get("last_verified") or "key saved",
                })
    except Exception:  # noqa: BLE001
        pass

    # ── Qdrant reachability (cheap collections call)
    try:
        from src.retrieval.vector_store import get_vector_client
        client = get_vector_client()
        cols = client.get_collections()
        cards.append({
            "kind": "vector", "name": "qdrant", "status": "healthy",
            "detail": f"{len(cols.collections)} collections",
        })
    except Exception as exc:  # noqa: BLE001
        cards.append({"kind": "vector", "name": "qdrant",
                      "status": "unreachable", "detail": str(exc)[:150]})

    # ── MCP sources (from repo policies in this workspace)
    try:
        from src.db.models import RepoReviewPolicy
        rows = (await session.scalars(
            select(RepoReviewPolicy).where(RepoReviewPolicy.workspace_id == ws_id)
        )).all()
        seen: set[str] = set()
        for r in rows:
            for src in (r.mcp_sources or []):
                name = src.get("name", "?")
                if name in seen:
                    continue
                seen.add(name)
                cards.append({
                    "kind": "mcp", "name": name,
                    "status": "configured",
                    "detail": f"url={src.get('url', '?')[:60]} repo={r.repo_slug}",
                })
    except Exception as exc:  # noqa: BLE001
        logger.debug("health_mcp_failed err=%s", exc)

    # ── Notification channels
    try:
        from src.db.models import NotificationChannel
        rows = (await session.scalars(
            select(NotificationChannel)
            .where(NotificationChannel.workspace_id == ws_id)
        )).all()
        for r in rows:
            cards.append({
                "kind": "notification", "name": r.name,
                "status": "enabled" if r.enabled else "disabled",
                "detail": r.kind,
            })
    except Exception:  # noqa: BLE001
        pass

    # ── Job queue
    try:
        from src.sync.queue import stats
        # SCOPED. `stats()` with no argument counts the whole installation, and
        # this card is rendered on a tenant's own health page — so one
        # workspace's operator read another's backlog, and a workspace with an
        # empty queue saw "degraded" because somebody else had a dead job.
        # `stats` has taken a `workspace_id` since it was written, for exactly
        # the reason its docstring gives: "the tiles above the list must not
        # count rows the list refuses to show, or the page lies." This caller
        # was the one that did not pass it.
        s = stats(workspace_id=ws_id)
        dead = s.get("dead", 0)
        cards.append({
            "kind": "queue", "name": "sync_jobs",
            "status": "degraded" if dead else "healthy",
            "detail": ", ".join(f"{k}={v}" for k, v in sorted(s.items())) or "empty",
        })
    except Exception as exc:  # noqa: BLE001
        cards.append({"kind": "queue", "name": "sync_jobs",
                      "status": "error", "detail": str(exc)[:150]})

    return {"cards": cards, "count": len(cards)}


__all__ = ["search_router", "health_router"]
