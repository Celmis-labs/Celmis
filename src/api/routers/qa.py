"""Q&A routes — available-repos detection + ask streaming.

Multi-repo strategy:
  1. If the chat has a project_id → take all repos from ProjectRepo
  2. If the chat has a repo_slug → single-repo (backward-compat with Streamlit)

SSE streaming:
  EventSourceResponse (sse-starlette) — returns an async generator
  google-genai async streaming via client.aio.models.generate_content_stream
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from src.api import repositories as repo
from src.api.deps import current_workspace_id, get_current_user
from src.api.schemas import AskRequest, AvailableRepo, MessageOut
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/qa", tags=["qa"])


class _AlreadyReported(Exception):
    """This failure's error event is already on the wire.

    Raised instead of returning early, so the stream still reaches the block
    that persists an assistant message. A `return` there is why a blocked
    question left the transcript with nothing after it and the UI showing "…"
    forever: the failure was reported to the browser and then forgotten by the
    database.
    """


# ════════════════════════════════════════════════════════════════════
# Available repos (vault-ready via a Qdrant scan)
# ════════════════════════════════════════════════════════════════════


@router.get("/available-repos", response_model=list[AvailableRepo])
async def available_repos(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[AvailableRepo]:
    """List of repos Q&A is ready for — the ones with vault points in Qdrant.

    Stage 22: hides the repositories the current user has no right to
    research (visibility=none), so that the UI/agents only see what is
    accessible.

    Returns: [{repo_slug, vault_points, is_ready, in_projects}]
    Sort: vault_points DESC.
    """
    from src.config import get_settings

    settings = get_settings()
    try:
        from qdrant_client import models

        from src.retrieval.vector_store import get_vector_client
        qc = get_vector_client()
        # Distribution of points by the `repo` field
        # Qdrant Cloud has scroll-by-payload — we use count per filter
        # For simplicity: scroll with payload only, group them in Python
        # Scoped to the caller's workspace. This scrolled the WHOLE shared
        # collection and listed every tenant's repository slugs — and their
        # point counts — to any authenticated user. The access resolver below
        # was the only thing standing between that list and the response, and
        # it falls open for a repository with no rules configured, which is
        # every repository until somebody writes one.
        #
        # A tenant filter is not a rule anybody has to remember to configure.
        from src.retrieval.vector_store import VectorScope

        scope = VectorScope.for_workspace(workspace_id)
        conditions = scope.must_conditions()
        scroll_filter = models.Filter(must=list(conditions)) if conditions else None

        repos_seen: dict[str, int] = {}
        next_offset = None
        while True:
            points, next_offset = qc.scroll(
                collection_name=settings.qdrant_collection,
                with_payload=["repo"],
                scroll_filter=scroll_filter,
                limit=500,
                offset=next_offset,
            )
            for p in points:
                slug = (p.payload or {}).get("repo")
                if slug:
                    repos_seen[slug] = repos_seen.get(slug, 0) + 1
            if next_offset is None:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant_scan_failed: %s", exc)
        return []

    # Hide repos the caller may not research (fine-grained access).
    from src.access import resolve_access

    access = resolve_access(
        user_id=user.id, is_admin=user.is_admin,
        workspace_id=workspace_id, repos=list(repos_seen.keys()),
    )

    out: list[AvailableRepo] = []
    for slug, count in sorted(repos_seen.items(), key=lambda kv: -kv[1]):
        dec = access.get(slug)
        if dec is not None and not dec.researchable:
            continue
        in_projects = await repo.projects_containing_repo(session, slug)
        out.append(
            AvailableRepo(
                repo_slug=slug,
                vault_points=count,
                is_ready=count > 0,
                in_projects=in_projects,
            )
        )
    return out


# ════════════════════════════════════════════════════════════════════
# Ask — streaming Q&A
# ════════════════════════════════════════════════════════════════════


@router.post(
    "/chats/{chat_id}/ask",
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def ask(
    chat_id: str,
    payload: AskRequest,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Q&A request. Stream=True → SSE events.

    SSE event types:
        - meta:     once, before content — {"vault_hits": [...], "files_read_count": N}
        - delta:    partial content — {"text": "..."}
        - done:     {"message_id": N, "tokens_in": N, "tokens_out": N, "elapsed_s": ...}
        - error:    {"detail": "..."}
    """
    # 1. Load the chat — so we know which repos are linked to it.
    #
    # The workspace check is the whole reason this line reads the way it does.
    # /api/chats gained `_owned_chat` guards on every by-id handler; this route
    # lives in another router and was missed, so a signed-in user of any
    # workspace who knew a chat id could read the entire transcript (it is fed
    # to the model as history — "summarise the conversation above" is enough),
    # write their own message into it, and rename it. 404 rather than 403:
    # whether a chat exists elsewhere is not this caller's business.
    chat = await repo.get_chat(session, chat_id, with_messages=True,
                               workspace_id=workspace_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")

    # 2. Determine the target repos: the project's repos, or a single repo.
    # Scoped too: a chat may legitimately belong to this workspace while
    # carrying a project_id that does not, and an unscoped dereference here
    # would hand back the other tenant's repo list.
    target_repos: list[str] = []
    if chat.project_id:
        project = await repo.get_project(session, chat.project_id,
                                         workspace_id=workspace_id)
        if project:
            target_repos = [r.repo_slug for r in project.repos]
    if not target_repos and chat.repo_slug:
        target_repos = [chat.repo_slug]
    if not target_repos:
        raise HTTPException(
            status_code=400,
            detail="chat is linked to neither a project nor a repo",
        )

    # 3. Persist the user message
    await repo.append_message(
        session,
        chat_id,
        role="user",
        content=payload.content,
    )
    # Auto-name the chat from the first user message if name=NULL
    await repo.auto_name_chat(session, chat_id, payload.content)
    await session.commit()

    # 4. Streaming response
    if not payload.stream:
        # Non-streaming — a pure JSON response with the full assistant message
        try:
            full_text, meta = await _generate_full(
                target_repos=target_repos,
                question=payload.content,
                history=[(m.role, m.content) for m in chat.messages[:-1]],
                user_id=user.id,
                is_admin=user.is_admin,
                workspace_id=workspace_id,
                include_code=payload.include_code,
            )
        except Exception as exc:  # noqa: BLE001
            # Same rule as the streaming twin: the body goes to the log, the
            # caller gets a code and a short hint.
            logger.exception("ask_full_failed chat=%s: %s", chat_id, exc)
            from src.llm.errors import classify
            failure = classify(exc)
            raise HTTPException(
                status_code=500,
                detail=f"{failure.code}: {failure.hint}" if failure.hint else failure.code,
            ) from exc

        asst_msg = await repo.append_message(
            session, chat_id, role="assistant", content=full_text, meta=meta,
        )
        await session.commit()
        return MessageOut(
            id=asst_msg.id,
            role="assistant",
            content=full_text,
            timestamp=asst_msg.timestamp,
            meta=meta,
        )

    # SSE streaming path
    return EventSourceResponse(
        _ask_stream(
            chat_id=chat_id,
            target_repos=target_repos,
            question=payload.content,
            history=[(m.role, m.content) for m in chat.messages[:-1]],
            user_id=user.id,
            is_admin=user.is_admin,
            workspace_id=workspace_id,
            include_code=payload.include_code,
        ),
        media_type="text/event-stream",
        # Ping every 15s so that proxies do not drop the idle connection
        ping=15,
    )


# ════════════════════════════════════════════════════════════════════
# Streaming generator + plumbing
# ════════════════════════════════════════════════════════════════════


async def _ask_stream(
    *,
    chat_id: str,
    target_repos: list[str],
    question: str,
    history: list[tuple[str, str]],
    user_id: str,
    is_admin: bool,
    workspace_id: str,
    include_code: bool = True,
) -> AsyncIterator[dict]:
    """Async generator that yields SSE events."""
    t0 = time.perf_counter()
    full_chunks: list[str] = []
    vault_hits_summary: list[dict[str, Any]] = []
    files_read: list[str] = []
    blocked_repos: list[str] = []
    hidden_files: list[str] = []
    # Tier 1 (vector store) missing/unreachable — the answer still gets built
    # from grep + graph + code, and the UI shows a setup hint instead of an error.
    vault_unavailable: str | None = None
    tokens_in = tokens_out = 0
    #: Failure code, or None. Persisted as meta["error"] — a message whose body
    #: is "(generation failed)" must carry a truthy flag, or every consumer that
    #: filters on it counts the failure as a successful answer.
    error: str | None = None

    try:
        # ── 0. Workspace budget gate (hard-stop budgets refuse here) ─
        from src.llm.budget import BudgetExceeded
        from src.llm.budget import enforce as _enforce_budget
        try:
            _enforce_budget(workspace_id)
        except BudgetExceeded as exc:
            # Carries a code like every other failure — without one the client
            # falls through to rendering `detail` raw, which is the branch this
            # whole change exists to close.
            logger.info("ask_stream_budget_blocked chat=%s: %s", chat_id, exc)
            error = "budget_exceeded"
            yield {"event": "error",
                   "data": _json_compact({"detail": "", "code": "budget_exceeded"})}
            # NOT `return`. Returning here skipped the persistence block below,
            # so the transcript kept the question with nothing after it and the
            # UI showed "…" forever — a failure that leaves no trace is the one
            # nobody can debug.
            raise _AlreadyReported from exc

        # ── 1. Multi-repo retrieval ─────────────────────────────────
        from src.qa.multi_repo_retriever import MultiRepoRetriever

        retriever = MultiRepoRetriever()
        context = await retriever.retrieve(
            question=question,
            repos=target_repos,
            history=history,
            user_id=user_id,
            is_admin=is_admin,
            workspace_id=workspace_id,
            include_code=include_code,
        )
        vault_hits_summary = [
            {"note_path": h.note_path, "score": h.score, "repo": h.repo}
            for h in context.vault_hits
        ]
        files_read = context.files_read
        blocked_repos = context.blocked_repos
        vault_unavailable = context.vault_unavailable
        hidden_files = context.hidden_files

        yield {
            "event": "meta",
            "data": _json_compact({
                "vault_hits": vault_hits_summary,
                "files_read_count": len(files_read),
                "repos": target_repos,
                "blocked_repos": blocked_repos,
                "vault_unavailable": vault_unavailable,
                "hidden_files_count": len(hidden_files),
                "code_included": include_code,
                # True when the answer was built from entry points and the
                # symbol graph because no vault existed. Without this the UI
                # cannot tell "no semantic search" from "no code at all", and
                # those need different advice.
                "code_fallback_used": getattr(context, "code_fallback_used", False),
            }),
        }

        # ── 2. Stream generation via the configured `chat` profile ──
        from src.llm.completion import stream_chat

        async for chunk_text in stream_chat(
            prompt=context.prompt,
            system_instruction=context.system_instruction,
            question=question,
            files_sent=files_read,
            workspace_id=workspace_id,
            user_id=user_id,
        ):
            full_chunks.append(chunk_text)
            yield {
                "event": "delta",
                "data": _json_compact({"text": chunk_text}),
            }
        # tokens — approximated from the accumulated chunks
        from src.llm.gemini_client import _estimate_tokens
        tokens_in = _estimate_tokens(context.prompt)
        tokens_out = _estimate_tokens("".join(full_chunks))
    except asyncio.CancelledError:
        # Client disconnected — we keep the partial result in the DB
        logger.info("ask_stream_cancelled chat=%s", chat_id)
        raise
    except _AlreadyReported:
        # The error event is already on the wire. Fall through to persistence
        # rather than re-classifying and yielding a second one.
        pass
    except Exception as exc:  # noqa: BLE001
        # The provider's full response body stays here and only here. It used
        # to travel to the browser verbatim and get pasted into a toast, where
        # a quota error filled half the screen with nested JSON.
        logger.exception("ask_stream_failed chat=%s: %s", chat_id, exc)
        from src.llm.errors import classify
        failure = classify(exc)
        # `error` was declared and never assigned, so meta["error"] was
        # bool(None) — False — on every failure, including messages whose whole
        # body is "(generation failed)". Anything filtering on that flag (the
        # UI, the usage view, a future alert) saw a clean run.
        error = failure.code or "generation_failed"
        yield {
            "event": "error",
            "data": _json_compact({"detail": failure.hint, "code": failure.code}),
        }

    # ── 3. Persist the assistant message regardless of outcome ─────
    elapsed = time.perf_counter() - t0
    full_text = "".join(full_chunks) or "(generation failed)"

    # Verify every file:line the model cited actually exists. Generation is
    # streamed, so we report rather than rewrite — the UI flags bad citations.
    #
    # Spend ledger: written by the provider layer (`src.llm.completion`), which
    # sees the real usage_metadata and covers the non-streaming variant too.
    # Recording here as well would double-count every answer.
    citation_meta: dict = {}
    if full_chunks:
        try:
            from src.qa.citations import verify_citations
            citation_meta = verify_citations(
                full_text, files_read=files_read, repos=target_repos,
            ).as_meta()
        except Exception as exc:  # noqa: BLE001
            logger.warning("citation_verify_failed chat=%s err=%s", chat_id, exc)
    meta = {
        "type": None,
        "route": "C",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "vault_hits": vault_hits_summary,
        "files_read": files_read,
        "files_read_count": len(files_read),
        "blocked_repos": blocked_repos,
        "vault_unavailable": vault_unavailable,
        "hidden_files": hidden_files,
        "hidden_files_count": len(hidden_files),
        "code_included": include_code,
        "elapsed_s": round(elapsed, 2),
        "error": bool(error),
        # The code as well as the flag: "it failed" sends someone to the logs,
        # "quota_exhausted" sends them to the provider.
        "error_code": error,
        **citation_meta,
    }

    # Create a separate session so that the yielding generator does not block
    # the request session
    from src.db import async_session
    async with async_session() as s:
        msg = await repo.append_message(
            s, chat_id, role="assistant", content=full_text, meta=meta,
        )
        await s.commit()
        msg_id = msg.id

    yield {
        "event": "done",
        "data": _json_compact({
            "message_id": msg_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "elapsed_s": meta["elapsed_s"],
            "citations_total": citation_meta.get("citations_total", 0),
            "citations_invalid": citation_meta.get("citations_invalid", 0),
        }),
    }


async def _generate_full(
    *,
    target_repos: list[str],
    question: str,
    history: list[tuple[str, str]],
    user_id: str = "default",
    is_admin: bool = True,
    workspace_id: str = "default",
    include_code: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Non-streaming alternate — the full text + meta in one block."""
    from src.llm.gemini_client import _estimate_tokens
    from src.qa.multi_repo_retriever import MultiRepoRetriever

    retriever = MultiRepoRetriever()
    context = await retriever.retrieve(
        question=question, repos=target_repos, history=history,
        user_id=user_id, is_admin=is_admin, workspace_id=workspace_id,
        include_code=include_code,
    )
    from src.llm.completion import stream_chat
    full = []
    async for chunk in stream_chat(
        prompt=context.prompt,
        system_instruction=context.system_instruction,
        question=question,
        files_sent=context.files_read,
        workspace_id=workspace_id,
        user_id=user_id,
    ):
        full.append(chunk)
    text = "".join(full)
    meta = {
        "type": None,
        "route": "C",
        "tokens_in": _estimate_tokens(context.prompt),
        "tokens_out": _estimate_tokens(text),
        "vault_hits": [
            {"note_path": h.note_path, "score": h.score, "repo": h.repo}
            for h in context.vault_hits
        ],
        "files_read": context.files_read,
        "files_read_count": len(context.files_read),
        "blocked_repos": context.blocked_repos,
        "hidden_files": context.hidden_files,
        "hidden_files_count": len(context.hidden_files),
        "code_included": context.code_included,
    }
    try:
        from src.qa.citations import verify_citations
        meta.update(verify_citations(
            text, files_read=context.files_read, repos=target_repos,
        ).as_meta())
    except Exception as exc:  # noqa: BLE001
        logger.warning("citation_verify_failed err=%s", exc)
    return text, meta


def _json_compact(obj: Any) -> str:
    """Serialize as single-line JSON for the SSE data: lines."""
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
