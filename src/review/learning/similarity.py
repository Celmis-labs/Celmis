"""Is this finding "the same" as one a person judged before?

Three tiers, cheapest first. All of them compare a finding of THIS repository
(workspace + repo slug) with signals recorded on THIS repository:

  1. exact     the same fingerprint (rule | file | normalised title);
  2. titles    Jaccard >= 0.75 over the normalised title words, the same rule
               (or, with none, the same category) and the same directory — no
               embeddings needed;
  3. vectors   one `embed_batch` over the run's findings, then the top ten
               signals per finding from the Qdrant collection
               (`learning_collection`), cosine >= `learning_similarity`.
               Unavailable embeddings or a collection of another width
               degrade to tiers 1-2; they never fail a review.

Signals reach the collection lazily (`index_pending`): a signal is embedded the
first time a review on its repository needs tier 3, not when it is recorded, so
a workspace that never turns suppression on pays nothing.

Isolation: every point carries the workspace id (`stamp_workspace`) and every
query filters on it and on the repository slug.
"""

from __future__ import annotations

import logging
import math
import posixpath
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

TITLE_JACCARD = 0.75
#: Signals worth comparing with: the ones that say "wrong" or "right".
JUDGED = ("dismissed", "accepted", "implemented")
EMBED_BATCH = 64
INDEX_LIMIT = 300
TOP_K = 10
TEXT_BODY = 400


@dataclass(frozen=True)
class SignalView:
    """The fields of a stored signal that matching and scoring read."""

    id: str
    fingerprint: str
    file_path: str
    title: str
    rule_id: str | None
    category: str | None
    signal: str
    source: str
    weight: float
    reason: str
    actor: str
    pr_provider: str
    pr_repo: str
    pr_number: int
    created_at: datetime | None
    body: str = ""
    embedded: bool = False

    @property
    def pr_key(self) -> tuple[str, str, int, str]:
        """One person on one PR counts once, whatever they said about how many
        similar findings."""
        return (self.pr_provider, self.pr_repo, self.pr_number, self.actor)


def view_of(row: Any) -> SignalView:
    return SignalView(
        id=row.id, fingerprint=row.fingerprint, file_path=row.file_path or "",
        title=row.title or "", rule_id=row.rule_id, category=row.category,
        signal=row.signal, source=row.source, weight=float(row.weight or 0),
        reason=row.reason or "", actor=row.actor or "", pr_provider=row.pr_provider or "",
        pr_repo=row.pr_repo or "", pr_number=int(row.pr_number or 0),
        created_at=row.created_at, body=row.body or "", embedded=bool(row.embedded))


def title_tokens(title: str) -> frozenset[str]:
    from src.review.issues import normalize_title

    return frozenset(normalize_title(title).split())


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def directory_of(path: str) -> str:
    return posixpath.dirname((path or "").strip().strip("/"))


def load_signals(
    ws: str, repo_slug: str, since: datetime, *, session: Any = None,
) -> list[SignalView]:
    """The judged signals of one repository since `since`."""
    from sqlalchemy import select

    from src.db.models import FindingSignal as M
    from src.review.learning.signals import _session

    with _session(session) as s:
        rows = s.scalars(select(M).where(
            M.workspace_id == ws, M.repo_slug == repo_slug, M.signal.in_(JUDGED),
            M.created_at >= since).order_by(M.created_at.desc()).limit(5000)).all()
        return [view_of(r) for r in rows]


# ─── Tiers 1 and 2 ───────────────────────────────────────────────────


def match_exact(fingerprint: str, views: Iterable[SignalView]) -> list[SignalView]:
    return [v for v in views if v.fingerprint == fingerprint]


def match_titles(
    title: str, file_path: str, rule_id: str | None, category: str | None,
    views: Iterable[SignalView],
) -> list[SignalView]:
    """Signals about almost the same claim in the same directory."""
    words = title_tokens(title)
    here = directory_of(file_path)
    rule = (rule_id or "").strip().lower()
    out: list[SignalView] = []
    for v in views:
        if directory_of(v.file_path) != here:
            continue
        other_rule = (v.rule_id or "").strip().lower()
        same_kind = (rule and rule == other_rule) or (
            not rule and not other_rule and category and category == v.category)
        if not same_kind:
            continue
        if jaccard(words, title_tokens(v.title)) >= TITLE_JACCARD:
            out.append(v)
    return out


# ─── Tier 3: the vector collection ───────────────────────────────────


def signal_text(category: str | None, title: str, body: str, file_path: str) -> str:
    """What is embedded for a finding or a signal: the same four things, in the
    same order, on both sides."""
    return (f"{category or ''}\n{title}\n{' '.join((body or '').split())[:TEXT_BODY]}\n"
            f"{directory_of(file_path)}").strip()


def _collection_name() -> str:
    from src.review.settings import get_review_settings

    return get_review_settings().learning_collection


def _qdrant(client: Any = None) -> Any:
    if client is not None:
        return client
    from src.retrieval.vector_store import get_vector_client

    return get_vector_client()


