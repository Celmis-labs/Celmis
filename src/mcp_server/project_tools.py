"""The two tools a project token can use: ``search_project`` and ``ask_project``.

Both answer about the token's own project only. The project (and workspace)
come from the token's database row — they are not parameters, so there is
nothing for the caller to point somewhere else. Every file read goes through the
project's per-repository include/exclude patterns and the repo's research deny
rules (:mod:`src.access.file_scope`, :mod:`src.access.effective`).

``search_project`` is free: symbol names from the graph plus a plain-text search
over the files, so a repository in a language the graph has no extractor for
(uploaded BSL code, XML, plain text) is still searchable. ``ask_project`` is the
Q&A pipeline over the same repositories and costs one model call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from src.access.effective import Principal, effective_access
from src.access.file_scope import FileScope, apply_scopes, scopes_for_links
from src.mcp_server import project_tokens as pt

logger = logging.getLogger(__name__)

MAX_QUERY_CHARS = 300
MAX_LIMIT = 50
ASK_TIMEOUT_S = 150.0
MAX_ANSWER_CHARS = 12_000
NOT_A_PROJECT_TOKEN = "This tool needs a project token (cmcp_…)."


class ProjectToolError(Exception):
    """Refusal with a sentence for the caller."""


@dataclass(frozen=True)
class ProjectScope:
    """Everything a tool body may touch: one project's repos, their file
    scopes and the research decisions, all resolved from the token's row."""

    view: pt.ProjectTokenView
    name: str
    slugs: tuple[str, ...]
    file_scopes: dict[str, FileScope]
    access: dict


def resolve_scope(view: pt.ProjectTokenView | None) -> ProjectScope:
    """The scope of ``view``'s project. ProjectToolError when the token is not
    usable or its project is gone."""
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import Project

    if view is None:
        raise ProjectToolError(NOT_A_PROJECT_TOKEN)
    problem = view.problem()
    if problem:
        raise ProjectToolError(problem)
    with Session(_sync_engine()) as s:
        project = s.get(Project, view.project_id)
        if project is None or project.workspace_id != view.workspace_id:
            raise ProjectToolError("The project of this token no longer exists.")
        slugs = tuple(r.repo_slug for r in project.repos)
        file_scopes = scopes_for_links(project.repos)
        name = project.name
    access = effective_access(
        Principal(f"project-token:{view.id}", False, slugs), view.workspace_id, slugs)
    access = apply_scopes(access, file_scopes)
    return ProjectScope(view, name, slugs, file_scopes, access)


def _readable(scope: ProjectScope) -> list[str]:
    return [s for s in scope.slugs
            if (d := scope.access.get(s)) is not None and d.code_visible]


def search_project(view: pt.ProjectTokenView | None, query: str,
                   repo_slug: str | None = None, limit: int = 15) -> dict[str, Any]:
    """Symbols and text lines matching ``query`` in the token's project."""
    from src.qa import text_search

    scope = resolve_scope(view)
    q = str(query or "").strip()[:MAX_QUERY_CHARS]
    if len(q) < 2:
        raise ProjectToolError("Say what to search for (at least two characters).")
    n = max(1, min(int(limit or 15), MAX_LIMIT))
    repos = _readable(scope)
    if repo_slug and str(repo_slug).strip():
        wanted = str(repo_slug).strip()
        if wanted not in repos:
            # The same answer for a repo of another project and one that does not exist.
            raise ProjectToolError(f"{wanted!r} is not a repository of this project.")
        repos = [wanted]

    symbols = _symbols(scope, repos, q, n)
    text: list[dict[str, Any]] = []
    complete = True
    per_repo_budget = max(3.0, 20.0 / max(1, len(repos)))
    for slug in repos:
        from src.config import get_settings

        root = get_settings().repo_path(slug)
        if not root.is_dir():
            continue
        dec = scope.access[slug]
        hits, done = text_search.search_text(
            root, q, limit=n, visible=dec.path_visible, time_budget=per_repo_budget)
        complete = complete and done
        text.extend({"repo_slug": slug, "file": h.path, "line": h.line,
                     "snippet": h.snippet, "score": h.score} for h in hits)
    text.sort(key=lambda h: (-h["score"], h["repo_slug"], h["file"], h["line"]))
    text = text[:n]
    return {
        "project": scope.name, "query": q, "repos": repos,
        "symbols": symbols, "text": text,
        "count": len(symbols) + len(text), "complete": complete,
    }


