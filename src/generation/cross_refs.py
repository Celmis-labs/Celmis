"""CrossRefsComputer — computes the computed block of vault note frontmatter.

For every module (= rel directory in the repo) it runs a graph traversal
over CALLS+IMPORTS, groups the reached symbols by their module, and returns
the top-N most connected modules as `cross_refs`.

Key idea: vault notes are organized by directory, but business features are
scattered cross-component. Without cross_refs, retrieval sees only the
directory neighbours of the vault hit. With cross_refs it sees the whole
call flow.

Architecturally these are computed fields in the frontmatter (vs static
LLM-generated ones): always computed cheaply through graph queries, without
an LLM, and not governed by the resume hash. That is deliberate — so that
adding a new computed field does not require an expensive full regeneration
of the vault through Gemini (incremental does not break).

PR-review note: this module's graph traversal is built so that it will be
easy to add impact analysis for a PR (callers/callees of the changed symbol)
in the future. The current CrossRefsComputer focuses on module-level
aggregation; for PR review there will be a separate class working at
symbol-level with the same graph queries (CALLS, IMPORTS).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.indexing.graph.graph_store import GraphStore
from src.vault.reader import VaultReader

logger = logging.getLogger(__name__)


# Bump when computed-fields semantics change → old notes are considered
# stale in Phase 4b and get re-computed.
COMPUTED_SCHEMA_VERSION = 1


@dataclass
class ComputedFields:
    """Computed block of the frontmatter — graph-derived, without an LLM."""

    cross_refs: list[str] = field(default_factory=list)
    callers_modules: list[str] = field(default_factory=list)
    outgoing_call_count: int = 0
    incoming_call_count: int = 0
    graph_centrality: float | None = None  # PageRank, not implemented yet
    computed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: int = COMPUTED_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "cross_refs": list(self.cross_refs),
            "callers_modules": list(self.callers_modules),
            "outgoing_call_count": int(self.outgoing_call_count),
            "incoming_call_count": int(self.incoming_call_count),
            "computed_at": self.computed_at,
            "computed_schema_version": int(self.schema_version),
        }
        if self.graph_centrality is not None:
            d["graph_centrality"] = float(self.graph_centrality)
        return d

    def content_hash_input(self) -> str:
        """Canonical form for comparison — no timestamp, deterministic ordering."""
        parts = [
            "cr:" + "|".join(sorted(self.cross_refs)),
            "cm:" + "|".join(sorted(self.callers_modules)),
            f"oc:{self.outgoing_call_count}",
            f"ic:{self.incoming_call_count}",
            f"gc:{self.graph_centrality}",
            f"sv:{self.schema_version}",
        ]
        return "\n".join(parts)


class CrossRefsComputer:
    """Computes ComputedFields for modules through graph traversal.

    Algorithm:
      1. Hop1 outgoing: modules that direct CALLS/IMPORTS lead to.
      2. Hop2 outgoing: modules reachable through the top-K hop1 ones
         (weighted down — see HOP2_DISCOUNT).
      3. Hop1 incoming (callers): modules that call/import into ours.
      4. Filter ≥ MIN_LINKS, sort by score, cap at MAX_REFS.

    Self-references are excluded. file_module nodes are excluded (real symbols only).
    """

    MIN_LINKS_HOP1 = 1     # hop1 — direct edges, even 1 link matters (entry point)
    MIN_LINKS_HOP2 = 2     # hop2 — needs ≥2 so we don't drag in random utils
    MIN_LINKS_INCOMING = 2 # callers — so we don't drag in episodic calls
    MAX_REFS = 12          # cap on cross_refs so the bundle doesn't blow up
    HOP2_TOP_PARENTS = 5   # how many top-hop1 modules we try for hop2
    HOP2_DISCOUNT = 2      # divide hop2 counts by this number (slightly lower weight)

    def __init__(
        self,
        store: GraphStore,
        vault_reader: VaultReader,
        repo: str,
    ) -> None:
        self.store = store
        self.vault_reader = vault_reader
        self.repo = repo
        self._path_to_module: dict[str, str] = self._build_module_index()

    def _build_module_index(self) -> dict[str, str]:
        """Map: source-path-prefix → vault note ref ("modules/<name>" without .md).

        Built once at init. If the vault is empty — the index is empty and all
        compute_for_module calls return empty ComputedFields (no-op).
        """
        notes = self.vault_reader.list_notes(self.repo, note_type="module")
        idx: dict[str, str] = {}
        for n in notes:
            path = (n.metadata.get("path") or "").rstrip("/")
            if not path:
                continue
            ref = n.relative_path
            if ref.endswith(".md"):
                ref = ref[:-3]
            idx[path] = ref
        logger.info("crossrefs_module_index built entries=%d", len(idx))
        return idx

    def _module_for_file(self, file_path: str) -> str | None:
        """Find the longest module-prefix that covers the file."""
        best: str | None = None
        best_len = -1
        for prefix, ref in self._path_to_module.items():
            covers = file_path == prefix or file_path.startswith(prefix + "/")
            if covers and len(prefix) > best_len:
                best_len = len(prefix)
                best = ref
        return best

    def _path_for_module_ref(self, ref: str) -> str | None:
        """Inverse lookup: ref → source path."""
        for path, r in self._path_to_module.items():
            if r == ref:
                return path
        return None

    def _outgoing_modules(self, prefix: str) -> dict[str, int]:
        """depth=1: where CALLS+IMPORTS from this module's symbols lead to."""
        rows = self.store.query(
            "MATCH (s:Symbol)-[:CALLS|IMPORTS]->(t:Symbol) "
            "WHERE s.file STARTS WITH $p AND t.kind <> 'file_module' "
            "  AND NOT t.file STARTS WITH $p "
            "RETURN t.file AS file, count(*) AS n",
            params={"p": prefix},
        )
        mods: dict[str, int] = defaultdict(int)
        for r in rows:
            mod = self._module_for_file(str(r.get("file", "")))
            if mod:
                mods[mod] += int(r.get("n", 0) or 0)
        return mods

    def _incoming_modules(self, prefix: str) -> dict[str, int]:
        """depth=1: who calls/imports this module's symbols."""
        rows = self.store.query(
            "MATCH (caller:Symbol)-[:CALLS|IMPORTS]->(s:Symbol) "
            "WHERE s.file STARTS WITH $p AND caller.kind <> 'file_module' "
            "  AND NOT caller.file STARTS WITH $p "
            "RETURN caller.file AS file, count(*) AS n",
            params={"p": prefix},
        )
        mods: dict[str, int] = defaultdict(int)
        for r in rows:
            mod = self._module_for_file(str(r.get("file", "")))
            if mod:
                mods[mod] += int(r.get("n", 0) or 0)
        return mods

    def compute_for_module(self, module_path: str) -> ComputedFields:
        """Cross_refs for a single module. Depends only on the graph."""
        prefix = module_path.rstrip("/") + "/"
        self_ref = self._module_for_file(module_path.rstrip("/"))

        try:
            hop1_out = self._outgoing_modules(prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crossrefs_hop1_out_failed module=%s err=%s", module_path, exc)
            hop1_out = {}

        # Throw away the self-reference
        if self_ref and self_ref in hop1_out:
            del hop1_out[self_ref]

        # Hop 2 — only through the top-N parents, to limit the explosion
        hop2_out: dict[str, int] = defaultdict(int)
        top_parents = sorted(hop1_out.items(), key=lambda kv: -kv[1])[: self.HOP2_TOP_PARENTS]
        for parent_ref, _ in top_parents:
            parent_path = self._path_for_module_ref(parent_ref)
            if not parent_path:
                continue
            parent_prefix = parent_path.rstrip("/") + "/"
            try:
                rows = self._outgoing_modules(parent_prefix)
            except Exception as exc:  # noqa: BLE001
                logger.debug("crossrefs_hop2_failed parent=%s err=%s", parent_ref, exc)
                continue
            for mod, n in rows.items():
                if mod == self_ref or mod in hop1_out:
                    continue
                hop2_out[mod] += n // self.HOP2_DISCOUNT

        # Backward — callers
        try:
            incoming = self._incoming_modules(prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("crossrefs_incoming_failed module=%s err=%s", module_path, exc)
            incoming = {}
        if self_ref and self_ref in incoming:
            del incoming[self_ref]

        # Hop1 — direct edges, always included (≥MIN_LINKS_HOP1).
        # Hop2 — discounted, needs a higher threshold.
        hop1_qualified = {m: n for m, n in hop1_out.items() if n >= self.MIN_LINKS_HOP1}
        hop2_qualified = {m: n for m, n in hop2_out.items() if n >= self.MIN_LINKS_HOP2}

        merged: dict[str, int] = defaultdict(int)
        for m, n in hop1_qualified.items():
            merged[m] += n + 1000  # priority boost: direct edges always above hop2
        for m, n in hop2_qualified.items():
            merged[m] += n

        cross_refs = [m for m, _ in sorted(merged.items(), key=lambda kv: -kv[1])][: self.MAX_REFS]

        callers = [
            m for m, n in sorted(incoming.items(), key=lambda kv: -kv[1])
            if n >= self.MIN_LINKS_INCOMING
        ][: self.MAX_REFS]

        return ComputedFields(
            cross_refs=cross_refs,
            callers_modules=callers,
            outgoing_call_count=sum(hop1_out.values()),
            incoming_call_count=sum(incoming.values()),
            graph_centrality=None,
        )

    def compute_for_all_modules(self) -> dict[str, ComputedFields]:
        """Batch compute for all vault modules. Returns {note_ref → ComputedFields}."""
        out: dict[str, ComputedFields] = {}
        for path, ref in self._path_to_module.items():
            try:
                out[ref] = self.compute_for_module(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("crossrefs_compute_failed module=%s err=%s", path, exc)
        return out
