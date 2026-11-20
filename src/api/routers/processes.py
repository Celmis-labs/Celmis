"""A repository's own list of business processes, and the notes generated from it.

    GET    /api/kb/{slug}/processes              — the list, with each process's note and last result
    PUT    /api/kb/{slug}/processes              — replace the list (JSON)
    POST   /api/kb/{slug}/processes/upload       — replace (or merge into) the list from a
                                                   .json / .yaml / .csv file
    DELETE /api/kb/{slug}/processes/{id}         — remove one process and its generated note
    POST   /api/kb/{slug}/processes/generate     — queue the notes: all, or ``ids``

Reading follows the documentation's rules (research access to the repository);
writing — saving the list, deleting, generating — needs a member of the
workspace, like importing knowledge. A repository of another workspace is 404.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers.docs import _readable_vault
from src.api.routers.kb import _require_writer
from src.kb import processes
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb/{slug}/processes", tags=["kb"])


class ProcessIn(BaseModel):
    id: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=processes.MAX_NAME * 2)
    description: str = Field(default="", max_length=processes.MAX_DESCRIPTION * 2)
    objects: list[str] = Field(default_factory=list, max_length=processes.MAX_OBJECTS * 2)
    domain: str = Field(default="", max_length=400)
    tags: list[str] = Field(default_factory=list, max_length=processes.MAX_TAGS * 2)


class ProcessListIn(BaseModel):
    processes: list[ProcessIn] = Field(default_factory=list, max_length=processes.MAX_PROCESSES * 2)


class GenerateIn(BaseModel):
    #: Ids to write; none = every process of the list.
    ids: list[str] | None = Field(default=None, max_length=processes.MAX_PROCESSES)
    engine: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=16)


def _refuse(exc: processes.ProcessSpecError) -> HTTPException:
    return HTTPException(status_code=400, detail={"message": str(exc), "problems": exc.problems[:50]})


def _view(slug: str) -> dict:
    specs = processes.load(slug)
    notes = processes.generated_notes(slug)
    run = processes.last_run(slug)
    results = run.get("results") if isinstance(run.get("results"), dict) else {}
    rows = []
    for s in specs:
        canon, plain = processes.split_objects(s)
        row = s.as_dict()
        row["canonical_objects"] = canon
        row["other_targets"] = plain
        got = notes.get(processes.note_path(s.id))
        if got is not None:
            kb = got[0]["kb"]
            row["note"] = {
                "path": processes.note_path(s.id), "status": kb.get("status", "draft"),
                "effective_status": kb.get("effective_status") or kb.get("status", "draft"),
                "stale": (kb.get("staleness") or {}).get("state") == "stale",
                "generated_at": kb.get("generated_at", ""),
                "objects_missing": kb.get("targets_missing") or kb.get("objects_missing") or [],
            }
        if s.id in results:
            row["result"] = results[s.id]
        rows.append(row)
    return {
        "repo": slug, "processes": rows,
        "limits": {"processes": processes.MAX_PROCESSES, "name": processes.MAX_NAME,
                   "description": processes.MAX_DESCRIPTION, "objects": processes.MAX_OBJECTS},
        "last_run": {k: v for k, v in run.items() if k != "results"},
    }


@router.get("")
def list_processes(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    _readable_vault(slug, user, workspace_id)
    return _view(slug)


@router.put("")
def replace_processes(
    slug: str,
    payload: ProcessListIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    try:
        specs = processes.normalise([p.model_dump() for p in payload.processes])
    except processes.ProcessSpecError as exc:
        raise _refuse(exc) from None
    processes.save(slug, specs, by=user.email or "")
    return _view(slug)


@router.post("/upload")
def upload_processes(
    slug: str,
    file: UploadFile = File(...),
    replace: bool = Form(default=True),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """The list from a .json, .yaml/.yml or .csv file. With ``replace`` (the
    default) the file is the whole list; otherwise its processes are added to the
    list, replacing those with the same id."""
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    data = file.file.read(processes.MAX_UPLOAD_BYTES + 1)
    try:
        rows = processes.parse_upload(file.filename or "", data)
        incoming = processes.normalise(rows)
        if not replace:
            merged = {s.id: s for s in processes.load(slug)}
            merged.update({s.id: s for s in incoming})
            incoming = processes.normalise([s.as_dict() for s in merged.values()])
    except processes.ProcessSpecError as exc:
        raise _refuse(exc) from None
    processes.save(slug, incoming, by=user.email or "")
    return _view(slug)


@router.post("/generate", status_code=202)
def generate(
    slug: str,
    payload: GenerateIn | None = None,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Queue the description notes: one model call per process, as one job."""
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
    from src.generation.engines import ENGINES
    from src.llm.prompts.language import DOC_LANGUAGES

    body = payload or GenerateIn()
    if body.engine is not None and body.engine not in ENGINES:
        raise HTTPException(status_code=400, detail=f"Unsupported documentation engine {body.engine!r}. "
                                                    f"Expected one of: {', '.join(ENGINES)}.")
    if body.language is not None and body.language not in DOC_LANGUAGES:
        raise HTTPException(status_code=400,
                            detail=f"Unsupported documentation language {body.language!r}. "
                                   f"Expected one of: {', '.join(sorted(DOC_LANGUAGES))}.")
    listed = {s.id for s in processes.load(slug)}
    if not listed:
        raise HTTPException(status_code=400, detail="The list of processes is empty.")
    ids = None
    if body.ids is not None:
        ids = sorted(dict.fromkeys(body.ids))
        unknown = [i for i in ids if i not in listed]
        if unknown or not ids:
            raise HTTPException(status_code=400, detail="Not in the list: " + ", ".join(unknown[:10])
                                if unknown else "No process chosen.")
    engine = resolve_doc_engine(body.engine, workspace_id)
    language = resolve_doc_language(body.language, workspace_id)

    from src.sync.queue import KIND_GENERATE_PROCESSES, enqueue

    job_id = enqueue(
        kind=KIND_GENERATE_PROCESSES,
        payload={"repo_slug": slug, "workspace_id": workspace_id, "user_id": user.id,
                 "ids": ids, "engine": engine, "language": language},
        # Per workspace and repository, and per chosen set: a request for one
        # process is not swallowed by a pending run of another.
        dedup_key=(f"generate_processes:{workspace_id}:{slug}:"
                   + ("all" if ids is None else hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16])),
        enqueued_by=user.email,
    )
    if job_id is None:
        return {"ok": True, "job_id": None, "queued": False, "engine": engine, "language": language,
                "detail": "A run for these processes is already queued. Nothing new was started."}
    return {"ok": True, "job_id": job_id, "queued": True, "engine": engine, "language": language,
            "count": len(ids) if ids is not None else len(listed),
            "detail": "Process notes queued — one model call per process."}


@router.delete("/{process_id}")
def delete_process(
    slug: str,
    process_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Remove a process from the list, and the note generated for it."""
    _readable_vault(slug, user, workspace_id)
    _require_writer(user, workspace_id)
    specs = processes.load(slug)
    kept = [s for s in specs if s.id != process_id]
    if len(kept) == len(specs):
        raise HTTPException(status_code=404, detail="No such process in the list")
    processes.save(slug, kept, by=user.email or "")
    removed = processes.remove_note(slug, process_id, workspace_id=workspace_id,
                                    by=user.email or "import")
    return {"removed": process_id, "note_removed": removed}
