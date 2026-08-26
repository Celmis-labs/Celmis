"""Generated documentation — read it, and take it with you.

    GET /api/docs/{slug}                  — what has been written for this repo
    GET /api/docs/{slug}/note?path=…       — one note, rendered by the client
    GET /api/docs/{slug}/export?format=…   — the whole thing as md or docx

These notes are what "Generate vault" produces: one markdown file per module,
written by the model while it built the search index. Until now nothing read
them back, so the feature looked like a prerequisite for Q&A — which it is not;
Q&A answers from source with code display on, vault or no vault. It is a
documentation generator, and this is the part that was missing.

`path` comes from the client and names a file on disk, so it is resolved and
checked against the repo's own vault directory before anything is opened. A
path that escapes is a 404, not an error message describing the boundary.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auto_review import get_auto_review_store
from src.api.deps import current_workspace_id, get_current_user
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/docs", tags=["docs"])

#: A whole repo's documentation in one request. Past this the response stops
#: being a document and becomes a download problem.
MAX_NOTES = 400


class NoteSummary(BaseModel):
    path: str
    title: str
    updated_at: str = ""
    words: int = 0


class DocsOverview(BaseModel):
    repo_slug: str
    notes: list[NoteSummary] = []
    #: Notes on disk beyond MAX_NOTES. Non-zero means the list is partial and
    #: the export will be too — silence here would be a quiet truncation.
    omitted: int = 0


class NoteOut(BaseModel):
    path: str
    title: str
    body: str
    updated_at: str = ""


def _vault_dir(slug: str, workspace_id: str) -> Path:
    """The repo's vault directory, or 404 if it is not this workspace's repo."""
    from src.config import get_settings

    owned = {c.repo_slug for c in get_auto_review_store().list_for_workspace(workspace_id)}
    if slug not in owned:
        raise HTTPException(status_code=404, detail="Repo not registered")
    return get_settings().repo_vault_path(slug)


def _title_of(post: frontmatter.Post, path: Path) -> str:
    for key in ("title", "name", "module"):
        value = post.metadata.get(key)
        if value:
            return str(value)
    # First markdown heading, else the file name — a note without a title is
    # still worth listing.
    for line in post.content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem.replace("_", " ").replace("-", " ")


def _notes(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.rglob("*.md") if p.is_file())


# Declared BEFORE /{slug}: FastAPI matches routes in declaration order,
# so GET /api/docs/export-all would otherwise be read as a repository
# whose slug is "export-all" — a 404 that looks like a missing repo
# rather than a shadowed route.
class BulkGenerateIn(BaseModel):
    """Documentation for a SET of repositories."""

    #: Explicit list. Omit to mean "every repository in the workspace",
    #: narrowed by the other two fields.
    repo_slugs: list[str] | None = Field(default=None, max_length=50)
    #: Owner prefix — "acme" selects everything under it without naming
    #: each one, which is how people actually think about a group of services.
    owner: str | None = Field(default=None, max_length=200)
    #: The condition that makes this worth asking for: only the repositories
    #: that have no documentation yet. Without it the same request means
    #: "regenerate everything", which is hours of model time and almost never
    #: what was meant.
    missing_only: bool = False
    language: str | None = Field(default=None, max_length=16)
    engine: str | None = Field(default=None, max_length=32)


@router.post("/generate", status_code=202)
async def generate_docs_bulk(
    payload: BulkGenerateIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """Queue documentation for several repositories at once.

    Through the same action the MCP tools use, so the repository cap, the
    ownership check and the not-indexed refusal apply identically whether a
    person asked or an agent did.
    """
    from src.automation.actions import ActionError, Actor, generate_docs

    actor = Actor(user_id=user.id, email=user.email,
                  workspace_id=workspace_id, label="web")
    try:
        return await generate_docs(
            actor, session,
            repo_slugs=payload.repo_slugs, owner=payload.owner,
            missing_only=payload.missing_only,
            language=payload.language, engine=payload.engine,
        )
    except ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/export-all")
def export_all_docs(
    owner: str = Query(default="", description="Only repos under this owner"),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """Every repository's documentation in one archive, one folder per repo.

    The per-repository export existed; handing somebody "the documentation" for
    a platform of nine services meant nine downloads and nine filenames to keep
    straight. An auditor asking for technical documentation is asking for the
    set, not for one service at a time.

    Marked like the individual exports: each note keeps its provenance footer,
    so a document lifted out of this archive still says what produced it.
    """
    import io
    import zipfile

    from src.api.auto_review import get_auto_review_store
    from src.vault.provenance import as_footer

    store = get_auto_review_store()
    repos = store.list_for_workspace(workspace_id)
    if owner:
        prefix = owner.strip().rstrip("/") + "/"
        repos = [r for r in repos if r.full_name.startswith(prefix)]
    if not repos:
        raise HTTPException(status_code=404, detail="No repositories in scope.")

    buf = io.BytesIO()
    included = 0
    empty: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for cfg in sorted(repos, key=lambda r: r.repo_slug):
            directory = _vault_dir(cfg.repo_slug, workspace_id)
            notes = sorted(directory.rglob("*.md")) if directory.exists() else []
            if not notes:
                empty.append(cfg.repo_slug)
                continue
            for path in notes:
                try:
                    post = frontmatter.load(path)
                except Exception:  # noqa: BLE001 — one unreadable note is not an outage
                    continue
                body = post.content
                block = post.metadata.get("provenance")
                if isinstance(block, dict) and block:
                    body = f"{body}\n\n---\n\n_{as_footer(block)}_\n"
                zf.writestr(
                    f"{cfg.repo_slug}/{path.relative_to(directory)}", body)
                included += 1
        if empty:
            # Named inside the archive rather than left to be noticed by
            # counting folders: a download that silently covers six of nine
            # services is the failure this whole surface is about.
            zf.writestr(
                "MISSING.txt",
                "These repositories have no documentation yet:\n"
                + "\n".join(f"  - {s}" for s in sorted(empty))
                + "\n\nGenerate it from the Documentation page, or with\n"
                  "POST /api/docs/generate {\"missing_only\": true}.\n")

    if not included:
        raise HTTPException(
            status_code=404,
            detail="No documentation has been generated for these repositories yet.")

    # UTC, not timezone.utc: only the former is imported here, and the
    # latter made this line a NameError the first time somebody
    # exported every document at once.
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    logger.info("docs_export_all ws=%s repos=%d notes=%d empty=%d by=%s",
                workspace_id, len(repos), included, len(empty), user.email)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="celmis-documentation-{stamp}.zip"'},
    )


@router.get("/{slug}", response_model=DocsOverview)
def list_notes(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> DocsOverview:
    directory = _vault_dir(slug, workspace_id)
    files = _notes(directory)
    out: list[NoteSummary] = []
    for path in files[:MAX_NOTES]:
        try:
            post = frontmatter.load(path)
        except Exception:  # noqa: BLE001 — one unreadable note is not an outage
            continue
        out.append(NoteSummary(
            path=str(path.relative_to(directory)),
            title=_title_of(post, path),
            updated_at=str(post.metadata.get("updated_at") or ""),
            words=len(post.content.split()),
        ))
    return DocsOverview(
        repo_slug=slug, notes=out, omitted=max(0, len(files) - MAX_NOTES),
    )


def _resolve(directory: Path, rel: str) -> Path:
    """`rel` inside `directory`, or 404.

    Compares resolved PATH PARTS, not string prefixes: `/vault/repo-evil`
    starts with `/vault/repo` and is a different directory. resolve() follows
    symlinks first, so a link planted in the vault is not a way out either.
    """
    root = directory.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise HTTPException(status_code=404, detail="Note not found")
    if not candidate.is_file() or candidate.suffix != ".md":
        raise HTTPException(status_code=404, detail="Note not found")
    return candidate


@router.get("/{slug}/note", response_model=NoteOut)
def read_note(
    slug: str,
    path: str = Query(max_length=500),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> NoteOut:
    directory = _vault_dir(slug, workspace_id)
    full = _resolve(directory, path)
    post = frontmatter.load(full)
    return NoteOut(
        path=path,
        title=_title_of(post, full),
        body=post.content,
        updated_at=str(post.metadata.get("updated_at") or ""),
    )


@router.get("/{slug}/export")
def export_docs(
    slug: str,
    format: str = Query(default="md", pattern="^(md|docx)$"),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """The repo's documentation as one file.

    PDF is not offered here on purpose: server-side rendering means weasyprint
    and its system libraries, while the documentation page prints to PDF from
    the browser with the typography it already shows on screen.
    """
    from datetime import datetime

    from src.docs.export import Note, to_docx, to_markdown

    directory = _vault_dir(slug, workspace_id)
    files = _notes(directory)[:MAX_NOTES]
    if not files:
        raise HTTPException(
            status_code=404,
            detail="No documentation generated for this repository yet.",
        )

    notes: list[Note] = []
    for path in files:
        try:
            post = frontmatter.load(path)
        except Exception:  # noqa: BLE001
            continue
        # The mark travels into the export. A Word file attached to an email
        # or a PDF in a filing is exactly the copy somebody hands over, and
        # nobody opens the frontmatter of those — so a provenance block that
        # only lives in the .md marks the copies that never leave.
        body = post.content
        block = post.metadata.get("provenance")
        if isinstance(block, dict) and block:
            from src.vault.provenance import as_footer

            body = f"{body}\n\n---\n\n_{as_footer(block)}_\n"
        notes.append(Note(
            path=str(path.relative_to(directory)),
            title=_title_of(post, path),
            body=body,
        ))

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    logger.info("docs_exported repo=%s format=%s notes=%d by=%s",
                slug, format, len(notes), user.email)

    if format == "docx":
        return Response(
            content=to_docx(slug, notes, generated_at=stamp),
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            headers={"Content-Disposition": f'attachment; filename="{slug}-{stamp}.docx"'},
        )
    return Response(
        content=to_markdown(slug, notes, generated_at=stamp),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-{stamp}.md"'},
    )


__all__ = ["router"]


class RegenerateIn(BaseModel):
    """Redo specific notes, optionally with a different engine or language."""

    #: Vault-relative paths, e.g. ["modules/orders.md"].
    note_paths: list[str] = Field(min_length=1, max_length=50)
    #: None → the workspace setting. The reason this is per-request rather than
    #: a setting: improving ONE module PRD with the agent engine should not
    #: require rebuilding the whole vault, and it should not mean flipping a
    #: workspace default and remembering to flip it back.
    engine: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=16)


@router.post("/{slug}/regenerate", status_code=202)
def regenerate_notes(
    slug: str,
    payload: RegenerateIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Rewrite a handful of vault notes without rebuilding the vault.

    A vault build over a large repository is dozens of documents and many
    minutes. Without this, the only way to improve one weak PRD — say, to have
    the agent engine research it properly instead of the api engine
    summarising it — was to run the whole thing again.
    """
    from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
    from src.generation.engines import ENGINES

    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")

    if payload.engine is not None and payload.engine not in ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported documentation engine {payload.engine!r}. "
                   f"Expected one of: {', '.join(ENGINES)}.",
        )
    engine = resolve_doc_engine(payload.engine, workspace_id)
    language = resolve_doc_language(payload.language, workspace_id)

    from src.sync.queue import KIND_REGENERATE_NOTES, enqueue

    job_id = enqueue(
        kind=KIND_REGENERATE_NOTES,
        payload={
            "repo_slug": slug,
            "note_paths": payload.note_paths,
            "engine": engine,
            "language": language,
            "workspace_id": workspace_id,
            "user_id": user.id,
        },
        # Keyed on the workspace AND the exact note set.
        #
        # It carried neither. Without the workspace, one tenant's pending job
        # swallowed another's request; and `[:120]` made two DIFFERENT note
        # sets collide whenever their sorted join shared a 120-character
        # prefix, which one long path plus a short one is enough to produce. A
        # hash has no prefix and no length.
        dedup_key=(
            f"regenerate:{workspace_id}:{slug}:"
            + hashlib.sha256(
                "\n".join(sorted(payload.note_paths)).encode()).hexdigest()[:16]
        ),
        enqueued_by=user.email,
    )
    if job_id is None:
        # `enqueue` returns None when an identical job is already pending, and
        # the response said "Queued 2 document(s)" regardless — so pressing the
        # button twice looked like it worked twice.
        logger.info("docs_regenerate_deduped repo=%s notes=%d", slug,
                    len(payload.note_paths))
        return {
            "ok": True, "job_id": None, "engine": engine, "language": language,
            "queued": False,
            "detail": "Already queued — the same document(s) are waiting to be "
                      "rewritten. Nothing new was started.",
        }
    logger.info("docs_regenerate_queued repo=%s notes=%d engine=%s by=%s",
                slug, len(payload.note_paths), engine, user.email)
    return {
        "ok": True, "job_id": job_id, "engine": engine, "language": language,
        "queued": True,
        "detail": f"Queued {len(payload.note_paths)} document(s) for rewriting "
                  f"with the {engine} engine.",
    }
