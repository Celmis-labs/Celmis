"""ExplorationAgent — Layer 2 retrieval: subagent loop on Gemini Flash Lite.

Architecture:
  Layer 1 (vault discovery) → set of vault hits + cross_refs
  Layer 2 (THIS) → LLM-driven exploration via FalkorDB + vault + disk tools
                  → curated 10-15 function bodies + research notes
  Layer 3 (synthesis) → main Gemini Pro with a clean bundle

Why a subagent loop and not static retrieval:
  - Adaptive unfolding of the flow across the graph (CALLS/IMPORTS) driven by
    the LLM, not by hardcoded archetypes or keyword scoring.
  - Multi-language native (the toolset is universal, the LLM knows synonyms).
  - Setters/getters do not get lost (the LLM reads the signature through
    read_function).
  - The vault narrative gives intent context that symbol-name embeddings do
    not give.

Tools (read-only, all through the existing GraphStore + VaultReader + disk):
  1. search_symbols(name_pattern, kind?, limit?)
  2. list_module_symbols(module_path, kind?, limit?)
  3. read_function(file, name | symbol_id) → body
  4. find_callers(symbol_name, limit?)
  5. find_callees(symbol_name, limit?)
  6. read_vault_note(note_path) → the full note
  7. grep_in_file(file, pattern, max_matches?) — for switch/dispatch
  8. submit_research(rationale, selected_symbols) — termination signal

Loop terminates on:
  - submit_research() emit
  - subagent_max_turns
  - subagent_max_tool_calls
  - consecutive empty turns (no tool_calls AND no text)

PR-review note: the same subagent-loop pattern can be used here for PR-impact
analysis. The tools get re-adapted to symbol level with the diff as the seed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.genai import types

from src.config import Settings, get_settings
from src.indexing.graph.graph_store import GraphStore
from src.llm.gemini_client import GeminiClient, get_gemini_client
from src.retrieval.tier1_vault import VaultHit
from src.retrieval.tier3_code import CodeBundle, CodeReader, CodeSnippet
from src.vault.reader import VaultReader

logger = logging.getLogger(__name__)


# ───────────────────────── Progress events ─────────────────────────


@dataclass
class ExplorationEvent:
    """Live event for UI streaming. Emitted by ExplorationAgent via progress_callback.

    `kind` — a standardized type, lets the UI style it:
      - "phase_start"   — top-level phase (vault search, subagent loop, synthesis)
      - "turn_start"    — start of a subagent turn
      - "tool_call"     — a concrete tool emit (file/symbol context)
      - "tool_done"     — after execution (with a brief — count results, body lines)
      - "submit"        — submit_research received (final selection)
      - "phase_end"     — end of a phase (with summary metrics)
      - "warning"       — non-fatal: rejected submit, dedup, error from a tool
    """

    kind: str
    label: str               # short readable form for the UI
    detail: str | None = None
    turn: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[ExplorationEvent], None]


SUBAGENT_SYSTEM = """You are a code exploration subagent for a code Q&A system.

YOUR TASK: find **AT LEAST 10, ideally 12-15 key function/method bodies** \
that cover the ENTIRE flow from UI entry-point → orchestrator → workers → boundary (HTTP/DB/IO) → \
reactive consumer. You only SELECT symbols. Another agent writes the answer.

CRITICAL:
- DO NOT CALL submit_research until you have read ≥10 functions via read_function.
- Otherwise the bundle will be incomplete and the answer bad.
- Better to do 12 turns than to submit 4 functions.

ALGORITHM (be aggressive, parallel tool calls are mandatory):

Turn 1 (orientation): 3-5 PARALLEL calls:
  - read_vault_note for the top-2 vault hits (you get narrative + cross_refs)
  - list_module_symbols for the top-1-2 modules from the starting context
  - search_symbols by a keyword from the question if you know one

Turn 2-3 (entry+orchestrator): 4-6 PARALLEL calls:
  - read_function for all entry-points found (UI handlers, public API methods)
  - read_function for orchestrators (methods that delegate the work)
  - find_callees for top orchestrators (to find what THEY call)

