"""Imported knowledge — a team's markdown cards next to the generated docs.

    POST   /api/kb/{slug}/import        — upload cards (.md files or a .zip)
    POST   /api/kb/{slug}/import-repo   — import the repository's own markdown,
                                          again after every index when `follow`
    POST   /api/kb/{slug}/check         — check every card again (1C objects, staleness)
    DELETE /api/kb/{slug}               — remove the imported cards
    GET    /api/kb/{slug}               — notes with their front matter, filtered
    GET    /api/kb/{slug}/note?path=    — one note: front matter, objects, links, back-links
    GET    /api/kb/{slug}/graph         — every note and the links between them
    GET    /api/kb/{slug}/changes       — notes that changed, newest first

Reading follows the documentation's own rules (src/api/routers/docs.py):
research access to the repository, and a generated note whose source path the
caller's rules conceal is left out. Writing — importing, checking, removing —
needs a member of the workspace (viewers read only). The configuration a
knowledge base checks its cards against must be a repository of the same
workspace: a card never learns what another workspace's code looks like.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from src.api.deps import ISSUE_WRITE_ROLES, current_workspace_id, get_current_user, workspace_role
from src.api.routers.docs import _readable_vault, _resolve, _vault_dir
from src.kb import catalog, changes, links, store
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["kb"])

#: Files one multipart upload may carry.
MAX_UPLOAD_PARTS = 500


def _require_writer(user: User, workspace_id: str) -> None:
    if getattr(user, "is_admin", False):
        return
    if workspace_role(user.id, workspace_id) not in ISSUE_WRITE_ROLES:
        raise HTTPException(status_code=403,
                            detail="Importing knowledge needs a member of this workspace; viewers read only.")


def _code_repo(value: str, slug: str, workspace_id: str) -> str:
    """The configuration repository to check cards against: one of this
    workspace's repositories, or "" for none."""
    v = (value or "").strip()
    if not v:
        return ""
    if v != slug:
        _vault_dir(v, workspace_id)  # 404 unless it is this workspace's repository
    return v


def _visible(slug: str, user: User, workspace_id: str) -> tuple[list, list]:
    """(notes the caller may see, their (info, body) pairs)."""
    directory, dec = _readable_vault(slug, user, workspace_id)
    pairs = []
    from src.vault.reader import vault_note_files

    for full in vault_note_files(directory)[: catalog.MAX_NOTES]:
        got = catalog.load(directory, full)
        if got is None:
            continue
        info, _body = got
        if info.denied_by(dec):
            continue
        pairs.append(got)
    return [i for i, _ in pairs], pairs


def _foreign(user: User, workspace_id: str):  # noqa: ANN202
    """Another repository's notes the caller may read, or None: a repository of
    another workspace, one that does not exist, or one the access rules hide is
    as good as absent (a link into it is dangling, never a way to learn of it)."""
    cache: dict[str, list | None] = {}

    def foreign(repo: str) -> list | None:
        if repo not in cache:
            try:
                cache[repo] = _visible(repo, user, workspace_id)[0]
            except HTTPException:
                cache[repo] = None
        return cache[repo]

    return foreign


def _bound_notes(slug: str, user: User, workspace_id: str) -> dict[str, list]:
    """Notes of the knowledge bases bound to ``slug`` as their code repository,
    for the ones the caller may read."""
    out: dict[str, list] = {}
    for other in store.bindings_to(slug):
        if other == slug:
            continue
        try:
            out[other] = _visible(other, user, workspace_id)[1]
        except HTTPException:
            continue
    return out


def _code_repo_of(slug: str) -> str:
    return str((store.load_binding(slug) or {}).get("code_repo") or "")


class ImportRepoIn(BaseModel):
    prefix: str = Field(default="", max_length=240)
    code_repo: str = Field(default="", max_length=200)
    #: Import again after every index of this repository (a push, the webhook).
    follow: bool = True


