"""Vector-store configuration API (installation-level).

Vectors live in ONE shared collection for the whole install, so this is gated
on the global admin, not workspace admins.

    GET  /api/vector-store         — effective config (type, url, source)
    PUT  /api/vector-store         — save {type, url?, api_key?} (tests first)
    DELETE /api/vector-store       — drop the UI override (back to env/local)
    POST /api/vector-store/test    — ping a candidate config without saving
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import get_current_user, require_admin
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vector-store", tags=["vector-store"])


class StoreConfigOut(BaseModel):
    type: str
    url: str
    api_key_set: bool
    source: str                      # ui | env | default
    supported: list[str]
    planned: list[str]


class StoreConfigIn(BaseModel):
    type: str = Field(pattern="^(local|qdrant|pinecone|weaviate)$")
    url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    model_config = ConfigDict(extra="forbid")


class TestOut(BaseModel):
    ok: bool
    detail: str
    collections: list[str] = []


def _out() -> StoreConfigOut:
    from src.retrieval.vector_store import PLANNED_TYPES, SUPPORTED_TYPES, load_store_config
    cfg = load_store_config()
    return StoreConfigOut(
        type=cfg["type"], url=cfg["url"], api_key_set=cfg["api_key_set"],
        source=cfg["source"], supported=list(SUPPORTED_TYPES), planned=list(PLANNED_TYPES),
    )


@router.get("", response_model=StoreConfigOut)
def get_config(_user: User = Depends(get_current_user)) -> StoreConfigOut:
    return _out()


@router.post("/test", response_model=TestOut)
def test(payload: StoreConfigIn, _user: User = Depends(require_admin)) -> TestOut:
    from src.retrieval.vector_store import test_connection
    res = test_connection(type_=payload.type, url=payload.url, api_key=payload.api_key)
    return TestOut(ok=res["ok"], detail=res["detail"],
                   collections=res.get("collections", []))


@router.put("", response_model=StoreConfigOut)
def save(payload: StoreConfigIn, user: User = Depends(require_admin)) -> StoreConfigOut:
    from src.retrieval.vector_store import (
        PLANNED_TYPES,
        save_store_config,
        test_connection,
    )
    if payload.type in PLANNED_TYPES:
        raise HTTPException(
            status_code=501,
            detail=f"{payload.type} support is planned but not implemented yet — "
                   f"use 'local' or 'qdrant' (any URL, incl. Qdrant Cloud).",
        )
    res = test_connection(type_=payload.type, url=payload.url, api_key=payload.api_key)
    if not res["ok"]:
        raise HTTPException(status_code=400, detail=f"Connection test failed: {res['detail']}")
    save_store_config(type_=payload.type, url=payload.url,
                      api_key=payload.api_key, saved_by=user.email)
    logger.info("vector_store_saved type=%s by=%s", payload.type, user.email)
    return _out()


@router.delete("", status_code=204)
def clear(user: User = Depends(require_admin)) -> None:
    from src.retrieval.vector_store import clear_store_config
    clear_store_config()
    logger.info("vector_store_cleared by=%s", user.email)


__all__ = ["router"]
