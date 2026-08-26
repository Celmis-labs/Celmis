"""Q&A Orchestrator — combines the router, tiers 1-3, and Gemini synthesis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings
from src.llm.prompts import (
    BA_ANSWER_PROMPT,
    BA_ANSWER_SYSTEM,
    OVERVIEW_PROMPT,
    OVERVIEW_SYSTEM,
    TECHNICAL_ANSWER_PROMPT,
    TECHNICAL_ANSWER_SYSTEM,
)
from src.qa.classifier import QuestionType, classify
from src.qa.router import QueryRouter, RouteDecision, RoutePath
from src.retrieval.tier1_vault import VaultHit, VaultRetriever
from src.retrieval.tier2_graph import (
    GraphExpansion,
    GraphRetriever,  # type alias for compatibility
)
from src.retrieval.tier3_code import CodeBundle, CodeReader

logger = logging.getLogger(__name__)


def _format_history(
    history: list[dict] | None,
    max_exchanges: int = 3,
    max_chars_per_msg: int = 800,
) -> str:
    """Formats history into text for the prompt. We take the last N exchanges so
    as not to bloat it.
    """
    if not history:
        # English, because this goes INTO the prompt: it fills the {history}
        # slot the model reads. A Ukrainian sentence sitting inside an
        # otherwise-English prompt is a nudge toward answering in Ukrainian,
        # which is the opposite of "answer in the language of the question" —
        # and it would land on the very first message of every chat.
        return "(first message in this chat — no previous context)"
    # 1 exchange = user + assistant = 2 messages
    relevant = history[-(max_exchanges * 2):]
    parts: list[str] = []
    for msg in relevant:
        role = str(msg.get("role", "")).upper()
        content = str(msg.get("content", ""))
        if len(content) > max_chars_per_msg:
            content = content[:max_chars_per_msg] + "… [truncated]"
        parts.append(f"{role}:\n{content}")
    return "\n\n---\n\n".join(parts)


@dataclass
class QAAnswer:
    """An answer from the Q&A engine."""

    question: str
    question_type: str
    route: str
    text: str  # Markdown
    vault_hits: list[VaultHit] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    answer_mode: str = "tech"  # "tech" | "ba" — which prompt was used

    def as_markdown(self) -> str:
        """Full output with metadata."""
        parts = [self.text, "", "---", "", f"**Type:** {self.question_type} · **Route:** {self.route}"]
        if self.vault_hits:
            parts.append("\n**Vault context used:**")
            for h in self.vault_hits:
                parts.append(f"- [[{h.note_path}]] (score {h.score:.3f})")
        if self.files_read:
            parts.append("\n**Files read:**")
            for f in self.files_read:
                parts.append(f"- `{f}`")
        return "\n".join(parts)


class QAOrchestrator:
    """Full Q&A flow: route → retrieve → synthesize → format."""

    def __init__(
        self,
        settings: Settings | None = None,
        vault_ret: VaultRetriever | None = None,
        graph_ret: GraphRetriever | None = None,
        code_reader: CodeReader | None = None,
        router: QueryRouter | None = None,
        workspace_id: str = "default",
        user_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # Answers leave through the LiteLLM gateway like every other surface:
        # a workspace's own provider and its virtual key apply to Q&A too, and
        # its spend lands where the rest of it does. This used to construct
        # get_gemini_client() and talk to Google directly.
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.vault_ret = vault_ret or VaultRetriever(
            self.settings, workspace_id=self.workspace_id,
        )
        # v3.0: GraphRetriever — own FalkorDBLiteStore backend, without CGC
        #: Set when Tier 1 could not answer — a CODE the UI turns into a
        #: sentence, never a Qdrant response body. Mirrors the field the
        #: multi-repo retriever has carried since it was written; this path had
        #: no equivalent and simply raised.
        self.vault_unavailable: str | None = None
        self.graph_ret = graph_ret or GraphRetriever(self.settings)
        self.code_reader = code_reader or CodeReader(self.settings)
        self.router = router or QueryRouter()


    def _answer_model(self, _agent: str | None = None) -> str | None:
        from src.llm.profiles import resolve_profile
        try:
            return resolve_profile("chat", getattr(self, "workspace_id", "default")).model
        except Exception:  # noqa: BLE001
            return None

    def _generate(self, **kw):
        """One prompt through the gateway.

        `mode`, `question` and `files_sent` were Gemini-client telemetry; the
        shared client takes `operation` and carries the rest itself.
        """
        from src.llm.client import build_llm_client

        kw.pop("mode", None)
        kw.pop("question", None)
        kw.pop("files_sent", None)
        client = build_llm_client(
            getattr(self, "user_id", None) or "system",
            getattr(self, "workspace_id", "default"),
            surface="chat",
            spend_surface="qa",
            resolve_model=self._answer_model,
        )
        return client.generate(agent="qa", mode="qa", **kw)

    def ask(
        self,
        *,
        question: str,
        repo: str,
        history: list[dict] | None = None,
        progress_callback: Any = None,
        answer_mode: str = "tech",
    ) -> QAAnswer:
        """The main method — question → answer.

        history: list of previous messages in the format
                 [{"role": "user"|"assistant", "content": str}, ...]
                 Used for follow-up questions in a chat session.

        progress_callback: optional `Callable[[ExplorationEvent], None]`.
                           Called with events at every retrieval phase —
                           for live UI progress (Streamlit placeholder, CLI verbose).

        answer_mode: "tech" (default) — Technical Research with code blocks and file:line refs.
                     "ba" — Business Analyst — a narrative workflow with a concrete example,
                     without code, for non-developers. Retrieval is the same, the prompt differs.
        """
        if answer_mode not in ("tech", "ba"):
            answer_mode = "tech"
        self._current_answer_mode = answer_mode  # read in _synthesize_technical
        decision = self.router.route(question)
        qtype = classify(question)
        repo_path = self.settings.repo_path(repo)

        logger.info(
            "qa_ask q_hash=%s type=%s route=%s symbols=%s files=%s history_len=%d",
            hash(question) & 0xFFFF,
            qtype.value,
            decision.path.value,
            decision.symbols,
            decision.files,
            len(history or []),
        )

        # Notify the UI at the start (for all routes — so the user sees it began)
        if progress_callback:
            try:
                from src.qa.exploration_agent import ExplorationEvent
                progress_callback(ExplorationEvent(
                    kind="phase_start",
                    label=f"📚 Vault search · type={qtype.value} · route={decision.path.value}",
                ))
            except Exception:  # noqa: BLE001
                pass

        if decision.path == RoutePath.EXACT_SYMBOL:
            return self._answer_exact_symbol(question, qtype, decision, repo, repo_path, history)
        if decision.path == RoutePath.FILE_PATH:
            return self._answer_file_path(question, qtype, decision, repo, repo_path, history)
        return self._answer_natural(
            question, qtype, decision, repo, repo_path, history,
            progress_callback=progress_callback,
        )

    # ─── Path A: Exact symbol ────────────────────────────────────────
    def _answer_exact_symbol(
        self,
        question: str,
        qtype: QuestionType,
        decision: RouteDecision,
        repo: str,
        repo_path: Path,
        history: list[dict] | None,
    ) -> QAAnswer:
        symbols = list(decision.symbols)
        graph_exp = self.graph_ret.expand(repo_path, symbols)

        # Fuzzy fallback: if the exact-name match returned nothing
        # (for example, the question is about "submitOrder" but the symbol is
        # called "useSubmitOrder"), we do a CONTAINS search over the graph.
        if not graph_exp.roots:
            fuzzy = self._fuzzy_symbol_candidates(repo_path, symbols)
            if fuzzy:
                graph_exp = self.graph_ret.expand(repo_path, fuzzy)
                logger.info("exact_symbol_fuzzy_fallback orig=%s resolved=%s",
                            symbols, fuzzy)

        vault_hits = self._fetch_vault_for_symbols(decision.symbols, repo, top_k=3)
        code_bundle = self._read_code_for(repo_path, graph_exp, vault_hits, question=question)
        return self._synthesize_technical(
            question, qtype, decision, vault_hits, graph_exp, code_bundle, history,
            repo=repo,
        )

    def _resolve_file_paths(
        self, repo_path: Path, files: list[str], max_matches_per_query: int = 5,
    ) -> list[str]:
        """Resolves decision.files to rel-paths that exist in the repo.

        If a base name is passed (`Order.ts`) — we search the graph by
        `s.file ENDS WITH '/Order.ts'`. This covers the case where the
        router extracted only the basename from the question, without a
        path. If a relative path is passed and it exists — we return it as is.
        """
        if not files:
            return []
        out: list[str] = []
        seen: set[str] = set()
        try:
            store = self.graph_ret._store_for(repo_path)
        except Exception:  # noqa: BLE001
            store = None

        for f in files:
            if not f:
                continue
            # A directly existing rel path?
            if (repo_path / f).is_file():
                if f not in seen:
                    seen.add(f)
                    out.append(f)
                continue

            if store is None:
                continue

            # basename → we search the graph by file_module
            try:
                rows = store.query(
                    "MATCH (s:Symbol {kind: 'file_module'}) "
                    "WHERE s.file ENDS WITH $tail OR s.file = $exact "
                    "RETURN DISTINCT s.file AS file LIMIT $lim",
                    params={"tail": "/" + f, "exact": f, "lim": max_matches_per_query},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("file_resolve_query_failed needle=%s err=%s", f, exc)
                continue
            for r in rows:
                rf = str(r.get("file", ""))
                if rf and rf not in seen:
                    seen.add(rf)
                    out.append(rf)
        return out

    def _fuzzy_symbol_candidates(
        self, repo_path: Path, symbols: list[str], limit_per_symbol: int = 3,
    ) -> list[str]:
        """CONTAINS-search over the graph — for the cases where the router gives
        'submitOrder' but the real name is 'useSubmitOrder'/'submitOrderHandler'/etc.
        """
        if not symbols:
            return []
        try:
            store = self.graph_ret._store_for(repo_path)
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        seen: set[str] = set()
        for s in symbols:
            if not s or len(s) < 3:
                continue
            try:
                rows = store.query(
                    "MATCH (sym:Symbol) "
                    "WHERE sym.kind <> 'file_module' "
                    "  AND toLower(sym.name) CONTAINS toLower($needle) "
                    "RETURN sym.name AS name, sym.kind AS kind "
                    "LIMIT $lim",
                    params={"needle": s, "lim": limit_per_symbol},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("fuzzy_query_failed needle=%s err=%s", s, exc)
                continue
            for r in rows:
                n = str(r.get("name", ""))
                if n and n not in seen:
                    seen.add(n)
                    out.append(n)
        return out

    # ─── Path B: File / path mentioned ───────────────────────────────
    def _answer_file_path(
        self,
        question: str,
        qtype: QuestionType,
        decision: RouteDecision,
        repo: str,
        repo_path: Path,
        history: list[dict] | None,
    ) -> QAAnswer:
        # Read every mentioned file in full (until the shared budget runs out)
        from src.retrieval.tier3_code import CodeBundle as _CB

        bundle = _CB(budget=self.settings.max_code_tokens_per_qa)
        resolved_files = self._resolve_file_paths(repo_path, decision.files)
        for file_rel in resolved_files:
            snippet = self.code_reader.read_full_file(repo_path, file_rel, redact_content=True)
            if snippet is None:
                continue
            if bundle.total_tokens + snippet.tokens > bundle.budget:
                bundle.truncated = True
                break
            bundle.snippets.append(snippet)
            bundle.total_tokens += snippet.tokens

        # Additionally: graph context for the symbols in these files.
        # v3.0: we pull the seed symbols through FalkorDBLiteStore — a Cypher
        # query by file=path → top-10 symbols.
        seed_symbols: list[str] = []
        try:
            store = self.graph_ret._store_for(repo_path)
            for file_rel in resolved_files or decision.files:
                rows = store.query(
                    "MATCH (s:Symbol) WHERE s.file = $f AND s.kind <> 'file_module' "
                    "RETURN s.name AS name LIMIT 10",
                    params={"f": file_rel},
                )
                for r in rows:
                    name = r.get("name")
                    if name:
                        seed_symbols.append(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("file_path_seeds_failed err=%s", exc)
        graph_exp = self.graph_ret.expand(repo_path, seed_symbols[:10]) if seed_symbols else GraphExpansion()

        vault_hits = self._vault_search(question, repo=repo, top_k=3)
        return self._synthesize_technical(
            question, qtype, decision, vault_hits, graph_exp, bundle, history,
            repo=repo,
        )

    # ─── Path C: Natural language ────────────────────────────────────
    def _answer_natural(
        self,
        question: str,
        qtype: QuestionType,
        decision: RouteDecision,
        repo: str,
        repo_path: Path,
        history: list[dict] | None,
        progress_callback: Any = None,
    ) -> QAAnswer:
        # Layer 1: vault discovery (semantic search over module notes)
        vault_hits = self._vault_search(question, repo=repo)

        # Overview/navigation — we stop at the vault narrative, without code
        if qtype in (QuestionType.OVERVIEW, QuestionType.NAVIGATION) and vault_hits:
            return self._synthesize_overview(
                question, qtype, decision, vault_hits, history, repo=repo)

        needs_code = qtype in (
            QuestionType.TECHNICAL,
            QuestionType.FUNCTIONAL,
            QuestionType.INTEGRATION,
        )

        # Layer 2: ExplorationAgent (subagent loop on Gemini Flash Lite)
        # for FUNCTIONAL/INTEGRATION/TECHNICAL — LLM-driven exploration
        # picks 8-15 key function bodies via graph + vault tools.
        # Falls back to the static vault-hits → graph query when the subagent
        # is unavailable or returns an empty bundle.
        graph_exp = GraphExpansion()
        code_bundle = None

        if needs_code and vault_hits:
            try:
                from src.qa.exploration_agent import ExplorationAgent

                store = self.graph_ret._store_for(repo_path)
                agent = ExplorationAgent(
                    store=store,
                    repo=repo,
                    repo_path=repo_path,
                    # The exploration agent builds its own client: it uses
                    # Gemini's native function-calling loop, which is not the
                    # same shape as a single completion and does not survive
                    # being routed through the gateway unchanged.
                    code_reader=self.code_reader,
                    settings=self.settings,
                    progress_callback=progress_callback,
                )
                exp_result = agent.run(question=question, vault_hits=vault_hits)
                code_bundle = exp_result.code_bundle
                logger.info(
                    "subagent_done turns=%d tool_calls=%d selected=%d bodies=%d "
                    "tokens=%d/%d reason=%s",
                    exp_result.turns_used,
                    exp_result.tool_calls_used,
                    len(exp_result.selected_symbols),
                    len(code_bundle.snippets) if code_bundle else 0,
                    exp_result.tokens_in,
                    exp_result.tokens_out,
                    exp_result.terminated_reason,
                )
                # If the subagent gave an empty bundle — fall back to the static path
                if code_bundle is None or not code_bundle.snippets:
                    logger.info("subagent_empty_bundle_fallback_to_static")
                    code_bundle = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("subagent_failed err=%s — fallback to static", exc)
                code_bundle = None

        # Static fallback: vault hits → cross_refs → graph query (without keywords).
        # Used for overview/navigation with a code part, or when the
        # ExplorationAgent failed.
        if code_bundle is None:
            seeds: list[str] = []
            for h in vault_hits:
                seeds.extend(h.symbols[:5])
            if decision.symbols:
                seeds.extend(decision.symbols)
            seeds = list(dict.fromkeys(seeds))[:15]
            graph_exp = self.graph_ret.expand(repo_path, seeds) if seeds else GraphExpansion()
            if needs_code:
                code_bundle = self._read_code_for(repo_path, graph_exp, vault_hits)
            else:
                from src.retrieval.tier3_code import CodeBundle as _CB

                code_bundle = _CB(budget=0)

        if progress_callback:
            try:
                from src.qa.exploration_agent import ExplorationEvent
                progress_callback(ExplorationEvent(
                    kind="phase_start",
                    label=f"✍️  Synthesizing answer ({self.settings.gemini_generation_model})",
                    detail=f"{len(code_bundle.snippets) if code_bundle else 0} snippets in bundle",
                ))
            except Exception:  # noqa: BLE001
                pass

        return self._synthesize_technical(
            question, qtype, decision, vault_hits, graph_exp, code_bundle, history,
            repo=repo,
        )

    # ─── Helpers ─────────────────────────────────────────────────────
    def _read_code_for(
        self,
        repo_path: Path,
        graph_exp: GraphExpansion,
        vault_hits: list[VaultHit] | None = None,
    ) -> CodeBundle:
        locations = graph_exp.all_file_locations()

        # Fallback: if the graph is empty — we read files from the module
        # paths of the vault hits
        if not locations and vault_hits:
            locations = self._locations_from_vault_hits(repo_path, vault_hits)

        return self.code_reader.read_locations(
            repo_path,
            locations,
            budget_tokens=self.settings.max_code_tokens_per_qa,
            context_lines=3,
            redact_content=True,
        )

    def _locations_from_vault_hits(
        self,
        repo_path: Path,
        vault_hits: list[VaultHit],
    ) -> list[tuple[str, int, int | None]]:
        """Collects the real (file, start_line, end_line) for the modules from vault hits.

        Strategy:
        1) Collect candidate prefixes from the vault hits (`path` from the
           frontmatter, or resolved `cross_refs`).
        2) For each prefix ask the GRAPH: all symbols that live in files
           under that path. This gives real start_line/end_line — not "(file, 1, None)",
           which previously only caught the imports in `_detect_block_end`.
        3) We sort by kind priority (function/method/class before variable)
           and cap at `max_locations`.
        4) If the graph returned nothing for a prefix (unparsed / Vue without
           a script block) — fall back to whole-file locations; tier3 with our
           fix in `_detect_block_end` will skip the imports and find the first
           body.
        """
        from src.indexing.modules import _IGNORE_DIRS, _is_non_source_file
        from src.vault.reader import VaultReader

        max_locations = 80  # cap, so as not to pour hundreds of symbols into tier3
        kind_priority = {
            "function": 0,
            "method": 1,
            "class": 2,
            "export_default": 3,
            "variable": 4,
        }

        reader = VaultReader(self.settings)

        # 1) Collect the prefixes from the vault hits
        prefixes: list[str] = []
        seen_prefixes: set[str] = set()
        resolved_module_paths: dict[str, str | None] = {}

        def _resolve_module_path(repo: str, ref: str) -> str | None:
            if ref in resolved_module_paths:
                return resolved_module_paths[ref]
            note_rel = ref if ref.endswith(".md") else f"{ref}.md"
            mod_note = reader.read(repo, note_rel)
            path = mod_note.metadata.get("path") if mod_note else None
            resolved_module_paths[ref] = path
            return path

        def _add_prefix(p: str | None) -> None:
            if not p or p in seen_prefixes:
                return
            seen_prefixes.add(p)
            prefixes.append(p)

        # 1a) Direct prefixes from the vault hits + 1-hop cross_refs
        first_hop_refs: list[tuple[str, str]] = []  # (repo, ref) for transitive expand
        for h in vault_hits:
            if h.type == "module" and h.path:
                _add_prefix(h.path)
            if h.cross_refs:
                for ref in h.cross_refs:
                    if not ref.startswith("modules/"):
                        continue
                    _add_prefix(_resolve_module_path(h.repo, ref))
                    first_hop_refs.append((h.repo, ref))

        # 1b) Transitive cross_refs (2nd hop): for every cross_ref module from
        # a vault hit — read its frontmatter and add ITS cross_refs.
        # Capped at TRANSITIVE_PER_HIT so the prefix set does not blow up.
        # Useful when the vault hits show module A, which is linked to B, but
        # the real logic lives in C (B→C via B's cross_refs). Without this the
        # bundle covers at most 2 layers of the flow; with it — 3.
        TRANSITIVE_PER_HIT = 4
        for repo, ref in first_hop_refs[:6]:
            note_rel = ref if ref.endswith(".md") else f"{ref}.md"
            mod_note = reader.read(repo, note_rel)
            if not mod_note:
                continue
            for sub_ref in (mod_note.metadata.get("cross_refs", []) or [])[:TRANSITIVE_PER_HIT]:
                if not isinstance(sub_ref, str) or not sub_ref.startswith("modules/"):
                    continue
                _add_prefix(_resolve_module_path(repo, sub_ref))

        if not prefixes:
            return []

        # 2) Query the graph — real symbols with end_line
        try:
            store = self.graph_ret._store_for(repo_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_store_open_failed err=%s", exc)
            store = None

        locations: list[tuple[str, int, int | None]] = []
        seen_keys: set[tuple[str, int]] = set()

        # Round-robin per-prefix: so that a large prefix (for example
        # `models/shared` with 60+ files) does NOT eat the whole budget and
        # leave the cross-feature prefixes (Pricing/Helpers/Data,
        # API/services) without their share.
        # IMPORTANT: this path is a fallback for the cases where the
        # ExplorationAgent is not used (for example route=A/B or the subagent
        # failed). For FUNCTIONAL/INTEGRATION natural-language questions the
        # ExplorationAgent does a completely different retrieval (LLM-driven
        # exploration).
        PER_PREFIX_CAP = 8
        per_prefix: dict[str, list[tuple[str, int, int | None]]] = {}

        if store is not None:
            for prefix in prefixes:
                norm = prefix.rstrip("/") + "/"
                try:
                    rows = store.query(
                        "MATCH (s:Symbol) "
                        "WHERE s.kind <> 'file_module' "
                        "  AND (s.file STARTS WITH $prefix OR s.file = $exact) "
                        "RETURN s.file AS file, s.start_line AS sl, s.end_line AS el, "
                        "       s.kind AS kind, s.name AS name "
                        "LIMIT 300",
                        params={"prefix": norm, "exact": prefix},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("graph_prefix_query_failed prefix=%s err=%s", prefix, exc)
                    rows = []

                # Sorting:
                # 1) kind_priority (function/method/class first)
                # 2) file path (stability)
                # 3) start_line
                rows.sort(key=lambda r: (
                    kind_priority.get(str(r.get("kind", "")), 9),
                    str(r.get("file", "")),
                    int(r.get("sl") or 0),
                ))

                # Bucket: we dedupe per file — take top-2 symbols per file, so
                # that a large file (FieldHelper with 50 methods) does not
                # take the whole bucket, but does not disappear either. Up to
                # PER_PREFIX_CAP unique files.
                bucket: list[tuple[str, int, int | None]] = []
                file_count: dict[str, int] = {}
                bucket_seen: set[tuple[str, int]] = set()
                MAX_PER_FILE = 2
                for r in rows:
                    f = str(r.get("file", ""))
                    sl = int(r.get("sl") or 0)
                    if not f or sl <= 0:
                        continue
                    key = (f, sl)
                    if key in bucket_seen:
                        continue
                    if file_count.get(f, 0) >= MAX_PER_FILE:
                        continue
                    if f not in file_count and len(file_count) >= PER_PREFIX_CAP:
                        # already PER_PREFIX_CAP unique files — we skip new ones
                        continue
                    bucket_seen.add(key)
                    file_count[f] = file_count.get(f, 0) + 1
                    el = r.get("el")
                    bucket.append((f, sl, int(el) if el else None))
                per_prefix[prefix] = bucket

            # Round-robin merge
            i = 0
            while len(locations) < max_locations:
                added = 0
                for prefix in prefixes:
                    bucket = per_prefix.get(prefix, [])
                    if i >= len(bucket):
                        continue
                    loc = bucket[i]
                    key = (loc[0], loc[1])
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    locations.append(loc)
                    added += 1
                    if len(locations) >= max_locations:
                        break
                if added == 0:
                    break
                i += 1

        # 3) If the graph stays silent for the prefixes — fall back to files
        #    (whole-file read via tier3 _detect_block_end, which now skips imports).
        if not locations:
            extensions = set(self.settings.supported_extensions)
            seen_files: set[str] = set()
            for prefix in prefixes:
                module_dir = repo_path / prefix
                if not module_dir.exists() or not module_dir.is_dir():
                    continue
                files = [
                    p for p in module_dir.rglob("*")
                    if p.is_file()
                    and p.suffix in extensions
                    and not any(part in _IGNORE_DIRS for part in p.parts)
                    and not _is_non_source_file(p)
                ]
                files.sort(key=lambda p: p.stat().st_size)
                for p in files[:20]:
                    rel = str(p.relative_to(repo_path))
                    if rel in seen_files:
                        continue
                    seen_files.add(rel)
                    locations.append((rel, 1, None))

        logger.info(
            "qa_vault_fallback_locations count=%d prefixes=%d module_refs_resolved=%d",
            len(locations),
            len(prefixes),
            len(resolved_module_paths),
        )
        return locations

    def _vault_search(self, question: str, *, repo: str,
                      top_k: int | None = None) -> list[VaultHit]:
        """Tier 1, degrading.

        The vault is an ACCELERATOR: retrieval already falls back to grep +
        graph + source, and the multi-repo retriever has degraded on a Qdrant
        failure since it was written. This path did not — the three call sites
        below let the exception out, so a workspace whose vault had never been
        generated got a 500 from chat rather than a slightly thinner answer.
        `vault_unavailable` carries a CODE the UI turns into a sentence; the
        raw Qdrant body names an internal collection and reads as a crash.
        """
        try:
            return self.vault_ret.search(question, repo=repo, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            from src.llm.errors import classify_vector_store
            self.vault_unavailable = classify_vector_store(exc).code
            logger.warning("vault_search_failed repo=%s code=%s err=%s",
                           repo, self.vault_unavailable, str(exc)[:200])
            return []

    def _fetch_vault_for_symbols(self, symbols: list[str], repo: str, top_k: int = 3) -> list[VaultHit]:
        if not symbols:
            return []
        # We build a natural-language search out of the symbols
        query = "Implementation of " + ", ".join(symbols[:5])
        return self._vault_search(query, repo=repo, top_k=top_k)

    # ─── Synthesis ───────────────────────────────────────────────────
    def _synthesize_overview(
        self,
        question: str,
        qtype: QuestionType,
        decision: RouteDecision,
        vault_hits: list[VaultHit],
        history: list[dict] | None,
        repo: str | None = None,
    ) -> QAAnswer:
        notes_block = "\n\n".join(
            f"### {h.note_path} (score {h.score:.2f})\n{h.content[:1500]}"
            for h in vault_hits
        )
        prompt = OVERVIEW_PROMPT.format(
            question=question,
            notes=notes_block,
            history=_format_history(history),
        )
        result = self._generate(
            prompt=prompt,
            mode="qa",
            operation="answer_overview",
            question=question,
            # The ledger's "by repository" breakdown showed documentation and
            # nothing else, because every answer here was recorded without
            # one. The value was in scope three frames up the whole time.
            repo=repo,
            system_instruction=OVERVIEW_SYSTEM,
        )
        return QAAnswer(
            question=question,
            question_type=qtype.value,
            route=decision.path.value,
            text=result.text,
            vault_hits=vault_hits,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
        )

    def _synthesize_technical(
        self,
        question: str,
        qtype: QuestionType,
        decision: RouteDecision,
        vault_hits: list[VaultHit],
        graph_exp: GraphExpansion,
        code_bundle: CodeBundle,
        history: list[dict] | None,
        repo: str | None = None,
    ) -> QAAnswer:
        vault_context = "\n\n".join(
            f"- [[{h.note_path}]]: {h.content[:400]}" for h in vault_hits
        ) or "(no vault notes matched)"

        graph_context = str(graph_exp.as_llm_context()) if graph_exp.all_symbols else "(no graph context)"
        # An empty code section reads to the model as "the user attached
        # nothing", and it answers by asking for the code back — in a product
        # where the code is on the server. Name the real cause and the fix.
        no_code_notice = ""
        if code_bundle.snippets:
            code_md = code_bundle.as_markdown()
        else:
            from src.qa.multi_repo_retriever import (
                _NO_CODE_MARKER,
                MultiRepoRetriever,
            )
            code_md = _NO_CODE_MARKER
            no_code_notice = MultiRepoRetriever._build_no_code_notice(
                vault_missing=not vault_hits,
            )

        # Mode selection: tech (default) — a code-heavy technical answer,
        # ba — a narrative workflow for non-developers. Retrieval is the same,
        # only the prompt + system instruction differ.
        mode = getattr(self, "_current_answer_mode", "tech")
        if mode == "ba":
            prompt_template = BA_ANSWER_PROMPT
            system_inst = BA_ANSWER_SYSTEM
            operation_name = "answer_ba"
        else:
            prompt_template = TECHNICAL_ANSWER_PROMPT
            system_inst = TECHNICAL_ANSWER_SYSTEM
            operation_name = "answer_technical"

        prompt = prompt_template.format(
            question=question,
            vault_context=vault_context,
            graph_context=graph_context,
            code_bundle=code_md,
            history=_format_history(history),
        )
        if no_code_notice:
            prompt = no_code_notice + "\n\n" + prompt

        result = self._generate(
            prompt=prompt,
            mode="qa",
            operation=operation_name,
            question=question,
            files_sent=code_bundle.files_included() if code_bundle else [],
            repo=repo,
            system_instruction=system_inst,
        )
        return QAAnswer(
            question=question,
            question_type=qtype.value,
            route=decision.path.value,
            text=result.text,
            vault_hits=vault_hits,
            files_read=code_bundle.files_included() if code_bundle else [],
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            answer_mode=mode,
        )
