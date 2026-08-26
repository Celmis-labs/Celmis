"""Give the already-indexed Qdrant points an owner.

    python -m scripts.backfill_vector_workspaces              # dry run
    python -m scripts.backfill_vector_workspaces --apply      # write it
    python -m scripts.backfill_vector_workspaces --workspace ws-abc --apply
    python -m scripts.backfill_vector_workspaces --collection code_analysis_vault

WHY THIS EXISTS
---------------
Points written before `workspace_id` existed carry no tenant. Search now ANDs
`workspace_id == <caller's>` into every query, and an equality condition never
matches a missing key — so those points are ALREADY invisible to every tenant
and visible only to a global admin. Nothing is leaking while this script has
not been run, and nothing has been deleted either. They are simply orphaned.

This gives them back. Not by guessing: the repo → workspace binding is real
data, recorded when somebody registered the repository (auto_review_config), and
that is the only mapping used here. A point whose repo is registered NOWHERE, or
registered in two workspaces at once, has no honest owner — it is reported and
left exactly as it is, which keeps it admin-only. Silently handing it to the
biggest workspace, or to the seeded 'default' one, would be the one outcome
worse than leaving it orphaned.

WHAT IT WILL AND WILL NOT DO
----------------------------
  * Only touches points where `workspace_id` is absent. Points that already
    have one are never rewritten, so re-running is a no-op and interrupting it
    halfway is safe. That is the whole of its idempotency.
  * Never deletes a point.
  * Dry run is the DEFAULT. Nothing is written without `--apply`.

READING THE OUTPUT
------------------
Per collection you get one line per repo — how many orphaned points it has and
which workspace they would go to — then a summary of what could not be mapped
and why. `unregistered` and `ambiguous` are counts of points that stay
admin-only; if either is large, the fix is upstream (register the repo, or
resolve the double binding) and then re-run this.

`no_repo_key` means the points carry no `repo` in their payload at all, so
there is nothing to map them by. The symbols collection is like this by
construction — `CodeChunk` records a file path, not a repository — so an
orphaned symbols point cannot be attributed by this script at any effort. It
stays admin-only until that repository is re-indexed, which stamps the tenant
at write time and is the cheaper fix.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, IsEmptyCondition, PayloadField
from rich.console import Console
from rich.table import Table

from src.retrieval.vector_store import VECTOR_WORKSPACE_KEY
from src.security.audit import normalize_workspace_id

logger = logging.getLogger(__name__)
console = Console()

_SCROLL_PAGE = 512
_SET_PAYLOAD_BATCH = 512

# Why a repo's points could not be given an owner. Each is reported, none is
# guessed around.
UNREGISTERED = "unregistered"   # slug is in no workspace's repo list
AMBIGUOUS = "ambiguous"         # slug is registered in two workspaces at once
PLACEHOLDER = "placeholder"     # registered to the literal 'default' — see below
NO_REPO_KEY = "no_repo_key"     # payload has no `repo` to map by


def repo_to_workspace() -> tuple[dict[str, str], dict[str, str]]:
    """(slug → workspace_id, slug → why-not) from the auto-review registry.

    A slug bound to two workspaces is deliberately NOT resolved by picking one.
    Same rule as `AutoReviewStore.workspace_for_repo` uses for webhook routing:
    an ambiguous repo fails closed.

    A slug registered to the literal 'default' is reported as a placeholder
    rather than stamped. 'default' is what `workspace_id` holds when nobody
    ever said which tenant a row belonged to — writing it into a point would
    hand that repository to whoever happens to own the seeded 'default'
    workspace, which is exactly the mistake this whole change is undoing.
    """
    from src.api.auto_review import get_auto_review_store

    bindings: dict[str, set[str]] = defaultdict(set)
    for cfg in get_auto_review_store().list_all():
        bindings[cfg.repo_slug].add(cfg.workspace_id)

    mapping: dict[str, str] = {}
    unmappable: dict[str, str] = {}
    for slug, workspaces in bindings.items():
        if len(workspaces) > 1:
            unmappable[slug] = AMBIGUOUS
            continue
        ws = normalize_workspace_id(next(iter(workspaces)))
        if ws is None:
            unmappable[slug] = PLACEHOLDER
        else:
            mapping[slug] = ws
    return mapping, unmappable


def _orphan_points(client: QdrantClient, collection: str) -> list[tuple[object, str]]:
    """Every point with no tenant, as (point_id, repo_slug_or_empty).

    `IsEmptyCondition` is the only selector used: it is what "was written
    before this field existed" looks like, and it is what makes a second run
    find nothing.
    """
    out: list[tuple[object, str]] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=Filter(must=[
                IsEmptyCondition(is_empty=PayloadField(key=VECTOR_WORKSPACE_KEY)),
            ]),
            limit=_SCROLL_PAGE,
            offset=offset,
            with_payload=["repo"],
            with_vectors=False,
        )
        for p in points:
            out.append((p.id, str((p.payload or {}).get("repo", "") or "")))
        if offset is None:
            break
    return out


def backfill_collection(
    client: QdrantClient,
    collection: str,
    *,
    mapping: dict[str, str],
    unmappable: dict[str, str],
    only_workspace: str | None,
    apply: bool,
) -> dict[str, int]:
    console.print(f"\n[bold cyan]── {collection}[/bold cyan]")
    try:
        client.get_collection(collection)
    except Exception as exc:  # noqa: BLE001
        console.print(f"  [dim]absent or unreadable — skipped ({type(exc).__name__}: {exc})[/dim]")
        return {}

    orphans = _orphan_points(client, collection)
    if not orphans:
        console.print("  [green]nothing to do — every point already has a workspace[/green]")
        return {}

    by_workspace: dict[str, list[object]] = defaultdict(list)
    skipped: Counter[str] = Counter()
    skipped_slugs: dict[str, set[str]] = defaultdict(set)
    per_repo: Counter[tuple[str, str]] = Counter()

    for point_id, slug in orphans:
        if not slug:
            skipped[NO_REPO_KEY] += 1
            continue
        ws = mapping.get(slug)
        if ws is None:
            reason = unmappable.get(slug, UNREGISTERED)
            skipped[reason] += 1
            skipped_slugs[reason].add(slug)
            continue
        if only_workspace and ws != only_workspace:
            continue
        by_workspace[ws].append(point_id)
        per_repo[(slug, ws)] += 1

    if per_repo:
        table = Table(box=None, pad_edge=False)
        table.add_column("repo", style="white")
        table.add_column("points", justify="right", style="cyan")
        table.add_column("→ workspace", style="green")
        for (slug, ws), n in sorted(per_repo.items(), key=lambda kv: -kv[1]):
            table.add_row(slug, str(n), ws)
        console.print(table)

    total = sum(len(v) for v in by_workspace.values())
    console.print(
        f"  [bold]{total}[/bold] orphaned point(s) can be attributed"
        f" across {len(by_workspace)} workspace(s)"
    )
    for reason, n in sorted(skipped.items()):
        slugs = sorted(skipped_slugs.get(reason, ()))
        detail = f" — {', '.join(slugs[:6])}{'…' if len(slugs) > 6 else ''}" if slugs else ""
        console.print(
            f"  [yellow]{n}[/yellow] point(s) stay unattributed ({reason}){detail}"
        )

    if not apply:
        console.print("  [dim]dry run — nothing written. Re-run with --apply.[/dim]")
        return {ws: len(ids) for ws, ids in by_workspace.items()}

    for ws, ids in by_workspace.items():
        for i in range(0, len(ids), _SET_PAYLOAD_BATCH):
            batch = ids[i:i + _SET_PAYLOAD_BATCH]
            client.set_payload(
                collection_name=collection,
                payload={VECTOR_WORKSPACE_KEY: ws},
                points=batch,
                wait=True,
            )
        console.print(f"  [green]✓[/green] {len(ids)} point(s) → {ws}")
        logger.info("vector_backfill collection=%s workspace=%s points=%d",
                    collection, ws, len(ids))
    return {ws: len(ids) for ws, ids in by_workspace.items()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stamp workspace_id onto Qdrant points written before it existed.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write. Without it this is a dry run and touches nothing.",
    )
    parser.add_argument(
        "--workspace", default=None,
        help="restrict to one workspace_id — useful for doing three tenants "
             "one at a time and checking each before moving on.",
    )
    parser.add_argument(
        "--collection", action="append", default=None,
        help="collection to process (repeatable). Default: the vault and "
             "symbols collections from settings.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from src.config import get_settings
    from src.retrieval.vector_store import get_vector_client, load_store_config

    settings = get_settings()
    cfg = load_store_config()
    client = get_vector_client()

    collections = args.collection or [
        settings.qdrant_collection,
        settings.qdrant_symbols_collection,
    ]

    console.print(
        f"[cyan]vector store:[/cyan] {cfg['type']} "
        f"{cfg['url'] or '(embedded local)'}"
    )
    console.print(
        "[bold red]APPLY — points will be written[/bold red]" if args.apply
        else "[bold yellow]DRY RUN — nothing will be written[/bold yellow]"
    )

    mapping, unmappable = repo_to_workspace()
    console.print(
        f"[cyan]repo → workspace:[/cyan] {len(mapping)} repo(s) mapped, "
        f"{len(unmappable)} without an honest owner"
    )
    if not mapping:
        console.print(
            "[yellow]No repository is registered to a real workspace — there is "
            "nothing to attribute anything to. Every orphaned point stays "
            "admin-only, which is the correct outcome, not a failure.[/yellow]"
        )

    grand: Counter[str] = Counter()
    for name in collections:
        for ws, n in backfill_collection(
            client, name,
            mapping=mapping, unmappable=unmappable,
            only_workspace=args.workspace, apply=args.apply,
        ).items():
            grand[ws] += n

    console.print("\n[bold]Total[/bold]")
    if not grand:
        console.print("  nothing to attribute")
    for ws, n in sorted(grand.items(), key=lambda kv: -kv[1]):
        console.print(f"  {ws}: {n} point(s)"
                      f"{'' if args.apply else ' (would be)'}")
    if not args.apply and grand:
        console.print("\n[dim]Re-run with --apply to write.[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
