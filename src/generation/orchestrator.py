"""Mode A orchestrator — the full batch vault-generation pipeline."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from src.config import Settings, get_settings
from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
from src.generation.feature_doc import FeatureDocGenerator, FeatureSpec
from src.generation.integration_doc import IntegrationDocGenerator
from src.generation.module_prd import ModulePRDGenerator

# v3.0: CodegraphClient removed — graph indexing goes through src.indexing.graph (Phase 5)
from src.indexing.modules import Module, ModuleDiscovery
from src.indexing.semgrep import SemgrepReport, SemgrepRunner
from src.sync.clone import RepoSync, SyncResult
from src.vault.writer import NoteMetadata, VaultWriter

logger = logging.getLogger(__name__)
console = Console()

# This many quota errors from the LLM IN A ROW stop generation: past that
# point every further module is guaranteed to fail the same way, and the loop
# only hammers 429s at the provider.
_QUOTA_ABORT_AFTER = 3


class QuotaExhaustedError(RuntimeError):
    """LLM quota exhausted (HTTP 429 / RESOURCE_EXHAUSTED) — abort the run
    instead of grinding every remaining module through the same error."""


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc)
    return (
        "429" in s
        or "RESOURCE_EXHAUSTED" in s
        or "Too Many Requests" in s
        or "quota" in s.lower()
    )


@dataclass
class GenerationResult:
    repo: str
    commit: str
    modules_generated: list[str] = field(default_factory=list)
    modules_skipped: list[str] = field(default_factory=list)
    features_generated: list[str] = field(default_factory=list)
    integrations_generated: list[str] = field(default_factory=list)
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    semgrep_findings: int = 0
    #: Documents that raised. A build where every one of them failed used to
    #: print "Generation complete" and the queue job succeeded — so an empty
    #: vault and a perfect vault looked identical from the outside.
    failures: list[str] = field(default_factory=list)
    #: Notes that reached the vector store, and why any batch did not.
    #:
    #: Documents and vectors are two halves of a vault and only one of them
    #: was ever checked. On production all three generation jobs reported
    #: `completed`, `attempts: 1`, `last_error: null` — and produced ZERO
    #: vectors, because `batched_qdrant()` downgrades an upsert failure to a
    #: warning and `produced_nothing` counts DOCUMENTS. Markdown existed, so
    #: the job passed. The user was left with a chat banner asking them to
    #: generate a vault they had just generated, and no run would ever clear
    #: it.
    notes_embedded: int = 0
    embedding_failures: list[str] = field(default_factory=list)

    @property
    def embedded_nothing(self) -> bool:
        """The vector store rejected everything that was offered to it.

        Separate from `produced_nothing` because the remedies differ: one is a
        generation failure to retry, the other is the vector half being down
        while the text half worked.

        DELIBERATELY NOT conditioned on documents generated in THIS run. It
        was, and that made the guard dead code for the one user who needs it:
        a resume run regenerates no modules, so `wrote` was False and the
        check could not fire however many upserts failed. Measured — a repo
        with notes already on disk gave `completed / attempts 1 / last_error
        null` twice in a row while Qdrant stayed at zero collections, which is
        the exit-less loop this guard exists to break.

        `embedding_failures` is the whole condition it needs. A run with
        nothing to embed records none and is not a failure; a run that tried
        and was refused records them and is.
        """
        return self.notes_embedded == 0 and bool(self.embedding_failures)

    @property
    def produced_nothing(self) -> bool:
        """Nothing was written and something was attempted.

        Distinct from a resume run where everything was already current — that
        one legitimately generates nothing and is a success.
        """
        return bool(self.failures) and not (
            self.modules_generated or self.features_generated
            or self.integrations_generated)

    def summary(self) -> str:
        if self.produced_nothing:
            head = (f"[red]✗ Generation produced nothing[/red] — "
                    f"{len(self.failures)} document(s) failed")
        elif self.embedded_nothing:
            head = ("[red]✗ Documents written but NOTHING embedded[/red] — "
                    f"{len(self.embedding_failures)} vector-store failure(s); "
                    "semantic search will stay empty")
        elif self.failures:
            head = (f"[yellow]⚠ Generation partly complete[/yellow] — "
                    f"{len(self.failures)} document(s) failed")
        else:
            head = "[green]✅ Generation complete[/green]"
        return (
            f"{head}\n"
            f"  repo={self.repo} commit={self.commit[:8]}\n"
            f"  modules: {len(self.modules_generated)} generated, "
            f"{len(self.modules_skipped)} skipped (resume)\n"
            f"  features: {len(self.features_generated)}\n"
            f"  integrations: {len(self.integrations_generated)}\n"
            f"  tokens in/out: {self.total_tokens_in}/{self.total_tokens_out}\n"
            f"  security findings: {self.semgrep_findings}"
        )


class GenerationOrchestrator:
    """Full pipeline: sync → index → discover → generate → vault + qdrant."""

    def __init__(self, settings: Settings | None = None, *,
                 workspace_id: str = "default", user_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.workspace_id = workspace_id
        # Needed by the agent engine: a Claude subscription token belongs
        # to a person, not a workspace, so without this the engine could
        # only ever find a workspace-level Anthropic API key.
        self.user_id = user_id
        self.settings.ensure_directories()
        self.sync = RepoSync(self.settings)
        self.semgrep = SemgrepRunner(self.settings)
        self.discovery = ModuleDiscovery(self.settings)
        self.vault_writer = VaultWriter(self.settings, workspace_id=workspace_id)
        # Share the same VaultWriter across generators so the batching
        # context manager can buffer their Qdrant upserts collectively.
        self.module_gen = ModulePRDGenerator(self.settings, vault_writer=self.vault_writer)
        self.feature_gen = FeatureDocGenerator(self.settings, vault_writer=self.vault_writer)
        self.integration_gen = IntegrationDocGenerator(self.settings, vault_writer=self.vault_writer)

    def run(
        self,
        repo_identifier: str,
        branch: str | None = None,
        *,
        features: list[FeatureSpec] | None = None,
        integrations: list[tuple[str, list[str]]] | None = None,
        skip_semgrep: bool = False,
        username: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
        progress_callback=None,
        force: bool = False,
        cancel_check=None,
        language: str | None = None,
        engine: str | None = None,
    ) -> GenerationResult:
        """Run the full pipeline.

        Args:
            repo_identifier: "owner/name" or a full URL
            branch: git branch
            features: list of features for the PRDs; if None — we skip them
            integrations: [(service_name, entry_points), ...]; if None — we skip them
            skip_semgrep: skip SAST (for a quick run)
            username: runtime git auth username (for the UI)
            password: runtime git auth password/token (for the UI)
            progress_callback: callable(phase: str, detail: str) — for UI updates
            language: language of the generated documentation; None — take the
                workspace setting. It is set here rather than in the
                constructor, because a one-off run of "this repo in English
                for the customer" must not require changing the setting and
                then changing it back.
        """
        # Resolved once and pushed onto the generators, so every document in
        # one run is written in one language even if the workspace setting is
        # edited midway through a build that takes minutes.
        doc_language = resolve_doc_language(language, self.workspace_id)
        doc_engine = resolve_doc_engine(engine, self.workspace_id)
        # Built once and shared: a vault build is dozens of documents, and an
        # agent engine that re-authenticated per document would spend most of
        # the run starting sessions.
        from src.generation.engines import build_engine

        # Always through an engine — including the default. The api engine
        # dispatches via build_llm_client, so generation finally leaves through
        # the LiteLLM gateway like every other surface instead of constructing
        # google-genai directly and appearing in none of the tenant's routing,
        # keys or spend.
        selected = build_engine(doc_engine, self.workspace_id, self.user_id)
        for gen in (self.module_gen, self.feature_gen, self.integration_gen):
            gen.language = doc_language
            gen.engine = selected
        logger.info("generation_settings repo=%s ws=%s language=%s engine=%s",
                    repo_identifier, self.workspace_id, doc_language, selected.name)
        def _notify(phase: str, detail: str = "") -> None:
            if progress_callback:
                # A UI/CLI callback must never abort the run it reports on.
                with contextlib.suppress(Exception):
                    progress_callback(phase, detail)

        def _checkpoint() -> None:
            """Cooperative cancel — called between units of work."""
            if cancel_check is not None and cancel_check():
                from src.sync.queue import JobCancelled
                raise JobCancelled("vault generation cancelled by user")

        quota_streak = 0

        def _note_gen_failure(exc: Exception) -> None:
            """Counts CONSECUTIVE quota errors across all generation loops;
            past the threshold — QuotaExhaustedError with instructions on how
            to recover."""
            nonlocal quota_streak
            if _is_quota_error(exc):
                quota_streak += 1
                if quota_streak >= _QUOTA_ABORT_AFTER:
                    raise QuotaExhaustedError(
                        f"LLM quota exhausted (429) — aborted after "
                        f"{quota_streak} consecutive quota errors. The queue "
                        "will auto-retry with backoff; or press Retry on the "
                        "Jobs page once the quota resets."
                    ) from exc
            else:
                quota_streak = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            # ─── Phase 1: Sync ───────────────────────────────────────
            _notify("sync", f"Cloning {repo_identifier}")
            t_sync = progress.add_task("[cyan]Phase 1: Cloning/updating repo...", total=None)

            def _clone_progress(msg: str) -> None:
                _notify("sync", msg)

            sync_result = self.sync.clone_or_update(
                repo_identifier,
                branch,
                username=username,
                password=password,
                api_token=api_token,
                progress_callback=_clone_progress,
            )
            progress.update(t_sync, completed=1)

            # ─── Phase 2: Indexing (v3.0: tree-sitter + FalkorDBLite) ───
            _notify("index", "Building code graph (tree-sitter)")
            t_idx = progress.add_task("[cyan]Phase 2: Indexing code graph...", total=None)
            try:
                from src.indexing.graph.pipeline import (
                    EmptyGraphRebuild,
                    index_repo_graph,
                )
                # src_subdir left at its default, which is now the WHOLE
                # repository. It used to be "src", and this call — the only
                # one that did not pass the argument — is how a Go+Rust repo
                # lost every Go symbol: the Rust is under src/, the Go is not.
                index_repo_graph(sync_result.path, sync_result.repo_slug, self.settings)
            except EmptyGraphRebuild as exc:
                # The graph was KEPT, so this is recoverable and the build
                # should go on. Logged at error rather than warning because
                # the repository now has documentation written against a
                # graph the run could not reproduce, and somebody has to know.
                logger.error("graph_index_refused repo=%s: %s — kept the "
                             "existing graph and continued",
                             sync_result.repo_slug, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("graph_index_failed: %s — continuing", exc)
            progress.update(t_idx, completed=1)

            # ─── Phase 2b: Semgrep ───────────────────────────────────
            semgrep_report: SemgrepReport | None = None
            if not skip_semgrep:
                _notify("semgrep", "Running SAST scan")
                t_sg = progress.add_task("[cyan]Phase 2b: SAST scan...", total=None)
                try:
                    semgrep_report = self.semgrep.scan(
                        sync_result.path,
                        self.settings.repo_data_path(sync_result.repo_slug),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("semgrep_failed: %s", exc)
                progress.update(t_sg, completed=1)

            # ─── Phase 3: Module discovery ───────────────────────────
            _notify("discover", "Discovering modules")
            t_disc = progress.add_task("[cyan]Phase 3: Discovering modules...", total=None)
            modules = self.discovery.discover(sync_result.path)
            # Discovery is path-based and leaves `symbols=[]`. The graph was
            # built two phases ago and knows exactly what each module declares,
            # so the two are joined here — before any note is written, because
            # the symbol list travels into the note's frontmatter and from
            # there into feature detection. Without it every feature was seeded
            # with an empty symbol list and its note came out with no source
            # code in it at all.
            from src.indexing.modules import enrich_with_graph
            enriched = enrich_with_graph(modules, sync_result.path, self.settings)
            progress.update(t_disc, completed=1)
            console.print(
                f"  Found [bold]{len(modules)}[/bold] modules "
                f"([bold]{enriched}[/bold] symbols)"
            )
            _notify("discover", f"Found {len(modules)} modules, {enriched} symbols")

            result = GenerationResult(
                repo=sync_result.repo_slug,
                commit=sync_result.commit_sha,
                semgrep_findings=len(semgrep_report.findings) if semgrep_report else 0,
            )

            # ─── Phase 4: Module PRDs ────────────────────────────────
            t_mod = progress.add_task(
                f"[cyan]Phase 4: Generating {len(modules)} module PRDs...",
                total=len(modules),
            )
            with self.vault_writer.batched_qdrant():
                for idx, module in enumerate(modules, 1):
                    _checkpoint()
                    _notify("module", f"[{idx}/{len(modules)}] {module.name}")
                    try:
                        r = self.module_gen.generate(
                            repo=sync_result.repo_slug,
                            repo_path=sync_result.path,
                            commit_sha=sync_result.commit_sha,
                            module=module,
                            semgrep=semgrep_report,
                            force=force,
                        )
                        if r.skipped:
                            result.modules_skipped.append(r.module_name)
                            _notify("module", f"  ↺ {module.name} already at commit, skipped")
                        else:
                            result.modules_generated.append(r.module_name)
                            result.total_tokens_in += r.tokens_in
                            result.total_tokens_out += r.tokens_out
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("module_gen_failed module=%s: %s", module.name, exc)
                        result.failures.append(f"module:{module.name}")
                        _note_gen_failure(exc)
                    progress.update(t_mod, advance=1)

            # ─── Phase 4b: Cross_refs computation (computed-fields) ──
            # Cheap graph queries for ALL modules (incl. skipped). Cascading
            # update: modules that were skipped on resume — their
            # computed-fields are updated anyway, because the graph could have
            # changed through other files. A re-embed is triggered only if the
            # computed block actually changed.
            _notify("cross-refs", f"Computing cross_refs for {len(modules)} modules")
            t_xrefs = progress.add_task("[cyan]Phase 4b: Computing cross_refs...", total=None)
            try:
                # Stage 21 — batch Phase 4b's re-embeds too (each changed
                # computed-block used to fire one embed HTTP call).
                with self.vault_writer.batched_qdrant():
                    self._enrich_cross_refs(sync_result.repo_slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cross_refs_phase_failed: %s — continuing", exc)
            progress.update(t_xrefs, completed=1)

            # ─── Phase 5: Features (auto-detected if not provided) ───
            from src.generation.auto_detect import detect_features, detect_integrations
            if features is None:
                _notify("auto-detect", "Auto-detecting features from module notes")
                features = detect_features(sync_result.repo_slug, self.settings)
                console.print(f"  Auto-detected [bold]{len(features)}[/bold] features")
            if features:
                t_feat = progress.add_task(
                    f"[cyan]Phase 5: Generating {len(features)} features...",
                    total=len(features),
                )
                with self.vault_writer.batched_qdrant():
                    for spec in features:
                        _checkpoint()
                        try:
                            self.feature_gen.generate(
                                repo=sync_result.repo_slug,
                                repo_path=sync_result.path,
                                commit_sha=sync_result.commit_sha,
                                feature=spec,
                            )
                            result.features_generated.append(spec.name)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("feature_gen_failed name=%s: %s", spec.name, exc)
                            result.failures.append(f"feature:{spec.name}")
                            _note_gen_failure(exc)
                        progress.update(t_feat, advance=1)

            # ─── Phase 6: Integrations (auto-detected if not provided) ──
            if integrations is None:
                _notify("auto-detect", "Auto-detecting integrations from graph")
                detected = detect_integrations(sync_result.repo_slug, self.settings)
                integrations = [(c.name, c.entry_points) for c in detected]
                console.print(f"  Auto-detected [bold]{len(integrations)}[/bold] integrations")
            if integrations:
                t_int = progress.add_task(
                    f"[cyan]Phase 6: Generating {len(integrations)} integrations...",
                    total=len(integrations),
                )
                with self.vault_writer.batched_qdrant():
                    for service_name, entry_points in integrations:
                        _checkpoint()
                        try:
                            self.integration_gen.generate(
                                repo=sync_result.repo_slug,
                                repo_path=sync_result.path,
                                commit_sha=sync_result.commit_sha,
                                service_name=service_name,
                                entry_points=entry_points,
                            )
                            result.integrations_generated.append(service_name)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("integration_gen_failed name=%s: %s", service_name, exc)
                            result.failures.append(f"integration:{service_name}")
                            _note_gen_failure(exc)
                        progress.update(t_int, advance=1)

            # ─── Phase 7: Write aggregated docs ──────────────────────
            t_agg = progress.add_task("[cyan]Phase 7: Writing architecture + index + security...", total=None)
            self._write_architecture(sync_result, modules, result)
            self._write_index(sync_result, modules, result)
            if semgrep_report:
                self._write_security_summary(sync_result, semgrep_report)
            progress.update(t_agg, completed=1)

        # Carry the vector half out with the text half. Without this the
        # caller sees documents and has no way to learn that none of them was
        # embedded — which is exactly the state three production jobs reported
        # as clean success.
        result.notes_embedded = getattr(self.vault_writer, "qdrant_upserted", 0)
        result.embedding_failures = list(
            getattr(self.vault_writer, "qdrant_failures", []))
        return result

    def _enrich_cross_refs(self, repo_slug: str) -> None:
        """Phase 4b: compute the computed-fields (cross_refs etc) and update the
        frontmatter of all module notes. Mirror of the `analyzer enrich-vault`
        command.

        Called INSIDE the full `generate` flow after Phase 4 (module PRDs), so
        that freshly created notes have cross_refs right away. For modules that
        were skipped because of resume — the computed fields are updated too
        (cascading).
        """
        from src.generation.cross_refs import CrossRefsComputer
        from src.indexing.graph.graph_store import make_graph_store
        from src.vault.reader import VaultReader

        db_path = self.settings.repo_graph_path(repo_slug)
        if not db_path.exists():
            logger.info("cross_refs_skipped no_graph at %s", db_path)
            return

        store = make_graph_store(db_path)
        reader = VaultReader(self.settings)
        try:
            computer = CrossRefsComputer(store, reader, repo_slug)
            all_computed = computer.compute_for_all_modules()
        finally:
            store.close()

        if not all_computed:
            logger.info("cross_refs_no_modules repo=%s", repo_slug)
            return

        updated = noop = failed = 0
        for ref, computed in all_computed.items():
            rel = ref + ".md" if not ref.endswith(".md") else ref
            try:
                changed = self.vault_writer.update_computed_fields(
                    repo=repo_slug,
                    relative_path=rel,
                    computed=computed.as_dict(),
                    index_in_qdrant=True,
                )
                if changed:
                    updated += 1
                else:
                    noop += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("cross_refs_update_failed path=%s err=%s", rel, exc)

        logger.info(
            "cross_refs_phase_done repo=%s updated=%d noop=%d failed=%d total=%d",
            repo_slug, updated, noop, failed, len(all_computed),
        )

    def _write_index(
        self,
        sync: SyncResult,
        modules: list[Module],
        result: GenerationResult,
    ) -> None:
        """Generates index.md for the repo."""
        lines = [f"# {sync.repo_slug}", "", f"> Commit: `{sync.commit_sha[:8]}`  ", ""]
        lines.append(f"## Modules ({len(modules)})\n")
        for m in sorted(modules, key=lambda x: x.name):
            lines.append(f"- [[modules/{m.name}]] — `{m.path}` ({len(m.files)} files)")
        if result.features_generated:
            lines.append(f"\n## Features ({len(result.features_generated)})\n")
            for f in result.features_generated:
                lines.append(f"- [[features/{f}]]")
        if result.integrations_generated:
            lines.append(f"\n## Integrations ({len(result.integrations_generated)})\n")
            for i in result.integrations_generated:
                lines.append(f"- [[integrations/{i}]]")
        lines.append(f"\n## Security\n\n- [[security/findings]] — {result.semgrep_findings} findings")

        self.vault_writer.write(
            repo=sync.repo_slug,
            relative_path="index.md",
            content="\n".join(lines),
            metadata=NoteMetadata(
                type="overview",
                repo=sync.repo_slug,
                commit=sync.commit_sha,
                keywords=["overview", "index", sync.repo_slug],
            ),
            redact_content=False,
        )

    def _write_architecture(
        self,
        sync: SyncResult,
        modules: list[Module],
        result: GenerationResult,
    ) -> None:
        """Architecture overview — graph statistics + module-level imports map.

        Pulls the data straight out of the FalkorDBLite graph: top integration
        points, module dep graph, top files by symbol density.
        """
        from src.indexing.graph.graph_store import make_graph_store

        db_path = self.settings.repo_graph_path(sync.repo_slug)
        if not db_path.exists():
            logger.info("architecture_skipped no_graph at %s", db_path)
            return

        store = make_graph_store(db_path)
        try:
            # Symbols by kind
            symbols_by_kind = store.query(
                "MATCH (s:Symbol) WHERE s.kind <> 'file_module' "
                "RETURN s.kind AS kind, count(*) AS n ORDER BY n DESC"
            )
            edges_by_kind = store.query(
                "MATCH ()-[r]->() RETURN type(r) AS kind, count(r) AS n ORDER BY n DESC"
            )
            top_targets = store.query(
                "MATCH (a:Symbol)-[:IMPORTS]->(b:Symbol) "
                "WHERE b.is_exported = true AND b.kind <> 'file_module' "
                "RETURN b.name AS name, b.kind AS kind, b.file AS file, "
                "       count(a) AS in_deg "
                "ORDER BY in_deg DESC LIMIT 15"
            )
        finally:
            store.close()

        lines = [
            f"# Architecture — {sync.repo_slug}",
            "",
            f"> Commit: `{sync.commit_sha[:8]}` · Modules: {len(modules)} · "
            f"Features: {len(result.features_generated)} · Integrations: {len(result.integrations_generated)}",
            "",
            "## Graph statistics",
            "",
            "### Symbols",
        ]
        for r in symbols_by_kind:
            lines.append(f"- **{r['kind']}**: {r['n']}")
        lines.append("\n### Edges\n")
        for r in edges_by_kind:
            lines.append(f"- **{r['kind']}**: {r['n']}")

        lines.append("\n## Top exported symbols (incoming IMPORTS)\n")
        lines.append("These are candidates for integration documentation — "
                     "the most imported from outside their own module.\n")
        for r in top_targets:
            file = r.get("file", "")
            lines.append(
                f"- **{r['name']}** ({r['kind']}) — `{file}` · "
                f"used by **{r['in_deg']}** symbols"
            )

        lines.append("\n## Modules\n")
        for m in sorted(modules, key=lambda x: -len(x.files))[:30]:
            lines.append(f"- [[modules/{m.name}]] — `{m.path}` ({len(m.files)} files)")

        self.vault_writer.write(
            repo=sync.repo_slug,
            relative_path="architecture.md",
            content="\n".join(lines),
            metadata=NoteMetadata(
                type="overview",
                repo=sync.repo_slug,
                commit=sync.commit_sha,
                keywords=["architecture", "overview", sync.repo_slug],
            ),
            redact_content=False,
        )

    def _write_security_summary(self, sync: SyncResult, report: SemgrepReport) -> None:
        counts = report.count_by_severity()
        lines = [
            "# Security Findings",
            "",
            f"> Generated {sync.commit_sha[:8]}  ",
            f"> Total findings: **{len(report.findings)}**",
            "",
            "## By Severity\n",
        ]
        for sev, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{sev}**: {n}")
        lines.append("\n## Details\n")
        for f in sorted(report.findings, key=lambda x: (x.severity, x.file, x.line)):
            lines.append(
                f"- `{f.rule_id}` ({f.severity}) — "
                f"[{f.file}:{f.line}]({f.file}#L{f.line})  \n  {f.message}"
            )

        self.vault_writer.write(
            repo=sync.repo_slug,
            relative_path="security/findings.md",
            content="\n".join(lines),
            metadata=NoteMetadata(
                type="security",
                repo=sync.repo_slug,
                commit=sync.commit_sha,
                keywords=["security", "findings", "sast", "semgrep"],
                security_findings=len(report.findings),
            ),
            redact_content=False,
        )
