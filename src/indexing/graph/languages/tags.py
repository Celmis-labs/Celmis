"""TagsExtractor — one extractor, every language the grammar authors wrote a
tags query for.

WHY THIS EXISTS. Celmis had seven hand-written extractors (Python, the
TypeScript family, Go, Java, C#, PHP, C/C++) at 440-640 lines each, and a
repository in any other language produced a graph with nothing in it. Measured
on the discourse checkout: 8185 `.rb` files, and the graph held 14258 symbols
of which ZERO were in a Ruby file — every one came from the Ember.js frontend
under `app/assets/javascripts`. The review agents for such a repository got
"(no graph context)" on every pull request, which is the same thing they get
for a repository nobody ever indexed.

Writing nineteen more hand-rolled walkers was the obvious answer and the wrong
one. `tree-sitter-language-pack` ships each grammar's own `tags` query — the
same artefact GitHub's code navigation is built on, written and maintained by
the people who wrote the grammar. `get_tags_query("ruby")` returns patterns
capturing `@definition.class`, `@definition.module`, `@definition.method` and
`@reference.call`; run against a real 69 KB `app/models/user.rb` it yields 1
class, 1 module, 233 methods and 1158 call references. That is the same shape
the hand-written extractors produce, from a source that cannot drift out of
sync with the grammar.

WHAT IT DOES NOT DO. A tags query knows definitions and references; it does not
know imports, module resolution, or which of four same-named methods a call
meant. Edges therefore leave `to_id=None` with the called name in
`raw_target`, exactly as the hand-written extractors do for unresolved calls,
and `resolver.py` finishes the job by name. Where a language already HAS a
hand-written extractor, that one keeps its higher priority: it resolves imports
and module paths this cannot see. This class is the floor for everything else,
not a replacement for them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import (
    EdgeInfo,
    ExtractionResult,
    SymbolExtractor,
    SymbolInfo,
)

logger = logging.getLogger(__name__)


#: A tags query names its captures `definition.<what>` / `reference.<what>`.
#: The graph's own vocabulary is narrower (see SymbolInfo.kind), so the long
#: tail of grammar-specific words is folded onto it rather than passed through:
#: a `definition.trait` and a `definition.interface` are both an interface to
#: everything downstream, and a kind nobody maps is worse than a coarse one.
_KIND_BY_CAPTURE = {
    "class": "class",
    "struct": "class",
    "record": "class",
    "enum": "class",
    "object": "class",
    "type": "class",
    "trait": "interface",
    "interface": "interface",
    "protocol": "interface",
    "module": "module",
    "namespace": "module",
    "package": "module",
    "method": "method",
    "function": "function",
    "macro": "function",
    "constructor": "method",
    "field": "variable",
    "constant": "variable",
    "variable": "variable",
    "property": "variable",
}

#: References that mean "this symbol uses that one". A tags query also emits
#: `reference.class` / `reference.type` (a mention in a signature), which is a
#: weaker claim than a call; both travel, distinguished by `kind` so the graph
#: can weigh them and `CALLER_EDGE_KINDS` in review/graph_context.py already
#: knows both words.
_EDGE_BY_CAPTURE = {
    "call": "CALLS",
    "send": "CALLS",
    "class": "REFERENCES",
    "type": "REFERENCES",
    "interface": "REFERENCES",
    "module": "REFERENCES",
    "implementation": "IMPLEMENTS",
}

#: A file that produces more than this many symbols is almost certainly
#: generated (a 30k-line protobuf stub, a vendored bundle). Indexing it costs
#: more than it is worth and it drowns the blast radius of everything around
#: it. The cap is per file and reported as a parse error so it is visible
#: rather than silent.
MAX_SYMBOLS_PER_FILE = 2000

#: Same reasoning for edges: `user.rb` legitimately produced 1158 call
#: references, so the ceiling has to sit well above that.
MAX_EDGES_PER_FILE = 6000


def _file_symbol_id(rel: str) -> str:
    return f"{rel}::__module__"


class TagsExtractor(SymbolExtractor):
    """Symbols and call edges for one language, via its own tags query.

    `language` is the tree-sitter language name; `extensions` are the suffixes
    the registry dispatches on. Both are instance attributes because one class
    serves every language — the base class declares them at class level for the
    hand-written adapters, which have exactly one language each.
    """

    def __init__(
        self,
        language: str,
        extensions: tuple[str, ...],
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.language = language
        self.extensions = extensions
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    # ─── the contract ────────────────────────────────────────────────

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        if source is None:
            try:
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])

        rel = self._relpath(file_path)
        result = ExtractionResult()

        try:
            matches = self._run_query(source)
        except Exception as e:  # noqa: BLE001
            # One unparsable file must never fail a repository's index. The
            # error travels on the result so the run can report it.
            logger.debug(
                "tags_extract_failed lang=%s file=%s err=%s", self.language, rel, e,
            )
            return ExtractionResult(parse_errors=[f"tags_query_failed: {e}"])

        if matches is None:
            # No tags query for this language. Emitting the file_module symbol
            # anyway would put the file in the graph as a thing that WAS
            # indexed, and "indexed, and empty" is the claim that let a
            # repository of 8185 Ruby files read as fully covered. Nothing
            # parsed it, so it holds nothing and says so.
            logger.debug("tags_no_query lang=%s file=%s", self.language, rel)
            return ExtractionResult(parse_errors=[f"no_tags_query: {self.language}"])

        file_sym_id = _file_symbol_id(rel)
        result.symbols.append(SymbolInfo(
            id=file_sym_id, name=Path(rel).stem, kind="file_module",
            file=rel, start_line=1, end_line=None, language=self.language,
        ))

        definitions = self._definitions(matches, rel, result)
        self._references(matches, definitions, file_sym_id, result)

        # DEFINED_IN, the same containment edge every hand-written extractor
        # emits: it is what makes "the symbols of this file" a graph question
        # rather than a string match on the path.
        for sym in result.symbols:
            if sym.id != file_sym_id:
                result.edges.append(EdgeInfo(
                    from_id=sym.id, to_id=file_sym_id,
                    kind="DEFINED_IN", confidence="strong",
                ))
        return result

    # ─── the query ───────────────────────────────────────────────────

    def _run_query(self, source: bytes) -> list[tuple[Any, dict]] | None:
        """Every (pattern, captures) the tags query matched, in document order.

        None — distinct from an empty list — when the grammar ships no tags
        query at all. Empty means "parsed, defines nothing", which is true of
        a file holding only comments; None means "nothing here can read this",
        and the caller must not record the file as indexed on the strength of
        it.

        `matches` rather than `captures`: the query captures a definition node
        AND its `@name` in one pattern, and only a match keeps the two together.
        Reading the capture lists separately would pair the 233rd method with
        the 233rd name and be right only by accident of ordering.
        """
        from tree_sitter import Query, QueryCursor
        from tree_sitter_language_pack import get_language, get_parser, get_tags_query

        query_text = get_tags_query(self.language)
        if not query_text:
            return None
        tree = get_parser(self.language).parse(source)
        query = Query(get_language(self.language), query_text)
        return QueryCursor(query).matches(tree.root_node)

    # ─── definitions ─────────────────────────────────────────────────

    def _definitions(
        self, matches: list[tuple[Any, dict]], rel: str, result: ExtractionResult,
    ) -> list[tuple[int, int, str]]:
        """Append every definition as a symbol; return their byte spans.

        The spans are what lets a reference say WHICH symbol makes it: a call
        on line 700 of a 233-method file belongs to the method whose body
        contains it, and the innermost containing definition is that method.
        """
        spans: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        capped = False
        for _pattern, caps in matches:
            for capture, nodes in caps.items():
                if not capture.startswith("definition."):
                    continue
                kind = _KIND_BY_CAPTURE.get(capture.split(".", 1)[1])
                if kind is None:
                    continue
                name = _first_name(caps)
                if not name:
                    continue
                node = nodes[0]
                line = node.start_point[0] + 1
                sym_id = f"{rel}::{name}@{line}"
                if sym_id in seen:
                    continue
                if len(result.symbols) >= MAX_SYMBOLS_PER_FILE:
                    capped = True
                    break
                seen.add(sym_id)
                result.symbols.append(SymbolInfo(
                    id=sym_id, name=name, kind=kind, file=rel,
                    start_line=line, end_line=node.end_point[0] + 1,
                    language=self.language,
                ))
                spans.append((node.start_byte, node.end_byte, sym_id))
            if capped:
                break
        if capped:
            result.parse_errors.append(
                f"symbol_cap_reached file={rel} cap={MAX_SYMBOLS_PER_FILE}"
            )
        # Innermost-first: a method inside a class must win the containment
        # test against the class that holds it, and the shorter span is the
        # inner one.
        spans.sort(key=lambda s: (s[1] - s[0], s[0]))
        return spans

    # ─── references ──────────────────────────────────────────────────

    def _references(
        self,
        matches: list[tuple[Any, dict]],
        definitions: list[tuple[int, int, str]],
        file_sym_id: str,
        result: ExtractionResult,
    ) -> None:
        """Append one edge per reference, from whatever encloses it.

        `to_id` is None with the name in `raw_target` — the same contract the
        hand-written extractors use for a call they cannot resolve, so
        `resolver.py` finishes these exactly as it finishes those.
        """
        seen: set[tuple[str, str, str]] = set()
        for _pattern, caps in matches:
            for capture, nodes in caps.items():
                if not capture.startswith("reference."):
                    continue
                edge_kind = _EDGE_BY_CAPTURE.get(capture.split(".", 1)[1])
                if edge_kind is None:
                    continue
                name = _first_name(caps)
                if not name:
                    continue
                node = nodes[0]
                from_id = _enclosing(definitions, node.start_byte) or file_sym_id
                key = (from_id, name, edge_kind)
                if key in seen:
                    # The same symbol calling the same name twice is one edge:
                    # the graph answers "who reaches this", and a caller that
                    # calls it four times is still one caller.
                    continue
                if len(result.edges) >= MAX_EDGES_PER_FILE:
                    result.parse_errors.append(
                        f"edge_cap_reached cap={MAX_EDGES_PER_FILE}"
                    )
                    return
                seen.add(key)
                result.edges.append(EdgeInfo(
                    from_id=from_id, to_id=None, kind=edge_kind,
                    confidence="unresolved", raw_target=name,
                ))

    # ─── helpers ─────────────────────────────────────────────────────

    def _relpath(self, file_path: Path) -> str:
        p = Path(file_path)
        if self.repo_root:
            try:
                return str(p.resolve().relative_to(self.repo_root))
            except ValueError:
                pass
        return str(p)


def _first_name(caps: dict) -> str:
    """The `@name` of this match, decoded. "" when the pattern captured none."""
    nodes = caps.get("name") or []
    if not nodes:
        return ""
    try:
        return nodes[0].text.decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _enclosing(definitions: list[tuple[int, int, str]], offset: int) -> str | None:
    """The innermost definition whose span contains `offset`.

    Linear over a list already sorted innermost-first, so the first hit is the
    innermost one. Fine at this size: the cap above bounds it at 2000, and the
    real files measured produce a few hundred.
    """
    for start, end, sym_id in definitions:
        if start <= offset < end:
            return sym_id
    return None
