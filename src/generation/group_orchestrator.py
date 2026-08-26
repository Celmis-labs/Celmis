"""Group-aware orchestrator — composes GenerationOrchestrator per repo +
generates group-level overview document.

Stage 11 (May 2026) — implementation.

Flow:
    1. For every repo in the RepoGroup → run GenerationOrchestrator.run()
       (sync + index + module discovery + LLM PRD generation)
    2. Materialize cross-repo edges (via GroupIndexer._persist_cross_repo_edges)
    3. Generate a group overview MD with:
        - List of repos (name + provider + indexed status + primary lang + symbol count)
        - Cross-repo edges table
        - Aggregated metrics

Output:
    {vault_dir}/groups/{group_name}/_overview.md   — group-level
    {vault_dir}/projects/{repo_slug}/...            — per-repo (existing)

No LLM call for the overview — deterministic markdown rendered from graph data.
LLM-generated executive summary — V2 enhancement (can be added later behind a
feature flag).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.config import Settings, get_settings
from src.generation.orchestrator import GenerationOrchestrator, GenerationResult
from src.groups.indexer import GroupIndexer
from src.groups.models import RepoGroup
from src.indexing.graph.graph_store import make_graph_store
from src.sync.clone import CloneError

logger = logging.getLogger(__name__)


@dataclass
class GroupGenerationResult:
    """Aggregated result across all repos in the group."""

    group_name: str
    per_repo: list[GenerationResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    cross_repo_edges: int = 0
    overview_path: Path | None = None
    elapsed_seconds: float = 0.0

    @property
    def total_modules(self) -> int:
        return sum(len(r.modules_generated) for r in self.per_repo)

    @property
    def total_features(self) -> int:
        return sum(len(r.features_generated) for r in self.per_repo)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens_in + r.total_tokens_out for r in self.per_repo)


class GroupGenerationOrchestrator:
    """Composes per-repo GenerationOrchestrator + group-level overview generation.

    Idempotent — re-running updates the overview document; per-repo generation
    respects the existing `force` flag (resume vs rebuild).
    """

    def __init__(
        self,
        group: RepoGroup,
        settings: Settings | None = None,
    ) -> None:
        self.group = group
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self._orchestrator = GenerationOrchestrator(self.settings)
        self._group_indexer = GroupIndexer(group, self.settings)

    def run(
        self,
        *,
        branch: str | None = None,
        skip_semgrep: bool = False,
        force: bool = False,
        progress_callback: Callable[[str, str], None] | None = None,
    ) -> GroupGenerationResult:
        """Run generation for all repos + materialize cross-repo + write overview.

        Args:
            branch: target branch (None → repo's default).
            skip_semgrep: skip the SAST scan per repo.
            force: rebuild even if the vault already exists.
            progress_callback: (phase, detail) — for UI/CLI updates.
        """
        import time

        t0 = time.time()
        result = GroupGenerationResult(group_name=self.group.name)

        def _notify(phase: str, detail: str = "") -> None:
            if progress_callback:
                # A UI/CLI callback must never abort the run it reports on.
                with contextlib.suppress(Exception):
                    progress_callback(phase, detail)

        # ─── Per-repo generation ─────────────────────────────
        for i, repo_id in enumerate(self.group.repos, 1):
            _notify("repo", f"[{i}/{len(self.group.repos)}] {repo_id}")
            try:
                gen_res = self._orchestrator.run(
                    repo_id,
                    # None → use repo's actual default branch (remote HEAD).
                    # Previously hardcoded "dev" — works for legacy Bitbucket
                    # repos but breaks GitHub/GitLab repos whose default
                    # branch is "main".
                    branch=branch,
                    skip_semgrep=skip_semgrep,
                    force=force,
                )
                result.per_repo.append(gen_res)
            except CloneError as exc:
                msg = f"clone_failed repo={repo_id}: {exc}"
                logger.error(msg)
                result.failures.append(msg)
                continue
            except Exception as exc:  # noqa: BLE001
                msg = f"generation_failed repo={repo_id}: {type(exc).__name__}: {exc}"
                logger.exception(msg)
                result.failures.append(msg)
                continue

        # ─── Cross-repo materialization ──────────────────────
        # Per-repo extraction is already written into the per-repo graph during
        # GenerationOrchestrator. Reloading the extractions from the graphs for
        # the cross-repo resolver — but that is not efficient.
        # Simpler: re-walk each repo + extract again via the factory (a cheap
        # operation after clone+index). Even simpler — call GroupIndexer.index()
        # in parallel, because it already does per-repo extraction +
        # materialization.
        #
        # Optimal: GroupIndexer and GenerationOrchestrator share the extraction
        # step — this duplicates work. YAGNI for now: GroupIndexer is fast
        # (~7-15s per repo).
        _notify("cross_repo", "materializing cross-repo edges")
        try:
            cross_result = self._group_indexer.index(
                progress_callback=lambda msg: _notify("cross_repo", msg),
            )
            result.cross_repo_edges = cross_result.cross_repo_edges
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross_repo_materialize_failed err=%s", exc)
            result.failures.append(f"cross_repo: {exc}")

        # ─── Overview document ───────────────────────────────
        _notify("overview", "generating group overview")
        try:
            result.overview_path = self._write_overview(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("overview_write_failed err=%s", exc)
            result.failures.append(f"overview: {exc}")

        result.elapsed_seconds = time.time() - t0
        logger.info(
            "group_generation_complete group=%s repos=%d failures=%d "
            "cross_repo=%d %.1fs",
            self.group.name, len(result.per_repo), len(result.failures),
            result.cross_repo_edges, result.elapsed_seconds,
        )
        return result

    # ─── Overview generation ────────────────────────────────

    def _write_overview(self, result: GroupGenerationResult) -> Path:
        """Build the group overview Markdown — deterministic, without an LLM."""
        overview_dir = self.settings.vault_dir / "groups" / self.group.name
        overview_dir.mkdir(parents=True, exist_ok=True)
        overview_path = overview_dir / "_overview.md"

        # Frontmatter
        now = datetime.now(UTC).isoformat()
        repo_slugs = []
        for r in result.per_repo:
            repo_slugs.append(r.repo)

        # Body
        lines: list[str] = []
        lines.append("---")
        lines.append(f"group: {self.group.name}")
        lines.append(f"description: {self.group.description or '(empty)'}")
        lines.append(f"generated_at: {now}")
        lines.append(f"repos: {len(self.group.repos)}")
        lines.append(f"indexed_repos: {len(result.per_repo)}")
        lines.append(f"failed_repos: {len(result.failures)}")
        lines.append(f"cross_repo_edges: {result.cross_repo_edges}")
        lines.append(f"total_modules: {result.total_modules}")
        lines.append(f"total_features: {result.total_features}")
        lines.append("---")
        lines.append("")
        lines.append(f"# 🗂️ {self.group.name}")
        lines.append("")
        if self.group.description:
            lines.append(self.group.description)
            lines.append("")

        # ─── Repos table ────────────────────────────────────
        lines.append("## Repositories")
        lines.append("")
        if not result.per_repo:
            lines.append("*(no repos indexed)*")
        else:
            lines.append("| Slug | Commit | Modules | Features | Integrations | SAST findings |")
            lines.append("|---|---|---|---|---|---|")
            for r in result.per_repo:
                lines.append(
                    f"| `{r.repo}` | `{r.commit[:8]}` | {len(r.modules_generated)} | "
                    f"{len(r.features_generated)} | {len(r.integrations_generated)} | "
                    f"{r.semgrep_findings} |"
                )
        lines.append("")

        # ─── Failures ───────────────────────────────────────
        if result.failures:
            lines.append("## ⚠️ Failures")
            lines.append("")
            for f in result.failures:
                lines.append(f"- `{f}`")
            lines.append("")

        # ─── Cross-repo edges ───────────────────────────────
        cross_edges = self._load_cross_repo_edges()
        lines.append("## Cross-repo edges")
        lines.append("")
        if not cross_edges:
            lines.append(
                "*No cross-repo edges materialized. Possible reasons: "
                "no image references between repos, or the repos share no Dockerfile/Compose patterns.*"
            )
        else:
            lines.append(f"Total: **{len(cross_edges)}** edges")
            lines.append("")
            lines.append("| Kind | From repo | From symbol | To repo | To target | Confidence |")
            lines.append("|---|---|---|---|---|---|")
            for e in cross_edges[:100]:  # cap viewable in MD
                lines.append(
                    f"| `{e['kind']}` | `{e['from_repo']}` | "
                    f"`{_truncate(e['from_id'], 30)}` | `{e['to_repo']}` | "
                    f"`{_truncate(e['to_id'], 30)}` | {e.get('confidence', '?')} |"
                )
            if len(cross_edges) > 100:
                lines.append("")
                lines.append(f"*({len(cross_edges) - 100} more edges not shown)*")
        lines.append("")

        # ─── Aggregated metrics ─────────────────────────────
        lines.append("## Metrics")
        lines.append("")
        lines.append(f"- Total modules generated: **{result.total_modules}**")
        lines.append(f"- Total features generated: **{result.total_features}**")
        lines.append(f"- Total tokens (in + out): **{result.total_tokens:,}**")
        lines.append(f"- Total elapsed: **{result.elapsed_seconds:.1f}s**")
        lines.append("")

        # ─── Per-repo links ────────────────────────────────
        if result.per_repo:
            lines.append("## Per-repo PRDs")
            lines.append("")
            for r in result.per_repo:
                lines.append(f"- [[{r.repo}/index]] (`{r.commit[:8]}`)")
            lines.append("")

        overview_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("group_overview_written path=%s", overview_path)
        return overview_path

    def _load_cross_repo_edges(self) -> list[dict]:
        """Read materialized cross-repo edges from the group fdblite."""
        from src.groups.manager import GroupManager

        db_path = GroupManager(settings=self.settings).graph_path(self.group)
        if not db_path.exists():
            return []

        store = make_graph_store(db_path)
        try:
            res = store.query(
                "MATCH (a:Symbol)-[r]->(b:Symbol) "
                "RETURN a.id AS from_id, a.module AS from_repo, "
                "       b.id AS to_id, b.module AS to_repo, "
                "       type(r) AS kind"
            )
        finally:
            store.close()

        return [
            {
                "from_repo": str(row.get("from_repo") or ""),
                "from_id": str(row.get("from_id") or ""),
                "to_repo": str(row.get("to_repo") or ""),
                "to_id": str(row.get("to_id") or ""),
                "kind": str(row.get("kind") or ""),
                "confidence": "?",  # not stored in synthetic edges right now
            }
            for row in res
        ]


def _truncate(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[: max_len - 1] + "…"
