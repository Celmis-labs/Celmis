"""Semantic search inside knowledge packs - the vector half of the hybrid search.

BM25 (``src.knowledge.search``) stays the base: it needs no index and no
embeddings. When the install has embeddings, the CURRENT version of every pack
is also embedded, chunk by chunk, into a Qdrant collection of its own
(``Settings.qdrant_pack_collection``), and a query fuses the two rankings by
reciprocal rank (``store._rank_core``).

Tenancy. Every point carries ``workspace_id`` (``stamp_workspace``) and a read
filters on it AND on the ids of the current versions of the packs the caller may
see; the store then re-checks each hit against those packs, like BM25. Nothing
here decides who may see what.

What is indexed. ``knowledge_pack_versions.indexed_signature`` records the
embeddings signature a version was indexed under. :func:`sync_pack` makes the
index of one pack right: the current version indexed under the current
signature, the points of every other version (old, pruned, archived) and of a
deleted pack gone. It is called after the commit of an upload, an activation or
a delete, and by :func:`backfill` for packs that existed before this feature or
were indexed under another embeddings model.

Never an error. With no Qdrant, no embeddings, a failing embedder or an empty
index every function here degrades to "nothing found" / "nothing done"; a query
embeds only when at least one visible current version is indexed (no billing for
a search that cannot use the answer).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.db.models import KnowledgeItem, KnowledgePack, KnowledgePackVersion
from src.retrieval.vector_store import VectorScope, stamp_workspace

logger = logging.getLogger(__name__)

#: Characters of one chunk (a section is cut near this) and the hard bounds per item.
CHUNK_CHARS = 1200
MAX_CHUNKS_PER_ITEM = 40
#: Texts embedded per call, points written per call.
EMBED_BATCH = 64
UPSERT_BATCH = 128
#: Vector hits asked of Qdrant for a query, and the floor of a hit's cosine score.
VECTOR_TOP_K = 60
MIN_SCORE = 0.0
#: Chunk text kept in the payload, for the snippet of a hit found only by meaning.
PAYLOAD_TEXT = 1500

_NAMESPACE = uuid.UUID("6f1c3a52-9d0e-4b7a-8c11-2f5d7a9e4b03")
_HEADING = re.compile(r"^(#{1,4})[ \t]+\S", re.MULTILINE)
_FRONT = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.DOTALL)


# ─── switches ────────────────────────────────────────────────────────


def enabled() -> bool:
    """On unless ``CELMIS_PACK_SEMANTIC=0``."""
    return os.environ.get("CELMIS_PACK_SEMANTIC", "1").strip().lower() not in ("0", "false", "no", "off")


def collection() -> str:
    from src.config import get_settings

    return get_settings().qdrant_pack_collection


def signature() -> str | None:
    """The embeddings fingerprint, or None when it cannot be told."""
    try:
        from src.llm import profiles

        return profiles.embeddings_signature()
    except Exception as exc:  # noqa: BLE001 - embeddings not configured
        logger.debug("pack_semantic_no_signature err=%s", type(exc).__name__)
        return None


def _client() -> Any:
    from src.retrieval import tier1_vault

    return tier1_vault._build_qdrant_client(None)  # noqa: SLF001 - the one factory the tests patch


# ─── chunking ────────────────────────────────────────────────────────


def chunks_of(name: str, description: str, content: str) -> list[str]:
    """Texts to embed for one item: its sections (by markdown heading), long ones cut at
    paragraph or line breaks near :data:`CHUNK_CHARS`. Every chunk starts with the item's
    name so that a chunk keeps its subject."""
    body = _FRONT.sub("", content or "", count=1).strip()
    head = (name or "").strip()
    parts: list[str] = []
    marks = [m.start() for m in _HEADING.finditer(body)]
    if not marks or marks[0] != 0:
        marks.insert(0, 0)
    marks.append(len(body))
    for lo, hi in zip(marks, marks[1:], strict=False):
        section = body[lo:hi].strip()
        while len(section) > CHUNK_CHARS:
            cut = max(section.rfind("\n\n", 0, CHUNK_CHARS), section.rfind("\n", 0, CHUNK_CHARS),
                      section.rfind(". ", 0, CHUNK_CHARS))
            cut = cut if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
            parts.append(section[:cut].strip())
            section = section[cut:].strip()
        if section:
            parts.append(section)
    merged: list[str] = []                       # tiny neighbours share a chunk
    for part in parts:
        if merged and len(merged[-1]) + len(part) < CHUNK_CHARS // 3:
            merged[-1] += "\n\n" + part
        else:
            merged.append(part)
    if not merged:
        merged = [(description or "").strip()] if (description or "").strip() else []
    elif description:
        merged[0] = f"{description.strip()}\n\n{merged[0]}"
    out = [f"{head}\n{p}" if head else p for p in merged[:MAX_CHUNKS_PER_ITEM]]
    return [t[:PAYLOAD_TEXT * 2] for t in out]


def point_id(item_id: str, n: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{item_id}:{n}"))


# ─── the collection ──────────────────────────────────────────────────


def _ensure_collection(client: Any, size: int) -> bool:
    from qdrant_client import models

    name = collection()
    try:
        info = client.get_collection(name)
    except Exception:  # noqa: BLE001 - not there yet
        info = None
    if info is not None:
        from src.retrieval.tier1_vault import _collection_width

        width = _collection_width(info)
        if width and width != size:
            logger.warning("pack_semantic_width_mismatch have=%s need=%s", width, size)
            return False
        return True
    try:
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
    except Exception as exc:  # noqa: BLE001 - a peer may have won the race
        try:
            client.get_collection(name)
        except Exception:  # noqa: BLE001
            logger.warning("pack_semantic_create_failed err=%s", type(exc).__name__)
            return False
    for key in ("workspace_id", "pack_id", "version_id"):
        try:
            client.create_payload_index(
                collection_name=name, field_name=key,
                field_schema=models.PayloadSchemaType.KEYWORD)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pack_semantic_payload_index_skipped err=%s", type(exc).__name__)
    return True


def _collection_exists(client: Any) -> bool:
    try:
        return bool(client.collection_exists(collection()))
    except Exception:  # noqa: BLE001
        return False


def _delete(client: Any, must: list[Any], must_not: list[Any] | None = None) -> None:
    from qdrant_client import models

    if not _collection_exists(client):
        return
    try:
        client.delete(
            collection_name=collection(),
            points_selector=models.FilterSelector(
                filter=models.Filter(must=must, must_not=must_not or None)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("pack_semantic_delete_failed err=%s", type(exc).__name__)


def _match(key: str, value: str) -> Any:
    from qdrant_client import models

    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


# ─── writing ─────────────────────────────────────────────────────────


@dataclass
class Indexed:
    chunks: int = 0
    versions: int = 0


def index_version(s: Session, pack: KnowledgePack, version: KnowledgePackVersion,
                  sig: str) -> int | None:
    """Embed the items of ``version`` (the pack's current one) and write their points.
    Returns the chunk count, or None when it could not be done (nothing is marked)."""
    from qdrant_client import models

    from src.llm import completion

    rows = s.execute(select(
        KnowledgeItem.id, KnowledgeItem.path, KnowledgeItem.kind, KnowledgeItem.name,
        KnowledgeItem.description, KnowledgeItem.content,
    ).where(KnowledgeItem.version_id == version.id)).all()
    pending: list[tuple[Any, int, str]] = []
    for row in rows:
        for n, text in enumerate(chunks_of(row.name, row.description, row.content)):
            pending.append((row, n, text))
    client = _client()
    try:
        vectors: list[list[float]] = []
        for i in range(0, len(pending), EMBED_BATCH):
            batch = [t for _r, _n, t in pending[i:i + EMBED_BATCH]]
            vectors.extend(completion.embed_batch(
                batch, operation="embed_pack_items", workspace_id=pack.workspace_id))
        if len(vectors) != len(pending):
            return None
        if vectors and not _ensure_collection(client, len(vectors[0])):
            return None
        points = [models.PointStruct(
            id=point_id(row.id, n), vector=vec,
            payload=stamp_workspace({
                "pack_id": pack.id, "version_id": version.id, "item_id": row.id,
                "path": row.path, "kind": row.kind, "chunk": n, "text": text[:PAYLOAD_TEXT],
            }, pack.workspace_id))
            for (row, n, text), vec in zip(pending, vectors, strict=True)]
        if points:
            for i in range(0, len(points), UPSERT_BATCH):
                client.upsert(collection_name=collection(), points=points[i:i + UPSERT_BATCH])
    except Exception as exc:  # noqa: BLE001 - BM25 still answers
        logger.warning("pack_semantic_index_failed err=%s", type(exc).__name__)
        return None
    version.indexed_signature = sig
    s.commit()
    return len(points)


def sync_pack(s: Session, pack_id: str) -> Indexed:
    """Make the index of one pack right (see the module text). Safe to repeat."""
    done = Indexed()
    if not enabled():
        return done
    try:
        client = _client()
    except Exception as exc:  # noqa: BLE001 - no vector store
        logger.debug("pack_semantic_no_client err=%s", type(exc).__name__)
        return done
    pack = s.get(KnowledgePack, pack_id)
    if pack is None or not pack.current_version_id:
        _delete(client, [_match("pack_id", pack_id)])
        return done
    keep = pack.current_version_id
    version = s.get(KnowledgePackVersion, keep)
    sig = signature()
    if version is None or sig is None:
        return done
    if version.indexed_signature != sig:
        count = index_version(s, pack, version, sig)
        if count is None:
            return done
        done = Indexed(count, 1)
    from qdrant_client import models

    _delete(client, [_match("pack_id", pack_id)],
            [models.FieldCondition(key="version_id", match=models.MatchValue(value=keep))])
    s.execute(update(KnowledgePackVersion).where(
        KnowledgePackVersion.pack_id == pack_id, KnowledgePackVersion.id != keep,
        KnowledgePackVersion.indexed_signature.is_not(None)).values(indexed_signature=None))
    s.commit()
    return done


def _run(pack_ids: Sequence[str]) -> None:
    try:
        from src.access.resolver import _sync_engine

        with Session(_sync_engine()) as s:
            for pack_id in pack_ids:
                sync_pack(s, pack_id)
    except Exception as exc:  # noqa: BLE001 - never block or break the caller
        logger.warning("pack_semantic_sync_failed err=%s", type(exc).__name__)


def _spawn(fn: Callable[[], None]) -> None:
    """Run ``fn`` in the background. Tests replace this with a direct call."""
    threading.Thread(target=fn, name="pack-semantic-index", daemon=True).start()


def schedule(pack_ids: Iterable[str]) -> None:
    """After a commit: bring the index of these packs right, in the background."""
    ids = [p for p in dict.fromkeys(pack_ids) if p]
    if ids and enabled():
        _spawn(lambda: _run(ids))


def backfill(workspace_id: str | None = None, *, s: Session | None = None) -> Indexed:
    """Index every pack whose current version is not indexed under the current embeddings
    signature (optionally one workspace's). Returns the totals; a failure stops quietly."""
    total = Indexed()
    if not enabled() or signature() is None:
        return total
    try:
        if s is None:
            from src.access.resolver import _sync_engine

            with Session(_sync_engine()) as own:
                return backfill(workspace_id, s=own)
        sig = signature()
        q = (select(KnowledgePack.id).join(
            KnowledgePackVersion, KnowledgePackVersion.id == KnowledgePack.current_version_id)
            .where(KnowledgePack.current_version_id.is_not(None)))
        if workspace_id:
            q = q.where(KnowledgePack.workspace_id == workspace_id)
        q = q.where(KnowledgePackVersion.indexed_signature.is_(None)
                    | (KnowledgePackVersion.indexed_signature != sig))
        for pack_id in list(s.scalars(q)):
            done = sync_pack(s, pack_id)
            total.chunks += done.chunks
            total.versions += done.versions
    except Exception as exc:  # noqa: BLE001
        logger.warning("pack_semantic_backfill_failed err=%s", type(exc).__name__)
    return total


def start_backfill() -> None:
    """At startup: the backfill, in the background, unless switched off."""
    if enabled() and os.environ.get("CELMIS_PACK_SEMANTIC_BACKFILL", "1").strip() != "0":
        _spawn(lambda: backfill())


# ─── reading ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VectorHit:
    item_id: str
    version_id: str
    score: float
    text: str


def indexed_versions(s: Session, version_ids: Sequence[str]) -> list[str]:
    """Of these (current) version ids, those indexed under today's signature."""
    sig = signature()
    if sig is None or not version_ids:
        return []
    return list(s.scalars(select(KnowledgePackVersion.id).where(
        KnowledgePackVersion.id.in_(list(version_ids)),
        KnowledgePackVersion.indexed_signature == sig)))


def search(s: Session, ws_id: str, query: str, version_ids: Sequence[str], *,
           top_k: int = VECTOR_TOP_K) -> list[VectorHit]:
    """The best chunks for ``query`` among the points of ``version_ids`` (current versions
    of packs the caller may see) in workspace ``ws_id``: one hit per item, best first.
    ``[]`` whenever the vector side cannot answer. Embeds the query only when at least one
    of the versions is indexed."""
    if not enabled() or not (query or "").strip():
        return []
    try:
        ids = indexed_versions(s, version_ids)
        if not ids:
            return []
        scope = VectorScope.for_workspace(ws_id)
        if scope.matches_nothing:
            return []
        client = _client()
        if not _collection_exists(client):
            return []
        from qdrant_client import models

        from src.llm import completion

        vector = completion.embed(query, operation="embed_pack_query", workspace_id=ws_id)
        response = client.query_points(
            collection_name=collection(), query=vector,
            query_filter=models.Filter(must=[
                *scope.must_conditions(),
                models.FieldCondition(key="version_id", match=models.MatchAny(any=ids))]),
            limit=top_k * 3, with_payload=True)
    except Exception as exc:  # noqa: BLE001 - BM25 only
        logger.warning("pack_semantic_query_failed err=%s", type(exc).__name__)
        return []
    best: dict[str, VectorHit] = {}
    allowed = set(ids)
    for point in response.points:
        payload = point.payload or {}
        item_id, version_id = str(payload.get("item_id") or ""), str(payload.get("version_id") or "")
        if (not item_id or version_id not in allowed or not scope.allows(payload)
                or float(point.score) <= MIN_SCORE or item_id in best):
            continue
        best[item_id] = VectorHit(item_id, version_id, float(point.score),
                                  str(payload.get("text") or ""))
        if len(best) >= top_k:
            break
    return list(best.values())


__all__ = [
    "Indexed", "VectorHit", "backfill", "chunks_of", "collection", "enabled", "index_version",
    "indexed_versions", "point_id", "schedule", "search", "signature", "start_backfill",
    "sync_pack",
]