def _symbols(scope: ProjectScope, repos: list[str], query: str, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        from src.mcp_server.dev_profile import common, rank
    except Exception:  # noqa: BLE001 — the text search still answers
        return out
    for slug in repos:
        dec = scope.access[slug]
        try:
            with common.open_store(slug) as store:
                rows = store.find_symbols(query, mode="auto", kind=None, limit=100)
        except Exception as exc:  # noqa: BLE001 — a repo without a graph has no symbols
            logger.debug("project_symbols_skipped repo=%s err=%s", slug, type(exc).__name__)
            continue
        for row in rows:
            fpath = str(row.get("file") or "")
            if fpath and not dec.path_visible(fpath):
                continue
            sc = rank.score(row, query)
            if sc <= 0:
                continue
            out.append({"repo_slug": slug, "name": row.get("name"), "kind": row.get("kind"),
                        "file": fpath, "line": row.get("start_line"), "_score": sc})
    out.sort(key=lambda m: -m["_score"])
    for m in out:
        m.pop("_score", None)
    return out[:n]


async def ask_project(view: pt.ProjectTokenView | None, question: str,
                      include_code: bool = True) -> dict[str, Any]:
    """Answer a question about the token's project (one model call)."""
    from src.api.routers import qa as qa_router
    from src.llm.budget import BudgetExceeded
    from src.llm.budget import enforce as enforce_budget

    scope = await asyncio.to_thread(resolve_scope, view)
    text = str(question or "").strip()
    if not text:
        raise ProjectToolError("Ask a question about the code.")
    repos = _readable(scope)
    if not repos:
        raise ProjectToolError("This project has no repository you can ask about.")
    try:
        enforce_budget(scope.view.workspace_id)
    except BudgetExceeded as exc:
        raise ProjectToolError(f"The workspace budget is used up: {exc}") from None
    try:
        answer, meta = await asyncio.wait_for(
            qa_router._generate_full(
                target_repos=repos, question=text[:2000], history=[],
                user_id=f"project-token:{scope.view.id}", is_admin=False,
                workspace_id=scope.view.workspace_id, include_code=bool(include_code),
                token_filter=tuple(scope.slugs), name_free_notice=True,
                file_scopes=scope.file_scopes),
            timeout=ASK_TIMEOUT_S)
    except TimeoutError:
        raise ProjectToolError("The answer took too long — narrow the question.") from None
    except ProjectToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        from src.llm.errors import classify

        logger.exception("ask_project_failed project=%s", scope.view.project_id)
        failure = classify(exc)
        raise ProjectToolError(
            f"{failure.code}: {failure.hint}" if failure.hint else failure.code) from None
    truncated = len(answer) > MAX_ANSWER_CHARS
    files = [str(f) for f in (meta.get("files_read") or [])][:15]
    return {
        "project": scope.name, "question": text[:300],
        "answer": answer[:MAX_ANSWER_CHARS] + ("…" if truncated else ""),
        "truncated": truncated, "repos": repos, "files": files,
        "files_total": len(meta.get("files_read") or []),
    }


def register_project_tools(mcp) -> None:  # noqa: ANN001
    """Register the two tools on the full MCP server."""

    @mcp.tool(
        name="search_project",
        description=(
            "Search the code of the project this token belongs to: symbol names "
            "from the index plus a text search over the files (works for any "
            "language, Unicode names included). Optional repo_slug narrows to "
            "one repository of the project. Free; bounded by limit (default 15, "
            "max 50)."
        ),
    )
    async def _search_project(
        query: str, repo_slug: str | None = None, limit: int = 15,
    ) -> dict[str, Any]:
        try:
            return {"ok": True, **await asyncio.to_thread(
                search_project, pt.current_view(), query, repo_slug, limit)}
        except ProjectToolError as exc:
            return {"ok": False, "error": str(exc)}

    @mcp.tool(
        name="ask_project",
        description=(
            "Ask a question about the code of the project this token belongs to. "
            "The answer is built from the index and the files read (listed in "
            "`files`). Costs one model call against the workspace budget; for "
            "plain lookups use search_project, which is free."
        ),
    )
    async def _ask_project(question: str) -> dict[str, Any]:
        try:
            return {"ok": True, **await ask_project(pt.current_view(), question)}
        except ProjectToolError as exc:
            return {"ok": False, "error": str(exc)}