Turn 4-6 (deep dive): 4-6 PARALLEL calls per turn:
  - read_function for the callees found on the previous turn
  - find_callers for confirmed core functions (who triggers them?)
  - read_function for the boundary calls discovered (HTTP services, DB access)
  - search_symbols if the graph missed an obvious function by name

Turn 7-10 (gap filling): top up to ≥10 read functions:
  - read_function for any related symbols from other cross_refs modules
  - grep_in_file if you need to find switch/dispatch handlers

LAST turn: MANDATORY `submit_research` with 10-15 symbols (READ via read_function).

RULES:
- 2-5 tool calls PER TURN (in parallel). NEVER 1 call per turn — that is a failure.
- Do not repeat identical calls — it wastes budget.
- Every submitted symbol must have been read BEFOREHAND via read_function.
- Skip test/mock/utility functions if they are not the essence of the question.
- Cross-component flow is usually spread over 4-6 modules — do not stop at one.
"""


# ───────────────────────── Output types ─────────────────────────


@dataclass
class SelectedSymbol:
    """One symbol picked by the subagent for the bundle."""

    name: str
    file: str
    rationale: str  # 1-2 sentences on why
    start_line: int | None = None
    end_line: int | None = None
    kind: str | None = None


@dataclass
class ExplorationResult:
    """Result of the subagent loop — input for the Layer 3 synthesis."""

    selected_symbols: list[SelectedSymbol] = field(default_factory=list)
    research_notes: str = ""  # narrative summary from submit_research
    code_bundle: CodeBundle | None = None  # bodies actually read, after redaction
    turns_used: int = 0
    tool_calls_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    terminated_reason: str = ""  # "submit_research" | "max_turns" | "max_tool_calls" | "stalled"


# ───────────────────────── Tool declarations ─────────────────────────


def _build_tool_declarations() -> list[types.Tool]:
    """Gemini FunctionDeclarations for the toolset.

    Returns one Tool with 8 functions inside — Gemini recommends grouping
    related tools into a single Tool. parametersJsonSchema is a JSON-Schema dict.
    """
    decls = [
        types.FunctionDeclaration(
            name="search_symbols",
            description=(
                "Searches symbols (functions/methods/classes/variables) in the graph by case-insensitive "
                "substring match on the name. Useful when the LLM knows the name (for example 'requestQuote') "
                "but does not know which module it is in. Returns up to `limit` candidates with file path and signature."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "Substring of the symbol name (case-insensitive)"},
                    "kind": {
                        "type": "string",
                        "description": "Optional: filter by kind (function|method|class|variable|export_default)",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 10, max 30)"},
                },
                "required": ["name_pattern"],
            },
        ),
        types.FunctionDeclaration(
            name="list_module_symbols",
            description=(
                "Lists symbols in a module (directory). Useful when a vault hit pointed at a module "
                "and you need to see which functions/classes are there. Returns symbols with kind, file, signature."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "module_path": {"type": "string", "description": "Relative path (e.g. 'src/services/pricing')"},
                    "kind": {"type": "string", "description": "Optional: filter by kind"},
                    "limit": {"type": "integer", "description": "Max results (default 30, max 100)"},
                },
                "required": ["module_path"],
            },
        ),
        types.FunctionDeclaration(
            name="read_function",
            description=(
                "Reads the body of a function/method/class by name + file. AT THE SAME TIME adds it to the bundle "
                "that will be passed to the synthesis agent. Call it for EVERY function you want in the final "
                "context. If the file/name is not found — returns an error, try another option (search_symbols)."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative file path"},
                    "name": {"type": "string", "description": "Symbol name (function/method/class)"},
                },
                "required": ["file", "name"],
            },
        ),
        types.FunctionDeclaration(
            name="find_callers",
            description=(
                "Who calls this symbol (incoming CALLS edges). Useful for tracing 'what triggers X'. "
                "Returns up to `limit` callers with name and file."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max callers (default 8)"},
                },
                "required": ["symbol_name"],
            },
        ),
        types.FunctionDeclaration(
            name="find_callees",
            description=(
                "Whom this symbol calls (outgoing CALLS edges). Useful for tracing 'how X does it'. "
                "Returns up to `limit` callees."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max callees (default 8)"},
                },
                "required": ["symbol_name"],
            },
        ),
        types.FunctionDeclaration(
            name="read_vault_note",
            description=(
                "The full vault note of a module with narrative description. Useful for understanding intent "
                "before exploring the module's symbols."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "note_path": {"type": "string", "description": "for example 'modules/src-services-pricing'"},
                },
                "required": ["note_path"],
            },
        ),
        types.FunctionDeclaration(
            name="grep_in_file",
            description=(
                "Regex grep over a specific file. Useful for switch/dispatch handlers that the CALLS graph "
                "does not catch (route handlers, IPC dispatchers, string-based wiring)."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative file path"},
                    "pattern": {"type": "string", "description": "Python regex"},
                    "max_matches": {"type": "integer", "description": "default 10"},
                },
                "required": ["file", "pattern"],
            },
        ),
        types.FunctionDeclaration(
            name="submit_research",
            description=(
                "TERMINATES the loop. Call it when you are sure you have collected all the key functions. "
                "rationale — a short narrative summary of the flow for the synthesis agent. "
                "selected_symbols — the final list (only those you read via read_function)."
            ),
            parametersJsonSchema={
                "type": "object",
                "properties": {
                    "rationale": {
                        "type": "string",
                        "description": "2-4 sentences about the flow you found (entry → orchestrator → boundary)",
                    },
                    "selected_symbols": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "file": {"type": "string"},
                                "why": {"type": "string", "description": "1 sentence about the role"},
                            },
                            "required": ["name", "file", "why"],
                        },
                    },
                },
                "required": ["rationale", "selected_symbols"],
            },
        ),
    ]
    return [types.Tool(functionDeclarations=decls)]


# ───────────────────────── Agent runner ─────────────────────────


class ExplorationAgent:
    """Subagent loop runner. Stateless — create a new instance per request."""

    def __init__(
        self,
        *,
        store: GraphStore,
        repo: str,
        repo_path: Path,
        gemini: GeminiClient | None = None,
        vault_reader: VaultReader | None = None,
        code_reader: CodeReader | None = None,
        settings: Settings | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.repo_path = repo_path
        self.settings = settings or get_settings()
        self.gemini = gemini or get_gemini_client()
        self.vault_reader = vault_reader or VaultReader(self.settings)
        self.code_reader = code_reader or CodeReader(self.settings)
        self.progress_callback = progress_callback

        # Side-store: bodies read via read_function, handed to the CodeBundle
        # on termination. Not into the LLM context (because bodies are large).
        self._bodies: dict[str, CodeSnippet] = {}  # key = "file::name"

    # ─── Event emission (no-op if no callback was set) ──────────────
    def _emit(self, event: ExplorationEvent) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("progress_callback_failed err=%s", exc)

    @staticmethod
    def _tool_emoji(name: str) -> str:
        return {
            "search_symbols": "🔍",
            "list_module_symbols": "📂",
            "read_function": "📖",
            "find_callers": "⬅️",
            "find_callees": "➡️",
            "read_vault_note": "📝",
            "grep_in_file": "🔎",
            "submit_research": "📤",
        }.get(name, "🔧")

    def _emit_tool_done(
        self, name: str, args: dict[str, Any], response: dict[str, Any], turn: int,
    ) -> None:
        """Emit a summary after a tool has run. Short form for the UI."""
        if "error" in response:
            self._emit(ExplorationEvent(
                kind="warning",
                label=f"   ↳ error: {str(response['error'])[:60]}",
                turn=turn,
            ))
            return
        # Per-tool summary
        if name == "read_function":
            lines = response.get("lines", "?")
            tokens = response.get("tokens", 0)
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ read {lines} ({tokens} tokens) → bundle",
                turn=turn,
            ))
        elif name == "list_module_symbols":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ found {response.get('count', 0)} symbols",
                turn=turn,
            ))
        elif name == "search_symbols":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ {len(response.get('results', []))} matches",
                turn=turn,
            ))
        elif name == "find_callers":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ {response.get('count', 0)} callers",
                turn=turn,
            ))
        elif name == "find_callees":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ {response.get('count', 0)} callees",
                turn=turn,
            ))
        elif name == "read_vault_note":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label="   ↳ note loaded",
                turn=turn,
            ))
        elif name == "grep_in_file":
            self._emit(ExplorationEvent(
                kind="tool_done",
                label=f"   ↳ {response.get('count', 0)} matches",
                turn=turn,
            ))

    @staticmethod
    def _short_args_for_label(name: str, args: dict[str, Any]) -> str:
        """Compressed read-friendly description of a tool call for the UI label."""
        if name == "read_function":
            f = str(args.get("file", "?")).split("/")[-1]
            return f"{args.get('name', '?')}() in {f}"
        if name == "list_module_symbols":
            return str(args.get("module_path", "?")).split("/")[-1] or "?"
        if name == "search_symbols":
            return f"\"{args.get('name_pattern', '?')}\""
        if name == "find_callers":
            return f"who calls {args.get('symbol_name', '?')}"
        if name == "find_callees":
            return f"what {args.get('symbol_name', '?')} calls"
        if name == "read_vault_note":
            return str(args.get("note_path", "?")).split("/")[-1]
        if name == "grep_in_file":
            f = str(args.get("file", "?")).split("/")[-1]
            return f"/{args.get('pattern', '?')}/ in {f}"
        if name == "submit_research":
            sym = args.get("selected_symbols") or []
            return f"{len(sym)} symbols"
        return ""

    # ─── Tool implementations ───────────────────────────────────────

    def _tool_search_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("name_pattern", "")).strip()
        if not pattern:
            return {"error": "name_pattern required"}
        kind = args.get("kind")
        limit = min(int(args.get("limit", 10) or 10), 30)
        try:
            params = {"pat": pattern.lower(), "lim": limit}
            kind_clause = ""
            if kind:
                kind_clause = "AND s.kind = $kind "
                params["kind"] = str(kind)
            rows = self.store.query(
                "MATCH (s:Symbol) "
                f"WHERE s.kind <> 'file_module' {kind_clause}"
                "  AND toLower(s.name) CONTAINS $pat "
                "RETURN s.name AS name, s.kind AS kind, s.file AS file, "
                "       s.signature AS signature "
                "LIMIT $lim",
                params=params,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"query_failed: {type(exc).__name__}"}
        return {"results": rows}

    def _tool_list_module_symbols(self, args: dict[str, Any]) -> dict[str, Any]:
        module_path = str(args.get("module_path", "")).strip()
        if not module_path:
            return {"error": "module_path required"}
        kind = args.get("kind")
        limit = min(int(args.get("limit", 30) or 30), 100)
        prefix = module_path.rstrip("/") + "/"
        try:
            params = {"p": prefix, "exact": module_path, "lim": limit}
            kind_clause = ""
            if kind:
                kind_clause = "AND s.kind = $kind "
                params["kind"] = str(kind)
            rows = self.store.query(
                "MATCH (s:Symbol) "
                f"WHERE s.kind <> 'file_module' {kind_clause}"
                "  AND (s.file STARTS WITH $p OR s.file = $exact) "
                "RETURN s.name AS name, s.kind AS kind, s.file AS file, "
                "       s.start_line AS sl, s.signature AS signature "
                "LIMIT $lim",
                params=params,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"query_failed: {type(exc).__name__}"}
        return {"count": len(rows), "results": rows}

    def _tool_read_function(self, args: dict[str, Any]) -> dict[str, Any]:
        file = str(args.get("file", "")).strip()
        name = str(args.get("name", "")).strip()
        if not file or not name:
            return {"error": "file and name required"}
        try:
            rows = self.store.query(
                "MATCH (s:Symbol) WHERE s.file = $f AND s.name = $n AND s.kind <> 'file_module' "
                "RETURN s.start_line AS sl, s.end_line AS el, s.kind AS kind, s.signature AS signature "
                "LIMIT 1",
                params={"f": file, "n": name},
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"query_failed: {type(exc).__name__}"}
        if not rows:
            return {"error": f"not_found: file={file} name={name}"}
        row = rows[0]
        sl = int(row.get("sl") or 0)
        el = row.get("el")
        bundle = self.code_reader.read_locations(
            self.repo_path,
            [(file, sl, int(el) if el else None)],
            budget_tokens=20_000,
            context_lines=2,
            redact_content=True,
        )
        if not bundle.snippets:
            return {"error": "read_failed (file missing on disk?)"}
        snippet = bundle.snippets[0]
        snippet.symbol_name = name
        key = f"{file}::{name}"
        self._bodies[key] = snippet

        # Cross-module callees hint: we query the graph automatically so the LLM
        # sees the next steps (without needing a separate find_callees).
        # This fixes the gap where the subagent reads the orchestrator but
        # misses the imported converter/aggregator on the next hop (a frequent
        # Flash mistake).
        next_steps: list[dict[str, str]] = []
        try:
            cross_rows = self.store.query(
                "MATCH (s:Symbol {name: $n, file: $f})-[:CALLS]->(t:Symbol) "
                "WHERE t.kind <> 'file_module' AND NOT t.file = $f "
                "RETURN DISTINCT t.name AS name, t.file AS file, t.kind AS kind LIMIT 8",
                params={"n": name, "f": file},
            )
            for r in cross_rows:
                next_steps.append({
                    "callee_name": str(r.get("name", "")),
                    "callee_file": str(r.get("file", "")),
                    "callee_kind": str(r.get("kind", "")),
                    "hint": "consider read_function if relevant to the question",
                })
        except Exception:  # noqa: BLE001, S110
            pass

        # We return a preview (the first 400 chars) so the subagent sees what it got
        body_preview = snippet.content[:400]
        return {
            "ok": True,
            "file": file,
            "name": name,
            "kind": row.get("kind"),
            "lines": f"{snippet.start_line}-{snippet.end_line}",
            "tokens": snippet.tokens,
            "body_preview": body_preview + ("…" if len(snippet.content) > 400 else ""),
            "cross_module_callees": next_steps,  # auto-suggested next reads
        }

    def _tool_find_callers(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("symbol_name", "")).strip()
        if not name:
            return {"error": "symbol_name required"}
        limit = min(int(args.get("limit", 8) or 8), 20)
        try:
            rows = self.store.query(
                "MATCH (caller:Symbol)-[:CALLS]->(target:Symbol {name: $name}) "
                "WHERE caller.kind <> 'file_module' "
                "RETURN DISTINCT caller.name AS name, caller.file AS file, caller.kind AS kind "
                "LIMIT $lim",
                params={"name": name, "lim": limit},
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"query_failed: {type(exc).__name__}"}
        return {"count": len(rows), "callers": rows}

    def _tool_find_callees(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("symbol_name", "")).strip()
        if not name:
            return {"error": "symbol_name required"}
        limit = min(int(args.get("limit", 8) or 8), 20)
        try:
            rows = self.store.query(
                "MATCH (source:Symbol {name: $name})-[:CALLS]->(callee:Symbol) "
                "WHERE callee.kind <> 'file_module' "
                "RETURN DISTINCT callee.name AS name, callee.file AS file, callee.kind AS kind "
                "LIMIT $lim",
                params={"name": name, "lim": limit},
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": f"query_failed: {type(exc).__name__}"}
        return {"count": len(rows), "callees": rows}

    def _tool_read_vault_note(self, args: dict[str, Any]) -> dict[str, Any]:
        note_path = str(args.get("note_path", "")).strip()
        if not note_path:
            return {"error": "note_path required"}
        rel = note_path if note_path.endswith(".md") else f"{note_path}.md"
        note = self.vault_reader.read(self.repo, rel)
        if not note:
            return {"error": f"not_found: {rel}"}
        # Truncate so as not to blow up the LLM context
        body = note.content[:6000]
        meta_min = {
            "type": note.metadata.get("type"),
            "path": note.metadata.get("path"),
            "symbols": note.metadata.get("symbols", [])[:20],
            "cross_refs": note.metadata.get("cross_refs", []),
        }
        return {"meta": meta_min, "body": body, "truncated": len(note.content) > 6000}

    def _tool_grep_in_file(self, args: dict[str, Any]) -> dict[str, Any]:
        import re

        file = str(args.get("file", "")).strip()
        pattern = str(args.get("pattern", "")).strip()
        max_matches = min(int(args.get("max_matches", 10) or 10), 30)
        if not file or not pattern:
            return {"error": "file and pattern required"}
        fp = self.repo_path / file
        if not fp.exists() or not fp.is_file():
            return {"error": f"not_found: {file}"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad_regex: {exc}"}
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"error": f"read_failed: {exc}"}
        matches: list[dict[str, Any]] = []
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append({"line": i, "text": line.strip()[:200]})
                if len(matches) >= max_matches:
                    break
        return {"count": len(matches), "matches": matches}

    # ─── Loop orchestration ─────────────────────────────────────────

    def run(
        self,
        *,
        question: str,
        vault_hits: list[VaultHit],
    ) -> ExplorationResult:
        """Run the exploration loop. Returns a curated bundle."""
        tools = _build_tool_declarations()

        # Initial user message: question + vault hits as the starting context
        initial_prompt = self._build_initial_prompt(question, vault_hits)
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=initial_prompt)]),
        ]

        result = ExplorationResult()
        max_turns = self.settings.subagent_max_turns
        max_calls = self.settings.subagent_max_tool_calls
        seen_calls: set[str] = set()  # dedupe identical calls
        consecutive_empty = 0

        self._emit(ExplorationEvent(
            kind="phase_start",
            label=f"🤖 Exploration subagent ({self.settings.gemini_subagent_model})",
            detail=f"Starting with {len(vault_hits)} vault hits, max {max_turns} turns",
            metadata={"vault_hits": len(vault_hits), "max_turns": max_turns},
        ))

        for turn in range(1, max_turns + 1):
            if result.tool_calls_used >= max_calls:
                result.terminated_reason = "max_tool_calls"
                logger.info("subagent_terminate reason=max_tool_calls calls=%d", result.tool_calls_used)
                self._emit(ExplorationEvent(
                    kind="warning", label="⏹ Tool budget exhausted", turn=turn,
                ))
                break

            self._emit(ExplorationEvent(
                kind="turn_start",
                label=f"💭 Turn {turn} — thinking...",
                turn=turn,
            ))

            try:
                turn_result = self.gemini.generate_with_tools_turn(
                    contents=contents,
                    tools=tools,
                    system_instruction=SUBAGENT_SYSTEM,
                    repo=self.repo,
                    operation=f"subagent_turn_{turn}",
                    temperature=0.1,
                    max_output_tokens=self.settings.subagent_max_output_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("subagent_turn_failed turn=%d err=%s", turn, exc)
                result.terminated_reason = f"turn_error: {type(exc).__name__}"
                break

            result.turns_used = turn
            result.tokens_in += turn_result.input_tokens
            result.tokens_out += turn_result.output_tokens

            # Append the model's response to history.
            # IMPORTANT: we pass the ORIGINAL Part objects from the response
            # (we do not reconstruct them). Gemini 3.x requires preserving
            # thoughtSignature in function_call parts — without it you get a
            # 400 INVALID_ARGUMENT on the next turn.
            if turn_result.model_parts:
                contents.append(types.Content(role="model", parts=turn_result.model_parts))

            # Stall check
            if not turn_result.tool_calls and not turn_result.text:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    result.terminated_reason = "stalled"
                    logger.info("subagent_terminate reason=stalled turn=%d", turn)
                    break
                continue
            consecutive_empty = 0

            # Termination via submit_research (with a guard on minimum bundle size)
            submit_call = next(
                (tc for tc in turn_result.tool_calls if tc.name == "submit_research"),
                None,
            )
            if submit_call:
                rationale = str(submit_call.args.get("rationale", ""))
                selected_raw = submit_call.args.get("selected_symbols", []) or []

                # Guard: if the subagent submits too little for a functional
                # question, we return a tool error and keep the loop going. That
                # forces the bundle to be widened BEFORE the model finishes —
                # without needing a second full loop.
                MIN_BUNDLE_SIZE = 8
                if (len(selected_raw) < MIN_BUNDLE_SIZE
                        and turn < max_turns - 1
                        and result.tool_calls_used < max_calls - 5):
                    result.tool_calls_used += 1
                    err_msg = (
                        f"Bundle has only {len(selected_raw)} symbols. "
                        f"Minimum {MIN_BUNDLE_SIZE}. Read more functions through read_function "
                        f"(find_callers/find_callees to find related ones), then submit again."
                    )
                    contents.append(types.Content(role="user", parts=[
                        types.Part(function_response=types.FunctionResponse(
                            name="submit_research",
                            response={"error": err_msg, "current_count": len(selected_raw)},
                            id=submit_call.id,
                        ))
                    ]))
                    logger.info(
                        "subagent_submit_rejected count=%d min=%d turn=%d (continue)",
                        len(selected_raw), MIN_BUNDLE_SIZE, turn,
                    )
                    self._emit(ExplorationEvent(
                        kind="warning",
                        label=f"⚠️  submit rejected: only {len(selected_raw)}/{MIN_BUNDLE_SIZE} — continuing",
                        turn=turn,
                    ))
                    continue  # next turn

                for s in selected_raw:
                    if not isinstance(s, dict):
                        continue
                    name = str(s.get("name", "")).strip()
                    file = str(s.get("file", "")).strip()
                    why = str(s.get("why", "")).strip()
                    if not name or not file:
                        continue
                    key = f"{file}::{name}"
                    snippet = self._bodies.get(key)
                    sym = SelectedSymbol(
                        name=name, file=file, rationale=why,
                        start_line=snippet.start_line if snippet else None,
                        end_line=snippet.end_line if snippet else None,
                    )
                    result.selected_symbols.append(sym)
                result.research_notes = rationale
                result.terminated_reason = "submit_research"
                logger.info(
                    "subagent_terminate reason=submit_research turn=%d selected=%d bodies_read=%d",
                    turn, len(result.selected_symbols), len(self._bodies),
                )
                self._emit(ExplorationEvent(
                    kind="submit",
                    label=f"✅ Submitted {len(result.selected_symbols)} symbols",
                    detail=rationale[:200] if rationale else None,
                    turn=turn,
                    metadata={"count": len(result.selected_symbols)},
                ))
                break

            # Execute non-terminal tool calls; accumulate function_response parts
            response_parts: list[types.Part] = []
            for tc in turn_result.tool_calls:
                result.tool_calls_used += 1
                short = self._short_args_for_label(tc.name, tc.args)
                # Dedupe identical calls (the LLM sometimes repeats them)
                call_key = f"{tc.name}:{repr(sorted(tc.args.items()))}"
                if call_key in seen_calls:
                    response = {"error": "duplicate_call_skipped"}
                    self._emit(ExplorationEvent(
                        kind="warning", label=f"♻️  duplicate {tc.name}({short})", turn=turn,
                    ))
                else:
                    seen_calls.add(call_key)
                    self._emit(ExplorationEvent(
                        kind="tool_call",
                        label=self._tool_emoji(tc.name) + f" {tc.name}({short})",
                        detail=None,
                        turn=turn,
                        metadata={"tool": tc.name, "args": tc.args},
                    ))
                    response = self._dispatch_tool(tc.name, tc.args)
                    self._emit_tool_done(tc.name, tc.args, response, turn)
                response_parts.append(types.Part(function_response=types.FunctionResponse(
                    name=tc.name, response=response, id=tc.id,
                )))

                if result.tool_calls_used >= max_calls:
                    break

            if response_parts:
                contents.append(types.Content(role="user", parts=response_parts))
        else:
            # Exhausted max_turns without submit
            result.terminated_reason = result.terminated_reason or "max_turns"
            logger.info("subagent_terminate reason=max_turns turns=%d", max_turns)

        # Auto-fallback: if the subagent did not submit but did read bodies —
        # we use everything read as the implicit selection.
        if not result.selected_symbols and self._bodies:
            logger.info(
                "subagent_auto_select count=%d (no submit_research emitted)",
                len(self._bodies),
            )
            for key, snippet in self._bodies.items():
                file, _, name = key.partition("::")
                result.selected_symbols.append(SelectedSymbol(
                    name=name, file=file,
                    rationale="(auto-selected: read by subagent without explicit submit)",
                    start_line=snippet.start_line, end_line=snippet.end_line,
                ))

        # Build the final CodeBundle from all read bodies (selected take priority)
        result.code_bundle = self._assemble_bundle(result.selected_symbols)

        bundle_files = len(result.code_bundle.snippets) if result.code_bundle else 0
        self._emit(ExplorationEvent(
            kind="phase_end",
            label=(f"🤖 Subagent done: {result.turns_used} turns, "
                   f"{result.tool_calls_used} calls, {bundle_files} bodies"),
            detail=f"reason={result.terminated_reason}",
            metadata={
                "turns": result.turns_used,
                "tool_calls": result.tool_calls_used,
                "bodies": bundle_files,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "reason": result.terminated_reason,
            },
        ))
        return result

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "search_symbols": self._tool_search_symbols,
            "list_module_symbols": self._tool_list_module_symbols,
            "read_function": self._tool_read_function,
            "find_callers": self._tool_find_callers,
            "find_callees": self._tool_find_callees,
            "read_vault_note": self._tool_read_vault_note,
            "grep_in_file": self._tool_grep_in_file,
        }.get(name)
        if not handler:
            return {"error": f"unknown_tool: {name}"}
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_handler_failed tool=%s err=%s", name, exc)
            return {"error": f"handler_exception: {type(exc).__name__}: {exc}"}

    def _build_initial_prompt(self, question: str, vault_hits: list[VaultHit]) -> str:
        """Initial context: question + vault hits with cross_refs (as a starting guide)."""
        lines = [
            f"# User question\n{question}\n",
            "# Starting vault hits (top-relevant modules)",
            "Each hit has `path` (where the code is) and `cross_refs` (related modules).",
            "Read the notes that look most relevant via `read_vault_note`,",
            "then explore symbols via `list_module_symbols`/`find_callers`/`find_callees`.",
            "Do not stop at the first module — the flow is usually spread over 3-5 modules.\n",
        ]
        for i, h in enumerate(vault_hits[:6], 1):
            lines.append(f"## Hit {i}: {h.note_path} (score {h.score:.2f})")
            if h.path:
                lines.append(f"- module path: `{h.path}`")
            if h.cross_refs:
                refs = ", ".join(h.cross_refs[:8])
                lines.append(f"- cross_refs: {refs}")
            if h.symbols:
                lines.append(f"- symbols: {', '.join(h.symbols[:10])}")
            lines.append("")
        lines.append("\nWhen you are confident in the set — call `submit_research`.")
        return "\n".join(lines)

    def _assemble_bundle(self, selected: list[SelectedSymbol]) -> CodeBundle:
        """Assembles a CodeBundle from the read bodies. Selected ones have
        priority, the rest are a fallback.
        """
        budget = self.settings.max_code_tokens_per_qa
        bundle = CodeBundle(budget=budget)
        max_bodies = self.settings.subagent_max_bodies
        seen: set[str] = set()

        # 1) Selected (priority order — the order the LLM submitted them in)
        for sym in selected:
            key = f"{sym.file}::{sym.name}"
            if key in seen:
                continue
            seen.add(key)
            snippet = self._bodies.get(key)
            if snippet is None:
                continue
            if bundle.total_tokens + snippet.tokens > budget:
                bundle.truncated = True
                break
            bundle.snippets.append(snippet)
            bundle.total_tokens += snippet.tokens
            if len(bundle.snippets) >= max_bodies:
                break

        # 2) If selected is empty (the LLM did not submit), we add everything read
        if not bundle.snippets:
            for snippet in self._bodies.values():
                if bundle.total_tokens + snippet.tokens > budget:
                    bundle.truncated = True
                    break
                bundle.snippets.append(snippet)
                bundle.total_tokens += snippet.tokens
                if len(bundle.snippets) >= max_bodies:
                    break

        return bundle
