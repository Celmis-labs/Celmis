"""Structural code review via ast-grep — fast, deterministic, no LLM cost.

Why structural rules:
    LLMs miss patterns deterministic checks catch reliably (mutable default arg,
    bare except, console.log left in code, == vs ===). They also overpay tokens
    for trivially-detectable issues. AST-grep runs in milliseconds and emits
    findings with confidence=1.0 — Verifier won't filter them as FPs.

Architecture:
    StructuralAgent  → implements ReviewAgent contract — parallel with LLM agents
    Rule pack        → list[StructuralRule] keyed by language
    ast-grep-py      → runs each rule's pattern/kind/relation against changed files

Rule format (per rule):
    id              — short stable identifier (e.g. 'py.empty-except')
    language        — tree-sitter language name
    severity        — error|warning|info
    title           — short headline
    body            — explanation (1-2 sentences)
    suggestion      — optional fix hint
    rule            — ast-grep YAML rule body, as Python dict
                      see https://ast-grep.github.io/reference/yaml.html
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.models import Finding, FindingSeverity, HunkSide

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructuralRule:
    """One ast-grep-backed structural pattern."""

    id: str
    language: str | tuple[str, ...]  # one tree-sitter lang or many: ('typescript', 'javascript', 'tsx')
    severity: FindingSeverity
    title: str
    body: str
    rule: dict[str, Any]     # ast-grep rule body — `kind`, `pattern`, `has`, `inside`, etc.
    suggestion: str | None = None
    file_globs: tuple[str, ...] = ()  # extra filter beyond language ext mapping

    @property
    def languages(self) -> tuple[str, ...]:
        if isinstance(self.language, str):
            return (self.language,)
        return tuple(self.language)


# ─── Default rule pack ──────────────────────────────────────────────


_RULES: list[StructuralRule] = [
    # ── Python ──
    StructuralRule(
        id="py.empty-except",
        language="python",
        severity=FindingSeverity.WARNING,
        title="Empty except clause silently swallows errors",
        body=(
            "Catching an exception with only `pass` hides bugs and makes "
            "debugging harder. At minimum log the exception or re-raise."
        ),
        suggestion="logger.exception('...') or raise",
        rule={
            "kind": "except_clause",
            "has": {"kind": "block", "has": {"kind": "pass_statement"}},
            "not": {"has": {
                "stopBy": "end",
                "any": [
                    {"kind": "raise_statement"},
                    {"kind": "return_statement"},
                    {"kind": "call"},
                ],
            }},
        },
    ),
    StructuralRule(
        id="py.bare-except",
        language="python",
        severity=FindingSeverity.WARNING,
        title="Bare `except:` catches everything including KeyboardInterrupt",
        body=(
            "Use `except Exception:` so SystemExit and KeyboardInterrupt still "
            "propagate as the user expects."
        ),
        suggestion="except Exception:",
        rule={
            "kind": "except_clause",
            "not": {"has": {"any": [
                {"kind": "identifier"},
                {"kind": "tuple"},
                {"kind": "as_pattern"},
            ]}},
        },
    ),
    StructuralRule(
        id="py.mutable-default-arg",
        language="python",
        severity=FindingSeverity.ERROR,
        title="Mutable default argument shared across calls",
        body=(
            "Default `[]` / `{}` is evaluated once at def-time. All calls share "
            "the same list/dict, leading to surprising state leaks."
        ),
        suggestion="def f(x=None): x = x or []",
        rule={
            "kind": "default_parameter",
            "has": {"any": [
                {"kind": "list"},
                {"kind": "dictionary"},
                {"kind": "set"},
            ]},
        },
    ),
    StructuralRule(
        id="py.print-debug",
        language="python",
        severity=FindingSeverity.INFO,
        title="`print()` left in production code",
        body="Use logging instead of print. Easier to filter, redirect, level-control.",
        suggestion="logger.info(...)",
        rule={"pattern": "print($$$ARGS)"},
        # File-level filter: only flag in non-test files
        file_globs=("src/*.py", "src/**/*.py"),
    ),
    # ── TypeScript / JavaScript / JSX / TSX ──
    StructuralRule(
        id="js.console-log",
        language=("typescript", "javascript", "tsx"),
        severity=FindingSeverity.INFO,
        title="`console.log` left in code",
        body=(
            "Production code should not log via console. Use a structured logger, "
            "or remove the call before merging."
        ),
        rule={"pattern": "console.log($$$ARGS)"},
    ),
    StructuralRule(
        id="js.loose-equality",
        language=("typescript", "javascript", "tsx"),
        severity=FindingSeverity.WARNING,
        title="`==` / `!=` does type coercion",
        body=(
            "`0 == ''` and `null == undefined` are both true. Use strict "
            "`===` / `!==` to compare values without coercion."
        ),
        suggestion="=== / !==",
        rule={
            "any": [
                {"pattern": "$A == $B"},
                {"pattern": "$A != $B"},
            ],
        },
    ),
    StructuralRule(
        id="js.await-in-foreach",
        language=("typescript", "javascript", "tsx"),
        severity=FindingSeverity.ERROR,
        title="`await` inside `Array.forEach` is ignored",
        body=(
            "forEach does not await its callback's Promise. The loop completes "
            "before async work finishes, leading to race conditions. Use a "
            "for-of loop or Promise.all(map(...))."
        ),
        suggestion="for (const x of arr) { await ... }",
        rule={"pattern": "$ARR.forEach(async ($$$P) => { $$$BODY })"},
    ),
    StructuralRule(
        id="js.todo-fixme",
        language=("typescript", "javascript", "tsx"),
        severity=FindingSeverity.INFO,
        title="TODO/FIXME without ticket reference",
        body="Track work in your issue tracker so it doesn't rot in code.",
        rule={
            "kind": "comment",
            "regex": "(?i)\\b(todo|fixme|hack|xxx)\\b(?!.*(#|JIRA|EN-|GH-))",
        },
    ),
    # ── Go ──
    StructuralRule(
        id="go.error-discarded",
        language="go",
        severity=FindingSeverity.WARNING,
        title="Error return value explicitly discarded with `_`",
        body=(
            "Discarding `err` with `_` hides failures. If the error is "
            "intentionally ignored, add a comment explaining why."
        ),
        rule={"pattern": "_, _ = $CALL"},
    ),
]


_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "tsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".java": "java",
}


def _detect_language(file_path: str) -> str | None:
    suffix = Path(file_path).suffix.lower()
    return _LANG_BY_EXT.get(suffix)


def _matches_globs(file_path: str, globs: tuple[str, ...]) -> bool:
    if not globs:
        return True
    from fnmatch import fnmatch
    name = Path(file_path).name
    return any(fnmatch(name, g) or fnmatch(file_path, g) for g in globs)


# ─── Agent ──────────────────────────────────────────────────────────


class StructuralAgent(ReviewAgent):
    """AST-grep-driven structural checks. Deterministic, fast (~ms), no tokens."""

    name = "structural"
    severity_default = FindingSeverity.WARNING

    def __init__(self, rules: list[StructuralRule] | None = None) -> None:
        self.rules: list[StructuralRule] = rules if rules is not None else list(_RULES)

    def review(self, context: AgentContext) -> AgentRunResult:
        t0 = time.time()
        try:
            from ast_grep_py import SgRoot
        except ImportError as exc:
            logger.warning("ast_grep_unavailable: %s", exc)
            return AgentRunResult(
                agent=self.name, error=f"ast-grep-py not installed: {exc}",
                elapsed_seconds=time.time() - t0,
            )

        pr = context.pull_request
        # Group hunks by file so we parse each file only once
        hunks_by_file: dict[str, list] = {}
        for h in pr.hunks:
            hunks_by_file.setdefault(h.file_path, []).append(h)

        rules_by_lang: dict[str, list[StructuralRule]] = {}
        for r in self.rules:
            for lang in r.languages:
                rules_by_lang.setdefault(lang, []).append(r)

        findings: list[Finding] = []
        files_scanned = 0
        files_failed = 0

        for file_path, hunks in hunks_by_file.items():
            lang = _detect_language(file_path)
            if lang is None or lang not in rules_by_lang:
                continue
            content = self._reconstruct_new_file_content(hunks)
            if not content:
                continue
            try:
                root = SgRoot(content, lang)
                ast_root = root.root()
            except Exception as exc:  # noqa: BLE001
                logger.debug("astgrep_parse_failed file=%s err=%s", file_path, exc)
                files_failed += 1
                continue
            files_scanned += 1

            new_lines = self._changed_new_lines(hunks)

            for rule in rules_by_lang[lang]:
                if not _matches_globs(file_path, rule.file_globs):
                    continue
                try:
                    matches = ast_root.find_all({"rule": rule.rule})
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "astgrep_rule_failed rule=%s lang=%s err=%s",
                        rule.id, lang, exc,
                    )
                    continue
                for m in matches:
                    line_1based = m.range().start.line + 1
                    # Only flag if the match starts on a line that the PR added/changed
                    if new_lines and line_1based not in new_lines:
                        continue
                    findings.append(self._match_to_finding(rule, file_path, line_1based, m))

        logger.info(
            "structural_done files_scanned=%d files_failed=%d findings=%d elapsed=%.2fs",
            files_scanned, files_failed, len(findings), time.time() - t0,
        )
        return AgentRunResult(
            agent=self.name,
            findings=findings,
            tokens_in=0, tokens_out=0,
            elapsed_seconds=time.time() - t0,
        )

    @staticmethod
    def _hunk_new_side_lines(hunk: Any) -> list[str]:
        """Extract new-side lines from a unified-diff hunk's content.

        Skips '@@' header, '-' lines (removed). Keeps '+' (added) and ' ' (context),
        stripping the leading prefix.
        """
        out: list[str] = []
        for raw in (hunk.content or "").splitlines():
            if not raw:
                # preserve blank lines as empty
                out.append("")
                continue
            if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
                continue
            if raw.startswith("-"):
                continue
            if raw.startswith("+") or raw.startswith(" "):
                out.append(raw[1:])
                continue
            # No prefix (rare) — keep verbatim
            out.append(raw)
        return out

    @classmethod
    def _reconstruct_new_file_content(cls, hunks: list) -> str:
        """Splice hunks at new_start positions; pad gaps with blank lines.

        We don't have the full file, only diff hunks. We approximate by
        placing each hunk's new-side lines at its new_start. Patterns that
        span multiple unchanged lines may miss matches — accepted trade-off.
        """
        max_line = 0
        for h in hunks:
            end = (h.new_start or 1) + (h.new_count or 0) - 1
            if end > max_line:
                max_line = end
        if max_line < 1:
            return ""

        lines: list[str] = [""] * max_line
        for h in hunks:
            start = max(1, h.new_start or 1)
            for i, ln in enumerate(cls._hunk_new_side_lines(h)):
                idx = start - 1 + i
                if 0 <= idx < len(lines):
                    lines[idx] = ln
        return "\n".join(lines)

    @staticmethod
    def _changed_new_lines(hunks: list) -> set[int]:
        """Set of new-side line numbers the PR added (`+` lines only)."""
        out: set[int] = set()
        for h in hunks:
            line = (h.new_start or 1) - 1
            for raw in (h.content or "").splitlines():
                if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
                    continue
                if raw.startswith("-"):
                    continue
                line += 1
                if raw.startswith("+"):
                    out.add(line)
        return out

    def _match_to_finding(
        self, rule: StructuralRule, file_path: str, line: int, match: Any,
    ) -> Finding:
        snippet = (match.text() or "")[:200].replace("\n", " ⏎ ")
        body = rule.body
        if snippet:
            body = f"{rule.body}\n\n```\n{snippet}\n```"
        return Finding(
            file_path=file_path,
            line=line,
            side=HunkSide.RIGHT,
            severity=rule.severity,
            title=rule.title,
            body=body,
            suggestion=rule.suggestion,
            agent=self.name,
            rule_id=f"structural.{rule.id}",
            confidence=1.0,
            # An ast-grep rule matched a syntax tree. There is a file, a line
            # and the code — the reader agrees or disagrees at a glance, which
            # is what separates this from a model's judgement no matter how
            # confident either one is.
            evidence_kind="proven",
        )


# ─── Public helpers ─────────────────────────────────────────────────


def list_rules() -> list[StructuralRule]:
    """Return copy of the active rule pack — for diagnostics / docs."""
    return list(_RULES)


# ─── Post-apply verification ─────────────────────────────────────────
#
# "Apply fix" wrote a patch into somebody's repository and reported success
# without ever looking at what it produced. These two functions are what it
# looks at now. Neither calls a model; both run on content already in memory.


def language_for(file_path: str) -> str | None:
    """Tree-sitter language for a path, or None when we do not know it.

    None is not a failure — it means "cannot check", and the caller must say
    so rather than refuse a patch it simply cannot read.
    """
    from pathlib import Path
    return _LANG_BY_EXT.get(Path(file_path).suffix.lower())


#: Beyond this a parse costs more than the patch is worth, and a generated
#: file that size is not what Apply-fix is for.
MAX_PARSE_BYTES = 2_000_000


def parses(content: str, language: str) -> tuple[bool, str]:
    """Does this text still parse? Returns (ok, reason).

    Python goes through `ast.parse`, which is exact. Everything else asks
    ast-grep whether the tree contains an ERROR node — note the `rule`
    wrapper: the bare `{"kind": "ERROR"}` form raises "missing field rule".

    (True, "") for content we cannot check, because a checker that reports
    "broken" when it means "unknown" would block every patch to a language we
    have no grammar for.
    """
    if len(content) > MAX_PARSE_BYTES:
        return True, ""
    if language == "python":
        import ast
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return False, f"line {exc.lineno}: {exc.msg}"
        except (RecursionError, MemoryError, ValueError) as exc:
            logger.debug("parse_check_gave_up err=%s", exc)
            return True, ""
        return True, ""
    try:
        from ast_grep_py import SgRoot
        errors = SgRoot(content, language).root().find_all(
            {"rule": {"kind": "ERROR"}})
    except Exception as exc:  # noqa: BLE001
        logger.debug("parse_check_unavailable lang=%s err=%s", language, exc)
        return True, ""
    if errors:
        line = errors[0].range().start.line + 1
        return False, f"line {line}: syntax error"
    return True, ""


def rule_by_id(rule_id: str) -> StructuralRule | None:
    """The rule behind a finding id, or None.

    Finding ids are `structural.<rule id>` — see `_match_to_finding`.
    """
    wanted = rule_id.split("structural.", 1)[-1]
    for rule in _RULES:
        if rule.id == wanted:
            return rule
    return None


def rule_still_matches(
    rule: StructuralRule,
    content: str,
    language: str,
    *,
    line_start: int,
    line_end: int,
) -> bool:
    """Does `rule` still fire INSIDE the lines the patch rewrote?

    Scoped to the replaced range on purpose. A whole-file scan would report
    matches elsewhere that the review deliberately never raised — the review
    only flags lines the pull request touched — and the user would read that
    as "your fix did not work".
    """
    try:
        from ast_grep_py import SgRoot
        matches = SgRoot(content, language).root().find_all({"rule": rule.rule})
    except Exception as exc:  # noqa: BLE001
        logger.debug("recheck_unavailable rule=%s err=%s", rule.id, exc)
        return False
    for m in matches:
        line = m.range().start.line + 1
        if line_start <= line <= line_end:
            return True
    return False