@router.post("/{slug}/import")
def import_upload(
    slug: str,
    files: list[UploadFile] = File(...),
    code_repo: str = Form(default=""),
    replace: bool = Form(default=False),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Cards from uploaded markdown files or zip archives. With ``replace``
    (the default) the upload is the whole knowledge base: imported cards it
    does not carry are removed. A plain ``def`` like the other routes here:
    the checks and the import block, and FastAPI runs it in its thread pool."""
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    code = _code_repo(code_repo, slug, workspace_id)
    if len(files) > MAX_UPLOAD_PARTS:
        raise HTTPException(status_code=413, detail=f"At most {MAX_UPLOAD_PARTS} files per upload; send a zip.")
    _, _, max_total = store._limits()
    raw: list[tuple[str, bytes]] = []
    total = 0
    for f in files:
        data = f.file.read(max_total + 1 - total)
        total += len(data)
        if total > max_total:
            raise HTTPException(status_code=413, detail=f"The upload is larger than {max_total // 1_000_000} MB.")
        raw.append((f.filename or "card.md", data))
    report = store.ImportReport(repo=slug)
    try:
        parsed = store.read_upload(raw, report)
    except store.ImportRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not parsed and not report.skipped:
        raise HTTPException(status_code=400, detail="No markdown files in the upload.")
    result = store.import_cards(
        slug, parsed, workspace_id=workspace_id, code_repo=code, replace=replace,
        binding={"source": "upload", "follow": False}, by=user.email or "import", report=report)
    return result.as_dict()


@router.post("/{slug}/import-repo")
def import_repo(
    slug: str,
    payload: ImportRepoIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """The repository's own markdown (under ``prefix``) as its knowledge base."""
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    code = _code_repo(payload.code_repo, slug, workspace_id)
    try:
        report = store.import_from_checkout(slug, workspace_id=workspace_id, prefix=payload.prefix,
                                            code_repo=code, follow=payload.follow,
                                            by=user.email or "import")
    except store.ImportRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return report.as_dict()


@router.post("/{slug}/check")
def check_cards(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    return store.recheck(slug, workspace_id=workspace_id, by=user.email or "check").as_dict()


@router.delete("/{slug}")
def remove_cards(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    return {"removed": store.remove_all(slug, workspace_id=workspace_id, by=user.email or "import")}


@router.get("/{slug}")
def list_cards(
    slug: str,
    type: str = Query(default="", max_length=40),  # noqa: A002
    status: str = Query(default="", max_length=40),
    domain: str = Query(default="", max_length=200),
    author: str = Query(default="", max_length=200),
    lang: str = Query(default="", max_length=20),
    object: str = Query(default="", max_length=300),  # noqa: A002
    tag: str = Query(default="", max_length=200),
    where: str = Query(default="", max_length=500),
    cards_only: bool = False,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Notes with their front matter. ``where`` takes ``key=value`` pairs for
    any other front matter key (``owner=ops migration=frappe``)."""
    infos, _ = _visible(slug, user, workspace_id)
    filters = {"type": type, "status": status, "domain": domain, "author": author,
               "lang": lang, "object": object, "tag": tag, **catalog.parse_where(where)}
    picked = [n for n in infos if (n.card or not cards_only) and catalog.matches(n, filters)]
    binding = store.load_binding(slug) or {}
    return {
        "repo": slug,
        "binding": {k: binding.get(k) for k in ("source", "prefix", "follow", "code_repo",
                                                 "imported_at", "checked_at", "cards") if k in binding},
        "notes": [n.summary() for n in picked],
        "types": sorted({n.type for n in infos if n.card}),
    }


@router.get("/{slug}/note")
def read_card(
    slug: str,
    path: str = Query(max_length=500),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    directory, dec = _readable_vault(slug, user, workspace_id)
    full = _resolve(directory, path)
    got = catalog.load(directory, full)
    if got is None or got[0].denied_by(dec):
        raise HTTPException(status_code=404, detail="Note not found")
    info, body = got
    _, pairs = _visible(slug, user, workspace_id)
    kb = info.kb
    objects = []
    missing = set(kb.get("objects_missing") or [])
    for name in kb.get("objects") or []:
        objects.append({"name": name, "found": None if "objects_missing" not in kb else name not in missing})
    return {
        "path": info.path, "title": info.title, "type": info.type, "card": info.card,
        "status": info.status, "origin": kb.get("origin", ""), "body": body, "fm": info.fm,
        "kb": {k: v for k, v in kb.items() if k not in ("fm",)},
        "objects": objects,
        "links": links.neighbours(info.path, pairs, repo=slug, code_repo=_code_repo_of(slug),
                                  foreign=_foreign(user, workspace_id),
                                  bound=_bound_notes(slug, user, workspace_id)),
    }


@router.get("/{slug}/graph")
def note_graph(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    _, pairs = _visible(slug, user, workspace_id)
    return links.graph(pairs, repo=slug, code_repo=_code_repo_of(slug),
                       foreign=_foreign(user, workspace_id))


@router.get("/{slug}/changes")
def changed_notes(
    slug: str,
    since: str = Query(default="", max_length=40),
    limit: int = Query(default=200, ge=1, le=changes.MAX_READ),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Notes that changed (created, rewritten, removed, marked stale or fresh
    again), newest first, one entry per note. ``since``: ``7d``, ``12h`` or an
    ISO date."""
    directory, _dec = _readable_vault(slug, user, workspace_id)
    when = changes.parse_since(since)
    if since and when is None:
        raise HTTPException(status_code=400, detail="since: use 7d, 12h, 30m or an ISO date")
    infos, _ = _visible(slug, user, workspace_id)
    visible = {n.path for n in infos}
    # A note still on disk is listed when the caller may see it. A removed one
    # is listed only when it was an imported card: a generated note's name
    # follows the source path it documented, which the caller's rules may hide.
    entries = [e for e in changes.read(directory, since=when, limit=limit)
               if e.get("path") in visible or str(e.get("path", "")).startswith(f"{store.KB_DIR}/")]
    return {"repo": slug, "since": when.isoformat() if when else "", "changes": entries}
