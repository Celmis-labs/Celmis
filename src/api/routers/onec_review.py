"""The web tab "Check a file": a report on an uploaded 1C file.

    POST /api/onec/review/files          upload a .erf/.epf/Form.bin (the signed-in
                                         user's session; no ticket) → a handle
    GET  /api/onec/review/configs        configuration dumps the caller may read
    GET  /api/onec/review/models         models to word the report (and the default)
    POST /api/onec/review                {handle, config, model, lang} → the report
    GET  /api/onec/review/{id}           the report again (kept 24 h, owner only)
    GET  /api/onec/review/{id}/export    ?format=pdf|docx|md

The report is a list of blocks (`src.docs.blocks`) rendered by the page, and by
the same renderers as the dependency audit's export. Files and reports belong to
the person who made them: another person's handle or id is a 404, a repository
they may not read is a 403.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, get_current_user
from src.onec import review as service
from src.onec import store
from src.onec.container import is_container
from src.onec.external import try_inspect
from src.users import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/onec/review", tags=["onec-review"])


class FileOut(BaseModel):
    handle: str
    file_name: str
    size: int
    sha256: str
    kind: str
    object_name: str
    synonym: str
    forms: int
    templates: int


class ConfigOut(BaseModel):
    slug: str
    name: str
    ref: str
    age: str
    upload: bool
    #: The owner workspace's name for a repository another workspace shares; "" for
    #: one of this workspace's own.
    shared_from: str = ""


class ModelOut(BaseModel):
    provider: str
    model: str


class ModelsOut(BaseModel):
    #: the surface "onec_check" is configured in /settings/llm
    default_available: bool
    default_provider: str = ""
    default_model: str = ""
    models: list[ModelOut]


class ReviewIn(BaseModel):
    handle: str = Field(min_length=1, max_length=40)
    config: str = Field(default="", max_length=300)
    #: "" / "none" (no model), "default", or "<provider>:<model>"
    model: str = Field(default="", max_length=200)
    lang: str = Field(default="uk", pattern="^(uk|en)$")


class ModelInfo(BaseModel):
    mode: str
    used: bool
    label: str = ""
    failure: str = ""


class ReviewOut(BaseModel):
    id: str
    file_name: str
    lang: str
    level: str
    config: str
    model: ModelInfo
    blocks: list[dict]
    created: str


def _out(payload: dict, rid: str) -> ReviewOut:
    return ReviewOut(id=rid, file_name=payload["file_name"], lang=payload["lang"],
                     level=payload["level"], config=payload.get("config", ""),
                     model=ModelInfo(**payload["model"]), blocks=payload["blocks"],
                     created=payload.get("created", ""))


@router.post("/files", response_model=FileOut, status_code=201)
async def upload_file(file: UploadFile = File(...), user: User = Depends(get_current_user),
                      workspace_id: str = Depends(current_workspace_id)) -> FileOut:
    data = await file.read(store.MAX_BYTES + 1)
    if len(data) > store.MAX_BYTES:
        raise HTTPException(413, f"the file is larger than {store.MAX_BYTES // (1024 * 1024)} MB")
    if not is_container(data):
        raise HTTPException(415, "not a 1C file (.erf, .epf or Form.bin)")
    ext, why = await asyncio.to_thread(try_inspect, data, file.filename or "upload")
    if ext is None:
        raise HTTPException(422, f"the file could not be read: {why}")
    try:
        saved = await asyncio.to_thread(store.add, str(user.id), workspace_id, data,
                                        file.filename or "upload")
    except store.StoreError as exc:
        raise HTTPException(413, str(exc)) from exc
    logger.info("onec_review_upload handle=%s bytes=%d by=%s", saved.handle, saved.size, user.email)
    syn = (ext.synonyms.get("ru") or ext.synonyms.get("uk") or ext.synonyms.get("en")
           or next(iter(ext.synonyms.values()), ""))
    return FileOut(handle=saved.handle, file_name=saved.file_name, size=saved.size,
                   sha256=saved.sha256, kind=ext.kind, object_name=ext.name, synonym=syn,
                   forms=len(ext.forms), templates=len(ext.templates))


@router.get("/configs", response_model=list[ConfigOut])
async def configs(q: str = Query(default="", max_length=100),
                  user: User = Depends(get_current_user),
                  workspace_id: str = Depends(current_workspace_id)) -> list[ConfigOut]:
    rows = await asyncio.to_thread(service.list_configs, str(user.id),
                                   bool(getattr(user, "is_admin", False)), workspace_id, q)
    return [ConfigOut(slug=r.slug, name=r.name, ref=r.ref, age=r.age, upload=r.upload,
                      shared_from=r.shared_from)
            for r in rows]


_MODELS: dict[tuple[str, str], tuple[float, list[str]]] = {}
_MODELS_LOCK = threading.Lock()
_PROVIDERS = ("google", "openai", "anthropic", "openrouter", "groq", "mistral", "litellm")
_MODEL_TTL = 300.0
_MAX_PER_PROVIDER = 80


def _provider_models(provider: str, user: User, workspace_id: str) -> list[str]:
    """Generation models of a provider the workspace has a key for; [] when none."""
    from src.api.routers.llm import provider_models
    from src.llm.profiles import get_provider_key

    key = (workspace_id, provider)
    now = time.time()
    with _MODELS_LOCK:
        hit = _MODELS.get(key)
        if hit and now - hit[0] < _MODEL_TTL:
            return hit[1]
    try:
        if provider != "litellm" and not get_provider_key(provider, workspace_id):
            names: list[str] = []
        else:
            names = list(provider_models(provider, user, workspace_id).generation)[:_MAX_PER_PROVIDER]
    except Exception as exc:  # noqa: BLE001 - one provider's failure hides only its models
        logger.info("onec_review_models_failed provider=%s err=%s", provider, type(exc).__name__)
        names = []
    with _MODELS_LOCK:
        _MODELS[key] = (now, names)
    return names


def _models_sync(user: User, workspace_id: str) -> ModelsOut:
    from src.llm.profiles import is_configured, resolve_profile

    available = is_configured("onec_check", workspace_id)
    prov = model = ""
    if available:
        p = resolve_profile("onec_check", workspace_id)
        prov, model = p.provider, p.model
    out: list[ModelOut] = []
    with ThreadPoolExecutor(max_workers=len(_PROVIDERS)) as pool:
        futures = {p: pool.submit(_provider_models, p, user, workspace_id) for p in _PROVIDERS}
        for p, fut in futures.items():
            try:
                out += [ModelOut(provider=p, model=m) for m in fut.result(timeout=20)]
            except Exception:  # noqa: BLE001
                continue
    return ModelsOut(default_available=available, default_provider=prov, default_model=model,
                     models=out)


@router.get("/models", response_model=ModelsOut)
async def models(user: User = Depends(get_current_user),
                 workspace_id: str = Depends(current_workspace_id)) -> ModelsOut:
    return await asyncio.to_thread(_models_sync, user, workspace_id)


@router.post("", response_model=ReviewOut, status_code=201)
async def create_review(body: ReviewIn, user: User = Depends(get_current_user),
                        workspace_id: str = Depends(current_workspace_id)) -> ReviewOut:
    try:
        payload = await asyncio.to_thread(
            service.run_review, user_id=str(user.id),
            is_admin=bool(getattr(user, "is_admin", False)), workspace_id=workspace_id,
            handle=body.handle, config_slug=body.config, model=body.model, lang=body.lang)
    except service.ReviewError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    logger.info("onec_review_done id=%s level=%s model=%s by=%s", payload["id"], payload["level"],
                payload["model"]["mode"], user.email)
    return _out(payload, payload["id"])


def _kept(rid: str, user: User, workspace_id: str) -> dict:
    payload = store.get_result(rid, str(user.id), workspace_id)
    if payload is None:
        raise HTTPException(404, "no such report of yours (unknown, expired after 24 h, or made "
                                 "by someone else)")
    return payload


@router.get("/{rid}", response_model=ReviewOut)
async def get_review(rid: str, user: User = Depends(get_current_user),
                     workspace_id: str = Depends(current_workspace_id)) -> ReviewOut:
    return _out(_kept(rid, user, workspace_id), rid)


@router.get("/{rid}/export")
async def export_review(rid: str, format: str = Query(default="pdf", pattern="^(md|docx|pdf)$"),
                        user: User = Depends(get_current_user),
                        workspace_id: str = Depends(current_workspace_id)) -> Response:
    from src.docs.blocks import blocks_from_data, to_markdown_text

    payload = _kept(rid, user, workspace_id)
    blocks = blocks_from_data(payload["blocks"])
    name, footer = payload.get("name", "celmis-1c-review"), payload.get("footer", "")
    pages = ("Стор.", "з") if payload.get("lang") == "uk" else ("Page", "of")
    if format == "docx":
        from src.docs.docx_blocks import blocks_to_docx

        return Response(
            content=await asyncio.to_thread(blocks_to_docx, blocks, footer=footer,
                                           page_words=pages),
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            headers={"Content-Disposition": f'attachment; filename="{name}.docx"'})
    if format == "pdf":
        from src.docs.pdf_blocks import blocks_to_pdf

        return Response(
            content=await asyncio.to_thread(blocks_to_pdf, blocks, footer=footer,
                                           page_words=pages),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
    return Response(content=to_markdown_text(blocks),
                    media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.md"'})


__all__ = ["router"]
