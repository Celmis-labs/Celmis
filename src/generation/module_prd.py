"""Generation of the per-module PRD document."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from src.config import Settings, get_settings
from src.indexing.modules import Module
from src.indexing.semgrep import SemgrepReport
from src.llm.prompts import MODULE_PRD_PROMPT, MODULE_PRD_SYSTEM
from src.llm.prompts.language import with_language
from src.retrieval.tier2_graph import GraphRetriever
from src.retrieval.tier3_code import CodeReader
from src.vault.provenance import build as _provenance
from src.vault.writer import NoteMetadata, VaultWriter

logger = logging.getLogger(__name__)

def _files_changed_between(
    repo_path: Path,
    old_sha: str,
    new_sha: str,
    files: list[str],
) -> bool:
    """True if any of `files` changed between old_sha..new_sha in Git.

    If we cannot determine it exactly (the old commit is absent from a shallow
    history, git error) — we return True (safe: better to regenerate than to
    miss something stale).
    """
    if not files:
        return True
    if old_sha == new_sha:
        return False
    try:
        from git import Repo

        repo = Repo(str(repo_path))
        # git diff --name-only old new -- file1 file2 ...
        # batch-limit so we don't blow up on a very large argv (>~1500 files)
        chunk_size = 500
        for i in range(0, len(files), chunk_size):
            batch = files[i : i + chunk_size]
            diff = repo.git.diff("--name-only", old_sha, new_sha, "--", *batch)
            if diff.strip():
                return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "git_diff_failed old=%s new=%s err=%s — assuming changed",
            old_sha[:8],
            new_sha[:8],
            exc,
        )
        return True

@dataclass
class ModulePRDResult:
    """Result of generating one module."""

    module_name: str
    note_path: Path
    tokens_in: int
    tokens_out: int
    skipped: bool = False  # True when the module was skipped (already generated at this commit)

class ModulePRDGenerator:
    """Builds the PRD for one module: graph + code → Gemini → MD."""

    def __init__(
        self,
        settings: Settings | None = None,
        graph_retriever: GraphRetriever | None = None,
        code_reader: CodeReader | None = None,
        vault_writer: VaultWriter | None = None,
        language: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        #: Output language for the generated document. None means "whatever
        #: the workspace is set to" — resolved at call time by `with_language`,
        #: so a run can override it by assigning this attribute without
        #: rebuilding the generator.
        self.language = language
        #: Which engine writes the document. None → the legacy direct
        #: Gemini path, kept as the default so this change adds a capability
        #: without moving anybody's transport underneath them. The orchestrator
        #: sets it per run.
        self.engine = None
        # v3.0: GraphRetriever now works directly with FalkorDBLiteStore.
        self.graph = graph_retriever or GraphRetriever(self.settings)
        self.code_reader = code_reader or CodeReader(self.settings)
        self.vault_writer = vault_writer or VaultWriter(self.settings, workspace_id=getattr(self, 'workspace_id', None))

    def _dispatch(
        self, *, prompt: str, system_instruction: str, code_context: str,
        metadata_context: dict, operation: str, repo: str,
        module: str | None = None, files_sent: list[str] | None = None,
    ):
        """Send the document to whichever engine this run selected.

        `self.engine` is None on the legacy path, which still goes straight to
        the Gemini client — the same call, the same arguments, so choosing an
        engine adds a capability rather than moving everybody's transport.
        """
        if self.engine is None:
            # Never silently fall back to a direct provider SDK: that
            # is exactly the bypass this module was fixed to remove,
            # and it would be invisible in the output.
            raise RuntimeError(
                f"{type(self).__name__} has no engine — build one with "
                "src.generation.engines.build_engine()")
        return self.engine.generate(
                prompt=prompt,
                system_instruction=system_instruction,
                code_context=code_context,
                metadata_context=metadata_context,
                operation=operation,
                repo=repo,
                module=module,
            )

    def generate(
        self,
        *,
        repo: str,
        repo_path: Path,
        commit_sha: str,
        module: Module,
        semgrep: SemgrepReport | None = None,
        force: bool = False,
    ) -> ModulePRDResult:
        """Generate and write the PRD for one module.

        If the vault file already exists and its commit matches the current one
        — generation is skipped (returns with skipped=True). This lets an
        interrupted run be resumed. For a forced regeneration pass force=True.
        """
        # ─── Resume check ────────────────────────────────────────────
        if not force:
            existing_path = self._existing_vault_path(repo, module)
            if existing_path:
                old_commit = self._vault_commit(existing_path)
                if old_commit:
                    # Case 1: the same commit → definitely skip
                    if old_commit == commit_sha:
                        logger.info(
                            "module_prd_skip module=%s commit=%s (same)",
                            module.name,
                            commit_sha[:8],
                        )
                        return ModulePRDResult(
                            module_name=module.name,
                            note_path=existing_path,
                            tokens_in=0,
                            tokens_out=0,
                            skipped=True,
                        )
                    # Case 2: different commits, but none of the module's files changed
                    if not _files_changed_between(
                        repo_path, old_commit, commit_sha, module.files
                    ):
                        self._update_vault_commit(existing_path, commit_sha)
                        logger.info(
                            "module_prd_skip module=%s commit=%s (no file changes)",
                            module.name,
                            commit_sha[:8],
                        )
                        return ModulePRDResult(
                            module_name=module.name,
                            note_path=existing_path,
                            tokens_in=0,
                            tokens_out=0,
                            skipped=True,
                        )
        # ─── 1. Seed symbols (opt., for graph expand) ───────────────
        # Note: graph enrichment is currently disabled — the Cypher schema in
        # FalkorDB CodeGraphContext needs to be clarified. The disk fallback
        # below works independently of the graph.
        symbols = module.symbols
        seed_names = list({s.name for s in symbols})[:20] if symbols else []

        # ─── 2. Graph expand ─────────────────────────────────────────
        from src.retrieval.tier2_graph import GraphExpansion

        graph_exp = (
            self.graph.expand(repo_path, seed_names) if seed_names else GraphExpansion()
        )

        # ─── 3. Read code ────────────────────────────────────────────
        locations: list[tuple[str, int, int | None]] = []
        if graph_exp.all_symbols:
            locations = [
                (s.file, s.line, s.end_line)
                for s in graph_exp.all_symbols.values()
                if s.file.startswith(module.path.rstrip("/") + "/")
            ][: self.settings.max_graph_nodes_per_query]

        # Fallback: if the graph is empty — read module files straight from disk
        if not locations:
            logger.info(
                "module_prd_fallback_disk module=%s files=%d",
                module.name,
                len(module.files),
            )
            # Sort by size (smaller files first — more of them fit)
            file_paths = [
                (f, (repo_path / f).stat().st_size)
                for f in module.files
                if (repo_path / f).exists()
            ]
            file_paths.sort(key=lambda x: x[1])
            locations = [(f, 1, None) for f, _ in file_paths[:100]]

        code_bundle = self.code_reader.read_locations(
            repo_path,
            locations,
            budget_tokens=self.settings.max_code_tokens_per_module,
            context_lines=2,
            redact_content=True,
        )

        # ─── 4. Semgrep findings for the module ──────────────────────
        findings = semgrep.filter_by_path(module.path) if semgrep else []

        # ─── 5. Build prompt payload ─────────────────────────────────
        metadata_ctx = {
            "module": module.name,
            "path": module.path,
            "files_count": len(module.files),
            "symbols_count": len(symbols),
            "graph": graph_exp.as_llm_context(),
            "security_findings": [f.as_dict() for f in findings],
        }

        # ─── 6. Gemini generation ────────────────────────────────────
        result = self._dispatch(
            prompt=MODULE_PRD_PROMPT,
            system_instruction=with_language(MODULE_PRD_SYSTEM, self.language),
            code_context=code_bundle.as_markdown(),
            metadata_context=metadata_ctx,
            operation="generate_module_prd",
            repo=repo,
            module=module.name,
            files_sent=code_bundle.files_included(),
        )

        # ─── 7. Write to the vault ───────────────────────────────────
        entry_points = [s.name for s in symbols if s.kind in ("function", "class", "component")][:10]
        keywords = self._extract_keywords(module.name, result.text, symbols)

        meta = NoteMetadata(
            # What produced this document. It is handed to a new developer,
            # shown to an auditor and committed to a repository — it outlives
            # every other place this answer could be kept.
            provenance=_provenance(
                engine=getattr(result, "engine", None),
                model=getattr(result, "model", None),
                language=self.language,
                tools_used=getattr(result, "tools_used", None),
                commit=commit_sha,
            ),
            type="module",
            repo=repo,
            commit=commit_sha,
            module=module.name,
            path=module.path,
            symbols=sorted({s.name for s in symbols})[:40],
            entry_points=entry_points,
            keywords=keywords,
            external_deps=[],  # TODO: extract from graph imports
            security_findings=len(findings),
        )

        note_rel = f"modules/{module.name}.md"
        full_path = self.vault_writer.write(
            repo=repo,
            relative_path=note_rel,
            content=result.text,
            metadata=meta,
            redact_content=True,
        )

        logger.info(
            "module_prd_done module=%s tokens=%d/%d files=%d",
            module.name,
            result.input_tokens,
            result.output_tokens,
            len(code_bundle.files_included()),
        )
        return ModulePRDResult(
            module_name=module.name,
            note_path=full_path,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
        )

    def _existing_vault_path(self, repo: str, module: Module) -> Path | None:
        """Returns the path to the module's existing vault note, if any."""
        note_rel = f"modules/{module.name}.md"
        path = self.settings.repo_vault_path(repo) / note_rel
        return path if path.exists() else None

    @staticmethod
    def _vault_commit(note_path: Path) -> str | None:
        """Reads frontmatter.commit from an existing note."""
        try:
            import frontmatter

            post = frontmatter.load(note_path)
            commit = str(post.metadata.get("commit") or "")
            return commit or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("vault_commit_read_failed path=%s err=%s", note_path, exc)
            return None

    @staticmethod
    def _update_vault_commit(note_path: Path, new_commit: str) -> None:
        """Updates frontmatter.commit without changing the content — for when the
        module did not change but the commit in the repo moved forward."""
        try:
            from datetime import datetime

            import frontmatter

            post = frontmatter.load(note_path)
            post["commit"] = new_commit
            post["analyzed_at"] = datetime.now(UTC).isoformat()
            note_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("vault_commit_update_failed path=%s err=%s", note_path, exc)

    @staticmethod
    def _extract_keywords(module_name: str, content: str, symbols) -> list[str]:
        """Simple heuristic: the module name, top-N words from content."""
        kws: set[str] = {module_name}
        # The first 3 symbols should be keywords
        kws.update(s.name for s in symbols[:3])
        # Extract CamelCase + snake_case identifiers from the text
        for token in re.findall(r"\b[A-Z][a-zA-Z]+|[a-z]+(?:_[a-z]+)+\b", content):
            kws.add(token.lower())
            if len(kws) >= 20:
                break
        return sorted(kws)[:20]
