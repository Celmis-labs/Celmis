"""Generation of PRD-level feature documents (cross-module).

Feature — a logical feature, it can span several modules (e.g. "Authentication"
touches auth + api-client + routing). They are discovered via keyword
clustering of the module notes, or from an explicit list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings, get_settings
from src.llm.prompts import FEATURE_PROMPT, FEATURE_SYSTEM
from src.llm.prompts.language import with_language
from src.retrieval.tier2_graph import GraphRetriever
from src.retrieval.tier3_code import CodeReader
from src.vault.provenance import build as _provenance
from src.vault.writer import NoteMetadata, VaultWriter

logger = logging.getLogger(__name__)

@dataclass
class FeatureSpec:
    """Feature description — name + seed symbols/modules to find the code."""

    name: str  # "login-flow", "order-management", etc.
    description: str = ""
    seed_symbols: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)

class FeatureDocGenerator:
    """For a single FeatureSpec — collects cross-module context and
    generates a PRD.
    """

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
        # v3.0: GraphRetriever — own FalkorDBLiteStore backend
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
        feature: FeatureSpec,
    ) -> Path:
        graph_exp = self.graph.expand(repo_path, feature.seed_symbols)
        locations = [
            (s.file, s.line, s.end_line)
            for s in graph_exp.all_symbols.values()
        ]
        code_bundle = self.code_reader.read_locations(
            repo_path,
            locations,
            budget_tokens=self.settings.max_code_tokens_per_module,
            context_lines=3,
        )

        # Belt to `enrich_with_graph`'s brace. If expansion still yields
        # nothing — a feature whose seeds were pruned, a graph that failed to
        # build — read the files of the modules this feature spans instead of
        # sending metadata alone. The module generator has had this fallback
        # all along, which is why module notes carried real `file.py:line`
        # references while every feature note said the source was absent and
        # marked its steps and business rules as *not determined*.
        if not code_bundle.files_included():
            fallback = _module_file_locations(repo_path, feature.modules)
            if fallback:
                logger.info(
                    "feature_code_fallback feature=%s modules=%d files=%d",
                    feature.name, len(feature.modules), len(fallback),
                )
                code_bundle = self.code_reader.read_locations(
                    repo_path, fallback,
                    budget_tokens=self.settings.max_code_tokens_per_module,
                    context_lines=0,
                )
            else:
                logger.warning(
                    "feature_no_code feature=%s — the note will describe "
                    "structure only", feature.name,
                )

        metadata_ctx = {
            "feature": feature.name,
            "description": feature.description,
            "modules_involved": feature.modules,
            "graph": graph_exp.as_llm_context(),
        }

        result = self._dispatch(
            prompt=FEATURE_PROMPT,
            system_instruction=with_language(FEATURE_SYSTEM, self.language),
            code_context=code_bundle.as_markdown(),
            metadata_context=metadata_ctx,
            operation="generate_feature_doc",
            repo=repo,
            module=None,
            files_sent=code_bundle.files_included(),
        )

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
            type="feature",
            repo=repo,
            commit=commit_sha,
            feature=feature.name,
            symbols=sorted(graph_exp.all_symbols.keys())[:40],
            cross_refs=[f"modules/{m}" for m in feature.modules],
            keywords=[feature.name.lower().replace("-", " "), *feature.modules],
        )

        rel = f"features/{feature.name}.md"
        return self.vault_writer.write(
            repo=repo,
            relative_path=rel,
            content=result.text,
            metadata=meta,
        )

#: Enough to characterise a feature without turning its note into a code dump.
MAX_FALLBACK_FILES = 12

def _module_file_locations(
    repo_path: Path, modules: list[str],
) -> list[tuple[str, int, int | None]]:
    """Whole-file locations for the modules a feature spans.

    `modules` holds names as feature detection saw them; notes record them as
    `modules/<name>`, so both shapes are accepted. Files are taken from the
    directory that matches the module name — the same path-based rule module
    discovery uses.
    """
    from src.config import get_settings

    settings = get_settings()
    extensions = set(settings.supported_extensions)
    out: list[tuple[str, int, int | None]] = []
    seen: set[str] = set()

    for raw in modules:
        name = str(raw).rsplit("/", 1)[-1].strip()
        if not name:
            continue
        for candidate in (repo_path / name, repo_path / "src" / name):
            if not candidate.is_dir():
                continue
            for path in sorted(candidate.rglob("*")):
                if len(out) >= MAX_FALLBACK_FILES:
                    return out
                if not path.is_file() or path.suffix not in extensions:
                    continue
                rel = str(path.relative_to(repo_path))
                if rel in seen:
                    continue
                seen.add(rel)
                # (file, line, end_line) with line=1 and no end — the reader
                # treats that as "the whole file, within budget".
                out.append((rel, 1, None))
            break
    return out
