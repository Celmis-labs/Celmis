"""Tier 1 — semantic search over the vault through Qdrant.

Vault MD files are embedded into Qdrant through the Gemini embedding API.
Search: embed(question) → vector search → relevant notes.

Tenancy
-------
One collection holds every tenant's notes, so `workspace_id` is a required
keyword on the constructor rather than a parameter with a "default": this
object IS a tenant's view of the vault. Everything it does inherits that —
`search` ANDs `self.scope.must_conditions()` into the query, `upsert_note` /
`upsert_notes_batch` stamp the tenant into the payload, and
`delete_by_note_path` can only reach the tenant's own points (plus
unattributed ones for the same note — see the method).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
)

from src.config import Settings, get_settings
from src.retrieval.vector_store import (
    VECTOR_WORKSPACE_KEY,
    VectorScope,
    stamp_workspace,
)

logger = logging.getLogger(__name__)


@dataclass
class VaultHit:
    """A single vault-search result."""

    note_path: str  # path relative to the vault (e.g. "modules/auth.md")
    score: float
    type: str  # module | feature | integration | overview | security
    module: str | None
    repo: str
    symbols: list[str]
    keywords: list[str]
    section: str | None
    content: str
    path: str | None = None  # for type=module — path relative to the repo (e.g. "src/auth")
    cross_refs: list[str] = field(default_factory=list)  # for type=feature/integration —
    # links to the members (e.g. ["modules/src-services-billing", ...])

    def as_dict(self) -> dict:
        return {
            "note_path": self.note_path,
            "score": round(self.score, 4),
            "type": self.type,
            "module": self.module,
            "path": self.path,
            "symbols": self.symbols,
            "keywords": self.keywords,
            "section": self.section,
            "content": self.content[:500] + "…" if len(self.content) > 500 else self.content,
        }


class VaultRetriever:
    """Semantic search over the vault. Requires the Qdrant collection to be
    already populated."""

    def __init__(
        self,
        settings: Settings | None = None,
        qdrant: QdrantClient | None = None,
        *,
        workspace_id: str | None,
    ) -> None:
        """`workspace_id` is keyword-only and has NO default on purpose.

        It used to default to "default", which meant every caller that had not
        thought about tenancy silently got one — and every one of them read and
        wrote the same shared slice of a collection holding three tenants. With
        no default, a caller that has not decided fails at the call site, where
        someone can see it, instead of at retrieval time, where nobody can.

        `None` is allowed and is not the same as absent: it is a caller that
        DID ask whose repo this is and got no answer (an unregistered slug).
        That retriever reads nothing and writes unattributed points.
        """
        self.settings = settings or get_settings()
        # Two jobs: the BYOK key slot for the embedding call, and — since the
        # collection is shared — which points this retriever may see at all.
        self.workspace_id = workspace_id
        self.scope = VectorScope.for_workspace(workspace_id)
        # No model client here on purpose: this tier reads the vault and
        # ranks it. One was constructed and never called, which is how a
        # gateway bypass shows up on an allow-list without anybody having
        # made a decision.
        self.qdrant = qdrant or _build_qdrant_client(self.settings)

    def search(
        self,
        question: str,
        *,
        repo: str | None = None,
        note_type: str | None = None,
        top_k: int | None = None,
    ) -> list[VaultHit]:
        """Semantic search. The filters are optional.

        Raises `CollectionMissing` when the vault has never been generated —
        BEFORE the embedding call, which is the point of checking. A search
        against a collection that does not exist used to embed the question
        first (a paid call), then ask Qdrant, then get a 404. Every search on
        the search page of a workspace with no vault was billed, and there is
        nothing a workspace in that state can do except keep pressing it.
        """
        top_k = top_k or self.settings.retrieval_vector_topk
        if not self.collection_exists():
            raise CollectionMissing(self.settings.qdrant_collection)
        from src.llm.completion import embed as _embed
        query_vec = _embed(
            question, operation="embed_query",
            workspace_id=self.workspace_id,
        )

        # ISOLATION. The tenant condition is not one of the "optional filters"
        # below it — it is always present, and `must` is AND, so `repo` /
        # `note_type` can only narrow the result further.
        must_conditions: list[FieldCondition] = list(self.scope.must_conditions())
        if repo:
            must_conditions.append(FieldCondition(key="repo", match=MatchValue(value=repo)))
        if note_type:
            must_conditions.append(FieldCondition(key="type", match=MatchValue(value=note_type)))

        query_filter = Filter(must=must_conditions) if must_conditions else None

        response = self.qdrant.query_points(
            collection_name=self.settings.qdrant_collection,
            query=query_vec,
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )

        hits: list[VaultHit] = []
        for point in response.points:
            if point.score < self.settings.retrieval_min_score:
                continue
            payload = point.payload or {}
            hits.append(
                VaultHit(
                    note_path=str(payload.get("note_path", "")),
                    score=float(point.score),
                    type=str(payload.get("type", "")),
                    module=payload.get("module"),
                    repo=str(payload.get("repo", "")),
                    symbols=list(payload.get("symbols", []) or []),
                    keywords=list(payload.get("keywords", []) or []),
                    section=payload.get("section"),
                    content=str(payload.get("content", "")),
                    path=payload.get("path"),
                    cross_refs=list(payload.get("cross_refs", []) or []),
                )
            )
        logger.info("vault_search q_hash=%s hits=%d", hash(question) & 0xFFFF, len(hits))
        return hits

    def collection_exists(self) -> bool:
        """Cheap: one metadata call, no vectors, no money.

        A Qdrant that is unreachable answers TRUE. "I could not ask" is not
        "it is not there", and returning False would turn a network blip into
        the message that tells the user to generate a vault they already have.
        The search below then fails on its own terms with the real error.
        """
        try:
            self.qdrant.get_collection(self.settings.qdrant_collection)
            return True
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "doesn't exist" in text or "Not found" in text or "404" in text:
                return False
            logger.warning("collection_probe_failed err=%s", text[:200])
            return True

    def ensure_collection(self, vector_size: int) -> bool:
        """Create the vault collection if it is not there. Returns True if it
        now exists.

        NOTHING CREATED IT. `qdrant_indexer` creates the code-CHUNK collection;
        the vault's own collection had no creator anywhere in the codebase, so
        on a deployment where it had never been made by hand every upsert died
        with

            404 Not Found: Collection `code_analysis_vault` doesn't exist!

        and `batched_qdrant` downgraded that to a warning. The result was a
        product where `readyz` reported `qdrant: {ok: true, collections: 0}`,
        every vault build wrote markdown and no vectors, semantic search
        answered `vault-not-generated` forever, and the chat banner asked the
        user to generate a vault they had generated three times.

        The width comes from the embedding just produced rather than from
        config: `embedding_dimensions` defaults to 0 ("whatever the server
        returns"), so config cannot answer, and the vectors in hand can. If a
        collection already exists at a different width, say so instead of
        writing points that can never be searched — the same refusal
        `qdrant_indexer` already makes for chunks.
        """
        from qdrant_client import models

        name = self.settings.qdrant_collection
        try:
            existing = self.qdrant.get_collection(name)
        except Exception:  # noqa: BLE001 — any failure means "not there yet"
            existing = None

        if existing is not None:
            current = _collection_width(existing)
            if current and current != vector_size:
                raise CollectionWidthMismatch(
                    f"vault collection {name!r} stores {current}-dimensional "
                    f"vectors and these embeddings are {vector_size}. Nothing "
                    "written now could ever be searched. Re-generate the vault "
                    "after re-creating the collection, or put the embeddings "
                    "model back to the one that built it."
                )
            return True

        try:
            self.qdrant.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=vector_size, distance=models.Distance.COSINE,
                ),
            )
            logger.info("vault_collection_created name=%s dims=%d", name, vector_size)
        except Exception as exc:  # noqa: BLE001
            # Another worker may have won the race; that is a success for us.
            try:
                self.qdrant.get_collection(name)
                logger.info("vault_collection_created_by_peer name=%s", name)
            except Exception:  # noqa: BLE001
                logger.warning("vault_collection_create_failed name=%s err=%s",
                               name, exc)
                return False

        # Every read filters on the tenant key, and an unindexed payload filter
        # in Qdrant is a full scan.
        try:
            self.qdrant.create_payload_index(
                collection_name=name,
                field_name=VECTOR_WORKSPACE_KEY,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("vault_payload_index_skipped err=%s", exc)
        return True

    def upsert_note(
        self,
        *,
        note_id: str,
        content: str,
        payload: dict[str, Any],
    ) -> None:
        """Add/update a single note in Qdrant. Used by the vault writer.

        The point is stamped with this retriever's workspace, so it is
        searchable by that tenant and nobody else."""
        from src.llm.completion import embed as _embed
        vector = _embed(
            content, task_type="RETRIEVAL_DOCUMENT", operation="embed_note", workspace_id=self.workspace_id,
            )
        from qdrant_client.models import PointStruct

        self.ensure_collection(len(vector))
        self.qdrant.upsert(
            collection_name=self.settings.qdrant_collection,
            points=[
                PointStruct(
                    id=note_id,
                    vector=vector,
                    payload=stamp_workspace(
                        {**payload, "content": content}, self.workspace_id,
                    ),
                )
            ],
        )

    def upsert_notes_batch(
        self,
        items: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """Batch upsert: [(note_id, content, payload), ...]."""
        if not items:
            return
        from src.llm.completion import embed_batch as _embed_batch
        vectors = _embed_batch(
            [content for _, content, _ in items],
            task_type="RETRIEVAL_DOCUMENT", operation="embed_notes_batch",
            workspace_id=self.workspace_id,
        )
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(
                id=note_id,
                vector=vector,
                payload=stamp_workspace(
                    {**payload, "content": content}, self.workspace_id,
                ),
            )
            for (note_id, content, payload), vector in zip(items, vectors, strict=True)
        ]
        if vectors:
            self.ensure_collection(len(vectors[0]))
        batch_size = self.settings.qdrant_upsert_batch_size
        for i in range(0, len(points), batch_size):
            self.qdrant.upsert(
                collection_name=self.settings.qdrant_collection,
                points=points[i : i + batch_size],
            )

    def delete_by_note_path(self, note_path: str, repo: str) -> None:
        """Delete all points for a single note (for example before an update).

        Scoped like a read, with one deliberate addition: `should` also matches
        points that carry NO tenant at all. Those are the pre-backfill points
        for this very (repo, note_path) — a stale copy of the note this call is
        about to replace. Refusing to delete them would leave the collection
        with two versions of one note, the old one permanently unreachable and
        permanently undeleted; and an unattributed point is by definition not
        some other tenant's, so removing it takes nothing away from anyone.

        A global scope contributes no conditions and matches everything, which
        is the same "the whole installation" it means on the read side.
        """
        must = [
            FieldCondition(key="note_path", match=MatchValue(value=note_path)),
            FieldCondition(key="repo", match=MatchValue(value=repo)),
        ]
        # must AND (should_1 OR should_2) — Qdrant's clause semantics.
        should = [
            *self.scope.must_conditions(),
            *([IsEmptyCondition(is_empty=PayloadField(key=VECTOR_WORKSPACE_KEY))]
              if not self.scope.global_ else []),
        ]
        self.qdrant.delete(
            collection_name=self.settings.qdrant_collection,
            points_selector=Filter(must=must, should=should or None),
        )


def _build_qdrant_client(settings: Settings) -> QdrantClient:
    from src.retrieval.vector_store import get_vector_client
    return get_vector_client()


class CollectionMissing(RuntimeError):
    """The vault collection has never been created in this workspace.

    Its own type rather than a string match on a Qdrant error, because two
    callers already matched on `"doesn't exist" in str(exc)` and a wording
    change in the client library would have turned "nothing indexed yet" back
    into "your search is broken".
    """

    def __init__(self, collection: str) -> None:
        super().__init__(
            f"vault collection {collection!r} does not exist — "
            f"nothing has been indexed for this workspace yet"
        )
        self.collection = collection


class CollectionWidthMismatch(RuntimeError):
    """The vault collection stores vectors of a different width.

    Raised rather than swallowed: points written at the wrong width are
    unsearchable forever, and a silent write is how a vault ends up full of
    notes nothing can find.
    """


def _collection_width(info: Any) -> int | None:
    """Vector width of an existing collection, or None if it cannot be read.

    The vault writes an UNNAMED vector, so the config is a bare `VectorParams`
    — but a collection made by another tool may carry a named map, and
    guessing wrong is worse than declining to check.
    """
    try:
        params = info.config.params.vectors
    except Exception:  # noqa: BLE001
        return None
    size = getattr(params, "size", None)
    if isinstance(size, int):
        return size
    if isinstance(params, dict):
        for value in params.values():
            inner = getattr(value, "size", None)
            if isinstance(inner, int):
                return inner
    return None
