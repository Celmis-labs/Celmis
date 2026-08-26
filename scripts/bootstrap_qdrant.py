"""Створює Qdrant колекції для vault notes та symbols.

Запуск:
    python -m scripts.bootstrap_qdrant
    python -m scripts.bootstrap_qdrant --recreate

Або через CLI:
    analyzer bootstrap-qdrant [--recreate]
"""

from __future__ import annotations

import logging
import sys

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PayloadSchemaType,
    VectorParams,
)
from rich.console import Console

from src.config import get_settings

logger = logging.getLogger(__name__)
console = Console()


def bootstrap(recreate: bool = False) -> None:
    """Ідемпотентно створює/перестворює Qdrant колекції."""
    settings = get_settings()
    from src.retrieval.vector_store import get_vector_client, load_store_config
    client = get_vector_client()

    console.print(f"[cyan]→ Vector store:[/cyan] {load_store_config()['type']} {settings.qdrant_url or '(embedded local)'}")
    # The width of the CONFIGURED embedder, not Gemini's. This script is
    # what creates the vault collection, so on an install pointing embeddings
    # at a local 768-wide model it used to build a 3072-wide collection that
    # rejects every vector the install can produce — discovered after a full
    # index run, which is the most expensive moment to discover it.
    from src.llm.completion import embedding_dimensions
    dims = embedding_dimensions()
    model = settings.embedding_model or settings.gemini_embedding_model
    console.print(f"[cyan]→ Model:[/cyan] {model}")
    console.print(f"[cyan]→ Dimensions:[/cyan] {dims}")
    console.print(f"[cyan]→ Collection:[/cyan] {settings.qdrant_collection}\n")

    _ensure_collection(
        client,
        name=settings.qdrant_collection,
        dim=dims,
        recreate=recreate,
    )
    _create_payload_indexes(client, settings.qdrant_collection)

    console.print("\n[green]✅ Qdrant bootstrap complete[/green]")


def _ensure_collection(
    client: QdrantClient,
    *,
    name: str,
    dim: int,
    recreate: bool,
) -> None:
    existing = False
    try:
        client.get_collection(name)
        existing = True
    except (UnexpectedResponse, ValueError):
        pass
    except Exception as exc:  # noqa: BLE001
        # qdrant_client у 1.14+ кидає custom; лишаємо загальний catch
        logger.debug("get_collection check: %s", exc)

    if existing and recreate:
        console.print(f"[yellow]Deleting existing collection '{name}'...[/yellow]")
        client.delete_collection(name)
        existing = False

    if existing:
        console.print(f"[dim]Collection '{name}' already exists — skipping create[/dim]")
        return

    console.print(f"Creating collection '{name}' ({dim} dim, Cosine)...")
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(
            size=dim,
            distance=Distance.COSINE,
            on_disk=False,  # для швидшого пошуку на малому масштабі
        ),
        hnsw_config=HnswConfigDiff(
            m=16,
            ef_construct=100,
        ),
        optimizers_config=OptimizersConfigDiff(
            default_segment_number=2,
        ),
    )
    console.print(f"[green]  ✓ Collection '{name}' created[/green]")


def _create_payload_indexes(client: QdrantClient, collection: str) -> None:
    """Payload indexes для швидкої фільтрації по repo/type/module."""
    indexes: list[tuple[str, PayloadSchemaType]] = [
        # Every query filters on the tenant (src/retrieval/vector_store.py),
        # and an unindexed payload filter in Qdrant is a full scan.
        ("workspace_id", PayloadSchemaType.KEYWORD),
        ("repo", PayloadSchemaType.KEYWORD),
        ("type", PayloadSchemaType.KEYWORD),
        ("module", PayloadSchemaType.KEYWORD),
        ("feature", PayloadSchemaType.KEYWORD),
        ("note_path", PayloadSchemaType.KEYWORD),
    ]
    for field_name, schema in indexes:
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field_name,
                field_schema=schema,
            )
            console.print(f"  [dim]index:[/dim] {field_name}")
        except Exception as exc:  # noqa: BLE001
            # Якщо вже існує — ок
            logger.debug("payload_index %s: %s", field_name, exc)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Bootstrap Qdrant collections")
    parser.add_argument("--recreate", action="store_true", help="Drop existing collection")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        bootstrap(recreate=args.recreate)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Error:[/red] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