def ensure_collection(client: Any, size: int, name: str | None = None) -> None:
    """Create the collection when it is missing (cosine, the width of the
    embeddings in hand, a keyword index on the tenant). A collection of another
    width raises `CollectionWidthMismatch` — points written at the wrong width
    could never be searched."""
    from qdrant_client import models

    from src.retrieval.tier1_vault import CollectionWidthMismatch, _collection_width
    from src.retrieval.vector_store import VECTOR_WORKSPACE_KEY

    name = name or _collection_name()
    try:
        existing = client.get_collection(name)
    except Exception:  # noqa: BLE001 — any failure means "not there yet"
        existing = None
    if existing is not None:
        current = _collection_width(existing)
        if current and current != size:
            raise CollectionWidthMismatch(
                f"collection {name!r} stores {current}-dimensional vectors and these "
                f"embeddings are {size}")
        return
    try:
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
    except Exception:  # noqa: BLE001 — another worker may have won the race
        client.get_collection(name)
        return
    for field in (VECTOR_WORKSPACE_KEY, "repo_slug"):
        try:
            client.create_payload_index(
                collection_name=name, field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD)
        except Exception as exc:  # noqa: BLE001
            logger.debug("learning_payload_index_skipped field=%s err=%s", field, exc)


def _embed(texts: list[str], ws: str) -> list[list[float]]:
    from src.llm.completion import embed_batch

    return embed_batch(texts, operation="learning_embed", workspace_id=ws)


def index_pending(
    ws: str, repo_slug: str, *, client: Any = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
    limit: int = INDEX_LIMIT, session: Any = None,
) -> int:
    """Embed the repository's judged signals that are not in the collection yet
    and upsert them. Returns how many were indexed. Raises on an embedding or
    collection failure (the caller degrades to tiers 1-2)."""
    from qdrant_client.models import PointStruct
    from sqlalchemy import select

    from src.db.models import FindingSignal as M
    from src.retrieval.vector_store import stamp_workspace
    from src.review.learning.signals import _session

    done = 0
    with _session(session) as s:
        rows = list(s.scalars(select(M).where(
            M.workspace_id == ws, M.repo_slug == repo_slug, M.signal.in_(JUDGED),
            M.embedded.is_(False)).order_by(M.created_at.desc()).limit(limit)).all())
        if not rows:
            return 0
        qdrant = _qdrant(client)
        for start in range(0, len(rows), EMBED_BATCH):
            chunk = rows[start:start + EMBED_BATCH]
            texts = [signal_text(r.category, r.title, r.body, r.file_path) for r in chunk]
            vectors = embed(texts) if embed is not None else _embed(texts, ws)
            if len(vectors) != len(chunk) or not vectors or not vectors[0]:
                raise RuntimeError("the embedder returned the wrong number of vectors")
            ensure_collection(qdrant, len(vectors[0]))
            qdrant.upsert(
                collection_name=_collection_name(),
                points=[PointStruct(
                    id=r.id, vector=list(vec),
                    payload=stamp_workspace({
                        "repo_slug": repo_slug, "signal": r.signal,
                        "fingerprint": r.fingerprint,
                        "ts": r.created_at.timestamp() if r.created_at else 0,
                    }, ws)) for r, vec in zip(chunk, vectors, strict=True)])
            for r in chunk:
                r.embedded = True
            s.commit()
            done += len(chunk)
    return done


def query_similar(
    ws: str, repo_slug: str, texts: list[str], *, threshold: float, client: Any = None,
    embed: Callable[[list[str]], list[list[float]]] | None = None, top_k: int = TOP_K,
) -> list[list[tuple[str, float]]]:
    """For each text, the (signal id, cosine) pairs at or above `threshold`
    within this workspace and repository. Raises on failure."""
    from qdrant_client import models

    from src.retrieval.vector_store import VectorScope

    if not texts:
        return []
    vectors = embed(texts) if embed is not None else _embed(texts, ws)
    if len(vectors) != len(texts):
        raise RuntimeError("the embedder returned the wrong number of vectors")
    qdrant = _qdrant(client)
    must = [*VectorScope.for_workspace(ws).must_conditions(),
            models.FieldCondition(key="repo_slug", match=models.MatchValue(value=repo_slug))]
    out: list[list[tuple[str, float]]] = []
    for vec in vectors:
        response = qdrant.query_points(
            collection_name=_collection_name(), query=list(vec),
            query_filter=models.Filter(must=must), limit=top_k, with_payload=False)
        hits = [(str(p.id), float(p.score)) for p in response.points
                if float(p.score) >= threshold and not math.isnan(float(p.score))]
        out.append(hits)
    return out


def delete_vectors(ids: Iterable[str], *, client: Any = None) -> None:
    """Remove signal vectors by id. Raises on a store failure."""
    from qdrant_client import models

    listed = [str(i) for i in ids]
    if not listed:
        return
    _qdrant(client).delete(
        collection_name=_collection_name(),
        points_selector=models.PointIdsList(points=listed))


def delete_repo_vectors(ws: str, repo_slug: str, *, client: Any = None) -> None:
    """Remove every vector of one repository of one workspace. Best-effort: a
    missing collection is nothing to delete."""
    from qdrant_client import models

    from src.retrieval.vector_store import VectorScope

    must = [*VectorScope.for_workspace(ws).must_conditions(),
            models.FieldCondition(key="repo_slug", match=models.MatchValue(value=repo_slug))]
    try:
        _qdrant(client).delete(
            collection_name=_collection_name(),
            points_selector=models.FilterSelector(filter=models.Filter(must=must)))
    except Exception as exc:  # noqa: BLE001
        logger.info("learning_vectors_purge_skipped err_type=%s", type(exc).__name__)
