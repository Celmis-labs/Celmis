"""Code context for the defect agent — what the diff alone does not show.

WHY THIS MODULE EXISTS. The defect agent used to read raw unified-diff hunks
with three lines of context, no line numbers, and no idea what function a
hunk sat in — while its prompt demanded that every claim be provable "inside
the file". A hunk is not the file. With a small model the gap produces both kinds of error at once:

  * MISSES — an inverted condition (`!==` became `===`, `!!x` became `!x`) is
    only wrong relative to the rest of the function; a removed fallback line
    is only wrong relative to what reads the variable afterwards; a caller
    that now passes `JSON.stringify(obj)` to a callee that expects the object
    is wrong in a line the hunk does not contain; `getTotal()` versus the raw
    field is wrong only given what `getTotal()` does.
  * FALSE POSITIVES — "undefined variable" for a name declared in the loop
    above the hunk; "wrong index" where the mapping array is declared higher
    in the file; "wrong type check" where the only caller passes an array.

The cure for both is the same: show the code. This module builds, for one pull
request, a block of text that sits next to the diff in the prompt:

  1. the hunks themselves, with NEW-SIDE LINE NUMBERS (so a finding can name a
     line instead of guessing it, and so "the line above" is a number);
  2. the full ENCLOSING FUNCTION of each hunk, at the PR head, with the lines
     the PR changed marked — or a window of lines when no function is found;
  3. a few CALL SITES of the functions the diff changed, found with `git grep`
     in the local clone at the head commit;
  4. the short bodies of CALLEES invoked on changed lines, when they are
     defined in the same file.

EVERY ADDITION IS BUDGETED AND FAILS OPEN. The total is capped by
`REVIEW_CODE_CONTEXT_CHARS`, each file by a third of that, each function by a
line count, call sites by `REVIEW_CODE_CONTEXT_CALLSITES`, and the whole build
by a wall-clock deadline. Anything that goes wrong — no clone, no provider, a
commit the clone does not hold, a parser that is missing, a file that is
minified or binary — drops that piece and the prompt carries the numbered
diff alone, which is still strictly more than the agent had before. Nothing in
here raises into the review.

WHERE THE FILE TEXT COMES FROM, in order: the local clone the code graph was
built from (`git show <head_sha>:<path>`, only when the clone holds that
commit — the working tree is never read, it may be on any branch), then the
provider's `read_file_at`, then the file at the PR's BASE commit with the diff
applied (`code_revision`; never the clone's HEAD, which is newer than the PR). The enclosing function is found with tree-sitter
(the repository already depends on it for the indexer) and, when no grammar
fits or the parse fails, a brace/indent scanner that understands strings and
comments well enough not to be fooled by a `}` inside either.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

#: Files longer than this are not read for context: a generated bundle, a
#: data dump. Counted in characters of the decoded text.
MAX_FILE_CHARS = 1_000_000
#: A line longer than this marks a minified file — nothing useful to show.
MAX_LINE_CHARS = 2_000
#: A function longer than this is shown as a window around the change, not in
#: full: the point is the neighbourhood, not a whole 900-line controller.
MAX_FUNCTION_LINES = 250
#: Half-width of the window used when there is no enclosing function (top-level
#: code, a config file, a template) or the function is too long.
WINDOW_LINES = 25
#: Callee bodies longer than this are not included.
MAX_CALLEE_LINES = 40
#: Call sites per changed symbol, and symbols looked up in total.
CALLSITES_PER_SYMBOL = 3
MAX_SYMBOLS = 8
#: A name that `git grep` finds more often than this is too common to say
#: anything about its callers — the first three hits would be arbitrary.
MAX_GREP_HITS = 60
#: Lines of context printed around a call site.
CALLSITE_CONTEXT = 2
#: Wall-clock allowance for the whole build, git calls included.
DEADLINE_SECONDS = 25.0
_GIT_TIMEOUT = 10

#: Path fragments never worth showing as a caller.
_SKIP_PATH = re.compile(
    r"(^|/)(vendor|node_modules|third_party|thirdparty|bower_components|dist|build|"
    r"\.git|min|minified)(/|$)|\.min\.(js|css)$|\.(lock|map|svg|png|jpe?g|gif|"
    r"ico|woff2?|ttf|eot|pdf|zip|gz)$|(^|/)(package-lock\.json|yarn\.lock)$",
    re.I,
)

#: Extension -> tree-sitter grammar name(s), tried in order.
_GRAMMARS: dict[str, tuple[str, ...]] = {
    ".php": ("php",), ".phtml": ("php",), ".inc": ("php",),
    ".js": ("javascript",), ".jsx": ("javascript", "tsx"), ".mjs": ("javascript",),
    ".cjs": ("javascript",), ".ts": ("typescript",), ".tsx": ("tsx",),
    ".py": ("python",), ".go": ("go",), ".java": ("java",),
    ".cs": ("csharp", "c_sharp"), ".rb": ("ruby",), ".rs": ("rust",),
    ".kt": ("kotlin",), ".c": ("c",), ".h": ("c", "cpp"), ".cpp": ("cpp",),
    ".cc": ("cpp",), ".hpp": ("cpp",),
}
#: Extensions the brace scanner may handle when no grammar did.
_BRACE_EXT = frozenset({
    ".php", ".phtml", ".inc", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".cs", ".go", ".c", ".h", ".cpp", ".cc", ".hpp", ".rs", ".kt",
    ".swift", ".scala", ".dart",
})

_FUNC_TYPE = re.compile(r"(function|method|constructor|arrow_function|func_literal)")
_NOT_FUNC_TYPE = re.compile(
    r"(type|call|parameter|parameters|modifier|body|name|declarator|statement_block|"
    r"invocation|reference|list|clause|keyword|signature|expression_statement)$"
)
_CONTROL = frozenset({
    "if", "for", "foreach", "while", "switch", "catch", "elseif", "else", "with",
    "until", "do", "try", "finally", "return", "match", "using", "lock", "fixed",
    "synchronized", "when", "unless",
})
_CALL = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(")
_SYMBOL_NOISE = frozenset({
    "__construct", "constructor", "init", "main", "get", "set", "run", "handle",
    "render", "execute", "call", "apply", "anonymous",
})


@dataclass(frozen=True)
class FuncSpan:
    """One function-like block: 1-based inclusive lines."""

    name: str
    start: int
    end: int

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


@dataclass
class _Budget:
    """Characters left to spend, and the time by which to stop."""

    chars: int
    deadline: float

    def take(self, text: str) -> bool:
        if len(text) > self.chars:
            return False
        self.chars -= len(text)
        return True

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.deadline


# ─── Numbered hunks ──────────────────────────────────────────────────

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class NumberedHunk:
    """A hunk rendered with new-side numbers, plus what it changed."""

    path: str
    text: str
    added: set[int] = field(default_factory=set)
    #: First and last new-side line the hunk touches. For a pure deletion both
    #: are the line the deletion sits before.
    first: int = 0
    last: int = 0


def number_hunk(hunk: Any) -> NumberedHunk:
    """Render one hunk with a new-side line number on every kept line.

    Added and context lines carry their number in the head file; removed
    lines carry a blank gutter (they have no line there) and keep their `-`
    so the `-`/`+` pairing the reader needs stays visible. The `@@` header is
    kept as it was. Never raises: an unparsable header leaves the text as the
    provider sent it.
    """
    content = str(getattr(hunk, "content", "") or "")
    path = str(getattr(hunk, "file_path", "") or "")
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ends the last diff line, not a line
    new_ln = int(getattr(hunk, "new_start", 0) or 0)
    out: list[str] = []
    added: set[int] = set()
    touched: list[int] = []
    for raw in lines:
        m = _HUNK_HEADER.match(raw)
        if m:
            new_ln = int(m.group(2))
            out.append(raw)
            continue
        if raw.startswith("+"):
            out.append(f"{new_ln:>6} {raw}")
            added.add(new_ln)
            touched.append(new_ln)
            new_ln += 1
        elif raw.startswith("-"):
            out.append(f"{'':>6} {raw}")
            touched.append(new_ln)
        elif raw.startswith("\\"):
            out.append(raw)
        else:
            out.append(f"{new_ln:>6} {raw}")
            new_ln += 1
    first = min(touched) if touched else int(getattr(hunk, "new_start", 0) or 0)
    last = max(touched) if touched else first
    return NumberedHunk(path=path, text="\n".join(out), added=added, first=first, last=last)


def render_numbered_diff(hunks: list, max_chars: int = 50_000) -> str:
    """The diff section: every hunk with line numbers, capped like the plain
    rendering it replaces (same limit, same truncation notice)."""
    parts: list[str] = []
    used = 0
    for i, hunk in enumerate(hunks):
        try:
            nh = number_hunk(hunk)
            chunk = (
                f"### {nh.path} (new lines {getattr(hunk, 'new_start', '?')}-"
                f"{getattr(hunk, 'new_start', 0) + getattr(hunk, 'new_count', 0) - 1})\n"
                f"```diff\n{nh.text}\n```\n"
            )
        except Exception:  # noqa: BLE001 - never lose a hunk to a renderer bug
            chunk = f"### {hunk.file_path}\n```diff\n{hunk.content}\n```\n"
        if used + len(chunk) > max_chars:
            parts.append(f"... (truncated, {len(hunks) - i} hunks omitted)")
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


# ─── Function spans: tree-sitter first, scanner second ───────────────


def _ext(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _node_name(node: Any) -> str:
    """The name of a function node; for an anonymous one, what it is bound
    to (`x = function`, `key: function`, `const f = () =>`)."""
    try:
        for target, fields in ((node, ("name",)), (node.parent, ("name", "key", "left", "property"))):
            if target is None:
                continue
            for f in fields:
                child = target.child_by_field_name(f)
                if child is not None and child.text:
                    text = child.text.decode("utf-8", "replace")
                    return re.split(r"[.>:\s]+", text.strip())[-1] or "anonymous"
    except Exception:  # noqa: BLE001
        pass
    return "anonymous"


def _treesitter_functions(text: str, path: str) -> list[FuncSpan] | None:
    """Every function-like node of `text`, or None when no grammar parsed it.

    None means "could not tell"; an empty list means "parsed, no functions".
    """
    grammars = _GRAMMARS.get(_ext(path))
    if not grammars:
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except Exception:  # noqa: BLE001 - optional at runtime
        return None
    for grammar in grammars:
        try:
            tree = get_parser(grammar).parse(text.encode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        spans: list[FuncSpan] = []
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            if (
                n.is_named and n.child_count > 0
                and _FUNC_TYPE.search(n.type) and not _NOT_FUNC_TYPE.search(n.type)
            ):
                spans.append(FuncSpan(_node_name(n), n.start_point[0] + 1, n.end_point[0] + 1))
        return spans
    return None


_FUNC_HEADER = re.compile(
    r"\bfunction\b|\bfunc\b|\bfn\b|=>\s*$|"
    r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|async|override|"
    r"export|default|get|set|virtual|readonly|synchronized)\s+)*\*?[\w$]+\s*\([^;{}]*\)"
    r"\s*(?::\s*[\w\\?|<>\[\], ]+)?\s*(?:throws\s+[\w., ]+)?\s*$|"
    r"^[\w$<>\[\],.?&*\\:\s]*?\b[\w$]+\s*\([^;{}=]*\)[^;{}=]*$"
)
_NAME_BOUND = re.compile(r"([\w$.]+)\s*[:=]\s*(?:async\s+)?function\b")
_NAME_FUNC = re.compile(r"\b(?:function|func|fn)\s+&?\s*(?:\([^)]*\)\s*)?([\w$]+)")
_NAME_CALLISH = re.compile(r"([\w$]+)\s*\(")


def _header_name(header: str) -> str | None:
    """The function name a block header declares, or None if it is not one."""
    h = " ".join(header.split())
    if not h:
        return None
    first_word = re.match(r"[\w$]+", h.lstrip("}) ").strip())
    if first_word and first_word.group(0).lower() in _CONTROL:
        return None
    if not _FUNC_HEADER.search(h):
        return None
    for rx in (_NAME_FUNC, _NAME_BOUND):
        m = rx.search(h)
        if m:
            return m.group(1).split(".")[-1]
    m = _NAME_CALLISH.search(h)
    if m and m.group(1).lower() not in _CONTROL:
        return m.group(1)
    return "anonymous" if "function" in h or "=>" in h else None


def _scan_braces(text: str, path: str) -> list[FuncSpan]:
    """Function spans of a brace language, without a parser.

    A small lexer: it tracks line numbers, skips `//` and `/* */` comments
    (and `#` ones in PHP), skips the contents of '...', "..." and `...`
    strings with backslash escapes, and keeps a stack of open braces. Text
    since the last `;`, `{` or `}` is the "header" of the next block; when it
    reads like a function head (see `_header_name`) the block is recorded as a
    function. A brace inside a string or a comment therefore never opens or
    closes anything. Regex literals and heredocs are not understood — a
    wrong guess there degrades the span, never the review.
    """
    hash_comments = _ext(path) in {".php", ".phtml", ".inc"}
    spans: list[FuncSpan] = []
    stack: list[tuple[int, str | None]] = []
    header: list[str] = []
    line = 1
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
            header.append(" ")
        elif c == "/" and nxt == "/" or (hash_comments and c == "#" and nxt != "["):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        elif c in "'\"`":
            quote = c
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                if i < n and text[i] == "\n":
                    line += 1
                i += 1
            header.append('""')
        elif c == "{":
            stack.append((line, _header_name("".join(header))))
            header = []
        elif c == "}":
            if stack:
                start, name = stack.pop()
                if name is not None:
                    spans.append(FuncSpan(name, start, line))
            header = []
        elif c == ";":
            header = []
        else:
            header.append(c)
        i += 1
    # The header sits on the line where its `{` opens, which for K&R/Allman
    # styles can be the line after the signature. Pull the start up to the
    # first non-blank line of the header text when that is cheap to know.
    return spans


def _indent_functions(text: str) -> list[FuncSpan]:
    """Function spans of an indentation language (Python): a `def` runs until
    the next non-blank line indented no deeper than it."""
    lines = text.split("\n")
    spans: list[FuncSpan] = []
    for idx, raw in enumerate(lines):
        m = re.match(r"(\s*)(?:async\s+)?def\s+(\w+)", raw)
        if not m:
            continue
        indent = len(m.group(1).expandtabs(4))
        end = idx
        for j in range(idx + 1, len(lines)):
            s = lines[j]
            if not s.strip():
                continue
            if len(s) - len(s.lstrip()) <= indent and not s.lstrip().startswith((")", "]", "}")):
                break
            end = j
        spans.append(FuncSpan(m.group(2), idx + 1, end + 1))
    return spans


def list_functions(text: str, path: str) -> list[FuncSpan]:
    """All function-like spans of `text`: tree-sitter when a grammar parses
    the file, the scanner when it does not, [] when neither applies."""
    try:
        ts = _treesitter_functions(text, path)
        if ts is not None:
            return ts
        ext = _ext(path)
        if ext == ".py":
            return _indent_functions(text)
        if ext in _BRACE_EXT:
            return _scan_braces(text, path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_context_functions_failed path=%s err=%s", path, exc)
    return []


def enclosing_function(
    spans: list[FuncSpan], first: int, last: int,
) -> FuncSpan | None:
    """The function to show for a change spanning `first..last`.

    The OUTERMOST function that contains the change and fits
    `MAX_FUNCTION_LINES`: a change in a callback should be read with the
    function that declares the variables the callback closes over. When even
    the innermost is too long, that innermost one is returned and the caller
    windows it.
    """
    containing = [s for s in spans if s.start <= first and s.end >= last]
    if not containing:
        return None
    fits = [s for s in containing if s.lines <= MAX_FUNCTION_LINES]
    if fits:
        return max(fits, key=lambda s: s.lines)
    return min(containing, key=lambda s: s.lines)


# ─── Where file text comes from ──────────────────────────────────────


class CodeSource:
    """Reads files at the PR head and greps the clone. Every method fails open.

    `clone_root` is the checkout the code graph was built from (or None);
    `provider` is anything with `read_file_at(repo, ref, path)` (or None).
    """

    def __init__(self, pr: Any, clone_root: Path | None, provider: Any | None) -> None:
        self.pr = pr
        self.root = clone_root
        self.provider = provider
        self.head = str(getattr(pr, "head_sha", "") or "")
        self._cache: dict[str, str | None] = {}
        self._have_commit: bool | None = None

    def _git(self, *args: str) -> subprocess.CompletedProcess | None:
        if self.root is None:
            return None
        try:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "-C", str(self.root), *args],
                capture_output=True, timeout=_GIT_TIMEOUT, check=False,
            )
        except Exception as exc:  # noqa: BLE001 - git missing, timeout
            logger.debug("code_context_git_failed args=%s err=%s", args[:1], exc)
            return None

    @property
    def clone_has_head(self) -> bool:
        if self._have_commit is None:
            ok = False
            if self.root is not None and re.fullmatch(r"[0-9a-fA-F]{7,64}", self.head):
                r = self._git("cat-file", "-e", f"{self.head}^{{commit}}")
                ok = bool(r and r.returncode == 0)
            self._have_commit = ok
        return self._have_commit

    def read(self, path: str) -> str | None:
        """The file at the head commit, or None. Minified/binary/huge -> None."""
        if path in self._cache:
            return self._cache[path]
        text = self._read_uncached(path)
        if text is not None and (
            len(text) > MAX_FILE_CHARS or "\x00" in text[:4096]
            or any(len(ln) > MAX_LINE_CHARS for ln in text.split("\n", 400)[:400])
        ):
            text = None
        self._cache[path] = text
        return text

    def _read_uncached(self, path: str) -> str | None:
        if self.clone_has_head:
            r = self._git("show", f"{self.head}:{path}")
            if r is not None and r.returncode == 0:
                return r.stdout.decode("utf-8", "replace")
        reader = getattr(self.provider, "read_file_at", None)
        if reader is not None and self.head:
            try:
                return reader(self.pr.repo, self.head, path)
            except Exception as exc:  # noqa: BLE001 - no scope, rate limit, outage
                logger.debug("code_context_provider_read_failed path=%s err=%s", path, exc)
        return self._from_base_plus_diff(path)

    def _from_base_plus_diff(self, path: str) -> str | None:
        """Last resort when the head is in neither the clone nor the provider:
        the file at the PR's base commit with the diff applied
        (`code_revision`). Never the clone's HEAD, which is newer than the PR."""
        if self.root is None:
            return None
        from src.review.code_revision import (
            KIND_BASE_PLUS_DIFF,
            hunks_by_new_path,
            overlay_file,
            resolve_code_revision,
        )

        rev = resolve_code_revision(self.pr, self.root)
        if rev.kind != KIND_BASE_PLUS_DIFF:
            return None
        hunks = hunks_by_new_path(self.pr).get(path)
        if not hunks:
            r = self._git("show", f"{rev.rev}:{path}")
            return r.stdout.decode("utf-8", "replace") if r and r.returncode == 0 else None
        if any(getattr(h, "is_deleted_file", False) for h in hunks):
            return None
        new_file = any(getattr(h, "is_new_file", False) for h in hunks)
        base_text = None
        if not new_file:
            old = getattr(hunks[0], "old_file_path", "") or path
            r = self._git("show", f"{rev.rev}:{old}")
            if r is None or r.returncode != 0:
                return None
            base_text = r.stdout.decode("utf-8", "replace")
        return overlay_file(base_text, hunks)

    def grep(self, name: str) -> list[tuple[str, int]] | None:
        """(path, line) of every whole-word hit for `name` at the head commit,
        None when the clone cannot be searched."""
        if not self.clone_has_head or not re.fullmatch(r"[\w$]+", name):
            return None
        r = self._git("grep", "-n", "-w", "-I", "-F", "-e", name, self.head, "--")
        if r is None or r.returncode not in (0, 1):
            return None
        hits: list[tuple[str, int]] = []
        prefix = f"{self.head}:"
        for row in r.stdout.decode("utf-8", "replace").split("\n"):
            if not row.startswith(prefix):
                continue
            parts = row[len(prefix):].split(":", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            if _SKIP_PATH.search(parts[0]) or len(parts[2]) > MAX_LINE_CHARS:
                continue
            hits.append((parts[0], int(parts[1])))
        return hits


def make_source(pr: Any, provider: Any | None = None) -> CodeSource:
    """A `CodeSource` for `pr`, locating the clone the way the graph does."""
    root: Path | None = None
    try:
        # The clone's location lives on the application settings (`repo_path`),
        # not on the review settings: asking the review settings found no
        # clone on a real server, and every consumer of it went quiet.
        from src.config import get_settings
        from src.review.graph_context import _clone_root, graph_slug_candidates

        settings = get_settings()
        for slug in graph_slug_candidates(pr):
            root = _clone_root(slug, settings)
            if root is not None:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_context_no_clone err=%s", exc)
    return CodeSource(pr, root, provider)


# ─── Sections ────────────────────────────────────────────────────────


def _numbered(lines: list[str], first: int, marks: set[int]) -> str:
    return "\n".join(
        f"{first + k:>6}{'*' if first + k in marks else ' '} {ln}" for k, ln in enumerate(lines)
    )


def _body(text_lines: list[str], span: FuncSpan, marks: set[int], first: int, last: int) -> str:
    """The numbered body of `span`, windowed around the change if it is long."""
    s, e = span.start, span.end
    note = ""
    if span.lines > MAX_FUNCTION_LINES:
        s = max(span.start, first - WINDOW_LINES)
        e = min(span.end, last + WINDOW_LINES)
        note = f"(function is {span.lines} lines; showing lines {s}-{e} of it)\n"
        head = _numbered(text_lines[span.start - 1:span.start + 1], span.start, marks)
        if s > span.start + 2:
            note += head + "\n   ...\n"
    return note + _numbered(text_lines[s - 1:e], s, marks)


@dataclass
class CodeContext:
    """The finished context: sections in prompt order, and what was spent."""

    enclosing: str = ""
    callsites: str = ""
    callees: str = ""
    symbols: list[str] = field(default_factory=list)

    def render(self) -> str:
        parts = [p for p in (self.enclosing, self.callsites, self.callees) if p]
        return "\n".join(parts)


def build_code_context(
    pr: Any,
    hunks: list,
    *,
    provider: Any | None = None,
    source: CodeSource | None = None,
    max_chars: int = 40_000,
    max_callsites: int = 8,
) -> CodeContext:
    """Build the enclosing-function, call-site and callee sections. Never raises."""
    out = CodeContext()
    try:
        _build(out, pr, hunks, provider, source, max_chars, max_callsites)
    except Exception as exc:  # noqa: BLE001 - context is a bonus, not a gate
        logger.warning("code_context_failed pr=%s err=%s", getattr(pr, "number", "?"), exc)
        return CodeContext()
    # The block budget counts bodies; the section headings are paid for here,
    # so the total is a hard ceiling whatever the headings weigh.
    if len(out.render()) > max_chars:
        out.callsites = ""
    if len(out.render()) > max_chars:
        out.callees = ""
    if len(out.render()) > max_chars:
        out.enclosing = out.enclosing[:max(0, max_chars - 40)] + "\n... (cut: budget)\n" if max_chars > 80 else ""
    return out


def _build(
    out: CodeContext, pr: Any, hunks: list, provider: Any | None,
    source: CodeSource | None, max_chars: int, max_callsites: int,
) -> None:
    if max_chars <= 0 or not hunks:
        return
    src = source or make_source(pr, provider)
    budget = _Budget(max_chars, time.monotonic() + DEADLINE_SECONDS)
    per_file = max(2_000, max_chars // 3)

    by_file: dict[str, list[Any]] = {}
    for h in hunks:
        if getattr(h, "is_binary", False) or getattr(h, "is_deleted_file", False):
            continue
        by_file.setdefault(h.file_path, []).append(h)

    shown: dict[str, list[tuple[int, int]]] = {}
    blocks: list[str] = []
    changed_syms: dict[str, tuple[str, FuncSpan]] = {}
    callee_jobs: list[tuple[str, FuncSpan | None, str]] = []
    file_lines: dict[str, list[str]] = {}
    file_spans: dict[str, list[FuncSpan]] = {}

    for path, fhunks in by_file.items():
        if budget.expired or budget.chars <= 0:
            break
        text = src.read(path)
        if text is None:
            continue
        lines = text.split("\n")
        file_lines[path] = lines
        spans = list_functions(text, path)
        file_spans[path] = spans
        used = 0
        seen: set[tuple[int, int]] = set()
        marks = {ln for hh in fhunks for ln in number_hunk(hh).added}
        for h in fhunks:
            nh = number_hunk(h)
            if h.is_new_file:
                continue  # the hunk IS the file
            hunk_end = h.new_start + max(h.new_count, 1) - 1
            fn = enclosing_function(spans, nh.first, nh.last)
            added_text = "\n".join(
                lines[ln - 1] for ln in sorted(nh.added) if 0 < ln <= len(lines))
            callee_jobs.append((path, fn, added_text))
            if fn is not None:
                if fn.name not in _SYMBOL_NOISE and fn.name != "anonymous":
                    changed_syms.setdefault(fn.name, (path, fn))
                key = (fn.start, fn.end)
                if key in seen:
                    continue
                seen.add(key)
                if fn.start >= h.new_start and fn.end <= hunk_end:
                    continue  # the hunk already shows all of it
                body = _body(lines, fn, marks, nh.first, nh.last)
                title = f"### {path} — `{fn.name}` (lines {fn.start}-{fn.end})"
            else:
                lo = max(1, nh.first - WINDOW_LINES)
                hi = min(len(lines), nh.last + WINDOW_LINES)
                if (lo, hi) in seen or hi <= lo:
                    continue
                seen.add((lo, hi))
                body = _numbered(lines[lo - 1:hi], lo, marks)
                title = f"### {path} — surrounding lines {lo}-{hi}"
            block = f"{title}\n```\n{body}\n```\n"
            if used + len(block) > per_file or not budget.take(block):
                blocks.append(f"... (more code context for {path} omitted: budget)\n")
                break
            used += len(block)
            blocks.append(block)
            shown.setdefault(path, []).append((nh.first, nh.last))
    if blocks:
        out.enclosing = (
            "## Code the diff sits in (head of the PR; lines marked * are changed by this PR)\n"
            "The full enclosing function (or a window of lines) for each hunk above. "
            "Judge a change against THIS code, not against the hunk alone.\n\n"
            + "\n".join(blocks)
        )

    if budget.expired or budget.chars <= 0:
        return
    out.symbols = list(changed_syms)
    out.callees = _callees(src, callee_jobs, file_lines, file_spans, shown, budget)
    if max_callsites > 0 and not budget.expired:
        out.callsites = _callsites(src, changed_syms, by_file, file_lines, max_callsites, budget)


_CALLEE_TOTAL = 6


def _callees(
    src: CodeSource, jobs: list[tuple[str, FuncSpan | None, str]],
    file_lines: dict[str, list[str]], file_spans: dict[str, list[FuncSpan]],
    shown: dict[str, list[tuple[int, int]]], budget: _Budget,
) -> str:
    """Short bodies of same-file functions invoked on changed lines."""
    blocks: list[str] = []
    done: set[tuple[str, str]] = set()
    for path, fn, added_text in jobs:
        spans = file_spans.get(path) or []
        for name in dict.fromkeys(m.group(1) for m in _CALL.finditer(added_text)):
            if name.lower() in _CONTROL or (path, name) in done:
                continue
            if fn is not None and name == fn.name:
                continue
            cands = [s for s in spans if s.name == name and s.lines <= MAX_CALLEE_LINES]
            if not cands:
                continue
            c = min(cands, key=lambda s: s.lines)
            if fn is not None and fn.start <= c.start and c.end <= fn.end:
                continue  # nested inside the function already shown
            done.add((path, name))
            body = _numbered(file_lines[path][c.start - 1:c.end], c.start, set())
            block = f"### {path} — callee `{name}` (lines {c.start}-{c.end})\n```\n{body}\n```\n"
            if len(blocks) >= _CALLEE_TOTAL or not budget.take(block):
                break
            blocks.append(block)
    if not blocks:
        return ""
    return (
        "## Callees defined in the same file (invoked on changed lines)\n"
        "What the called functions actually do — read before assuming their behaviour.\n\n"
        + "\n".join(blocks)
    )


def _callsites(
    src: CodeSource, symbols: dict[str, tuple[str, FuncSpan]], by_file: dict[str, list[Any]],
    file_lines: dict[str, list[str]], limit: int, budget: _Budget,
) -> str:
    """Up to `limit` call sites of the changed functions, found by git grep.

    The function's own lines (its definition, a recursive call) are not
    callers and are skipped; so are hits that are definitions of the same
    name elsewhere. A name with more than `MAX_GREP_HITS` hits is too common
    to sample honestly and is left out rather than shown arbitrarily.
    """
    if not symbols or not src.clone_has_head:
        return ""
    blocks: list[str] = []
    for name, (own_path, own) in list(symbols.items())[:MAX_SYMBOLS]:
        if len(blocks) >= limit or budget.expired:
            break
        hits = src.grep(name)
        if not hits or len(hits) > MAX_GREP_HITS:
            continue
        definition = re.compile(
            rf"\b(?:function|def|func|fn)\s+&?{re.escape(name)}\b")
        taken = 0
        for hp, ln in hits:
            if taken >= CALLSITES_PER_SYMBOL or len(blocks) >= limit:
                break
            if hp == own_path and own.start <= ln <= own.end:
                continue
            text = src.read(hp)
            if text is None:
                continue
            lines = file_lines.get(hp) or text.split("\n")
            if not 0 < ln <= len(lines) or definition.search(lines[ln - 1]):
                continue
            lo, hi = max(1, ln - CALLSITE_CONTEXT), min(len(lines), ln + CALLSITE_CONTEXT)
            block = (
                f"### `{name}` called in {hp}:{ln}\n```\n"
                f"{_numbered(lines[lo - 1:hi], lo, {ln})}\n```\n"
            )
            if not budget.take(block):
                return _wrap_callsites(blocks)
            blocks.append(block)
            taken += 1
    return _wrap_callsites(blocks)


def _wrap_callsites(blocks: list[str]) -> str:
    if not blocks:
        return ""
    return (
        "## Call sites of the changed functions (found in the repository at the PR head)\n"
        "A sample, not the full list. Use them to check what the callers pass and "
        "expect; a claim about a caller must quote one of these lines.\n\n"
        + "\n".join(blocks)
    )
