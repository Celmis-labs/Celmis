"""Integration guide generation — how to call a service/module from outside."""

from __future__ import annotations

import logging
from pathlib import Path

from src.config import Settings, get_settings
from src.llm.prompts import INTEGRATION_PROMPT, INTEGRATION_SYSTEM
from src.llm.prompts.language import with_language
from src.retrieval.tier2_graph import GraphRetriever
from src.retrieval.tier3_code import CodeReader
from src.vault.provenance import build as _provenance
from src.vault.writer import NoteMetadata, VaultWriter

logger = logging.getLogger(__name__)

class IntegrationDocGenerator:
    """For one exported service — assembles an integration guide."""

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
        service_name: str,
        entry_points: list[str],
    ) -> Path:
        """Generates an integration guide for service_name from the set of
        entry_points.
        """
        graph_exp = self.graph.expand(repo_path, entry_points, depth=1)
        locations = [
            (s.file, s.line, s.end_line)
            for s in graph_exp.all_symbols.values()
        ]
        code_bundle = self.code_reader.read_locations(
            repo_path,
            locations,
            budget_tokens=self.settings.max_code_tokens_per_module,
            context_lines=4,
        )

        metadata_ctx = {
            "service": service_name,
            "entry_points": entry_points,
            "graph": graph_exp.as_llm_context(),
        }

        result = self._dispatch(
            prompt=INTEGRATION_PROMPT,
            system_instruction=with_language(INTEGRATION_SYSTEM, self.language),
            code_context=code_bundle.as_markdown(),
            metadata_context=metadata_ctx,
            operation="generate_integration_doc",
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
            type="integration",
            repo=repo,
            commit=commit_sha,
            entry_points=entry_points,
            symbols=sorted(graph_exp.all_symbols.keys())[:40],
            keywords=[service_name.lower(), "integration", "api"],
            extra={"service": service_name},
        )

        rel = f"integrations/{service_name}.md"
        return self.vault_writer.write(
            repo=repo,
            relative_path=rel,
            content=result.text,
            metadata=meta,
        )
