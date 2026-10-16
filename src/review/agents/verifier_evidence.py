"""Repository evidence for the LLM veto — what a reader would open before deciding.

Why this exists. Review agents on a cheap model
can emit confident false positives that no reading of the DIFF
can refute, because the refutation lives in code the diff does not contain:

    "undefined constant X"            X is a class constant in another file
    "undefined variable $v"           $v is set in the loop enclosing the hunk,
                                      well above it
    "the dialog closes itself because the template markup, one file over,
     the overlay has class dialogbox" shows it does not
    "missing is_file check"           the check is many lines above the hunk
    "typo in the global appState"     that IS the global's name, repo-wide
    "wrong array index"               the index matches a mapping defined
                                      elsewhere in the same file

The veto in `verifier.py` could not touch these. It woke only with three or
more judgeable findings (a typical pull request has one or two), and its prompt
held the first five hunks of the PR cut at 2000 characters — usually not even
the finding's own file. So the evidence is gathered here, per finding:

    window    the finding's file at the PR head commit, ±`WINDOW_RADIUS`
              lines around the line it points at
    hunks     the PR's own hunks for THAT file (not the PR's first five)
    callees   the BODIES of functions called within `CALLEE_RADIUS` lines of the
              finding's line (a closure in a variable, a function or a method
              with exactly one definition in the repository). E.g. "the
              variable is undefined after the catch" was kept because the
              catch calls a helper that ends the process, and the helper is
              never in the window around the line
    symbols   what the claim names — backticked tokens, `Class::CONST`,
              ALL_CAPS identifiers, `$vars`, quoted ids/classes, `name(` calls
              — capped at `MAX_SYMBOLS`, each searched across the repository

Where it reads from, in order. The local clone the code graph was built from
(`graph_context._clone_root`, i.e. `settings.repo_path(slug)`): `git show
<sha>:<path>` and `git grep -n -F <sha>` when the head commit is in it, which
is exact; clone HEAD when only the commit is missing, labelled as such because
line numbers and content may differ; else the provider (`read_file_at`,
`search_code`). A hosted code index covers the DEFAULT branch, not the pull
request, so those hits are labelled so: they prove a symbol exists there and
their absence proves nothing.

What this does not claim. Evidence is text put in front of a model; whether it
refutes a claim is the model's judgement, and the prompt tells it to keep a
finding whenever the evidence is absent or ambiguous. An empty bundle is the
old behaviour, not an error. EVERYTHING here fails open — a missing clone, a
provider without search, a timeout, a bad path each mean less evidence, never
a failed review — and the stats (`EvidenceBundle.stats`) say how much of the
evidence actually arrived, so a thin answer can be told from a thorough one.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.review.agents.literal_lookups import resolve_lookups
from src.review.code_revision import (
    KIND_BASE_PLUS_DIFF,
    KIND_CLONE_HEAD,
    KIND_HEAD,
    KIND_NONE,
    MAX_OVERLAY_FILES,
    hunks_by_new_path,
    overlay_file,
    resolve_code_revision,
    touched_base_paths,
)

logger = logging.getLogger(__name__)

#: Total characters of evidence across all findings of one veto call. The
#: veto's prompt is paid for on every finding in it; this is what keeps a
#: forty-finding review from sending forty files.
TOTAL_BUDGET_CHARS = 24_000

#: The most one finding may take, however few findings share the budget.
PER_FINDING_CAP_CHARS = 8_000

#: Lines of the finding's file shown on each side of the line it points at.
#: Wide on purpose: the enclosing loop and the guard that refuted the
#: measured false positives were 40 and 60 lines above the hunk.
WINDOW_RADIUS = 80

#: Symbols searched per finding.
MAX_SYMBOLS = 6

#: Matches shown per symbol, each with one line of context either side.
MAX_HITS_PER_SYMBOL = 4

#: Provider searches (HTTP calls) per veto call, summed over findings. A hosted
#: code search is rate limited and slow; the clone has no such limit.
MAX_PROVIDER_SEARCHES = 12

#: Wall clock for gathering everything, after which the rest is skipped.
GATHER_DEADLINE_SECONDS = 40.0

#: Seconds one git subprocess may run.
GIT_TIMEOUT_SECONDS = 10.0

#: Lines of git output read before the process is stopped.
GIT_MAX_OUTPUT_LINES = 400

#: Paths a search never reports: dependencies, build output, lockfiles and
#: minified bundles hold every identifier in the world and no answer.
_EXCLUDE_PATHSPECS = (
    ":(exclude,glob)**/vendor/**",
    ":(exclude,glob)**/node_modules/**",
    ":(exclude,glob)**/dist/**",
    ":(exclude,glob)**/build/**",
    ":(exclude,glob)**/*.min.js",
    ":(exclude,glob)**/*.min.css",
    ":(exclude,glob)**/*.map",
    ":(exclude,glob)**/*.lock",
    ":(exclude,glob)**/package-lock.json",
    ":(exclude,glob)**/composer.lock",
    ":(exclude,glob)**/yarn.lock",
    ":(exclude,glob)**/pnpm-lock.yaml",
)

#: Words that are not symbols however they are spelled in a claim.
_STOPWORDS = frozenset(["a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "for", "from", "has", "have", "if", "in", "is", "it", "its", "not", "of", "on", "or", "so", "that", "the", "then", "this", "to", "was", "were", "will", "with", "true", "false", "null", "none", "nil", "undefined", "void", "self", "this", "parent", "static", "if", "else", "elseif", "for", "foreach", "while", "switch", "case", "break", "continue", "return", "function", "class", "const", "let", "var", "new", "try", "catch", "finally", "throw", "throws", "isset", "empty", "unset", "array", "list", "echo", "print", "include", "require", "import", "export", "default", "public", "private", "protected", "int", "str", "string", "bool", "float", "todo", "fixme", "note", "error", "warning", "info", "json", "http", "https", "html", "css", "sql", "api", "url", "uri", "xss", "csrf", "cwe", "php", "js", "ts", "get", "post", "put", "delete", "head", "options", "php", "js", "ts", "py", "go", "rb", "css", "html", "json", "yml", "yaml", "md", "txt", "xml", "sql"])

#: ALL_CAPS words that name a protocol or a standard, not a constant.
_CAPS_STOPWORDS = frozenset(["TODO", "FIXME", "NOTE", "NULL", "TRUE", "FALSE", "JSON", "HTTP", "HTTPS", "HTML", "CSS", "SQL", "API", "URL", "URI", "XSS", "CSRF", "CWE", "OWASP", "GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PHP", "JS", "TS", "UTF", "ASCII", "UUID", "ID", "IDS", "DOM", "CLI", "CPU", "RAM", "EOF", "EOL", "OK", "PR", "CI", "CD"])

_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_SCOPE = re.compile(r"\b([A-Za-z_]\w*)::([A-Za-z_]\w*)")
_DOLLAR = re.compile(r"\$[A-Za-z_]\w*")
_QUOTED = re.compile(r"[\"']([#.]?[A-Za-z][\w-]{2,39})[\"']")
_SELECTOR = re.compile(r"(?:(?<=[\s\"'(`])|^)([#.][A-Za-z][\w-]{2,39})(?![\w(])")
_CAPS = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_CALL = re.compile(r"\b([A-Za-z_]\w{2,})\(")
_IDENT = re.compile(r"^[#.$]?[A-Za-z_][\w:\\.>-]*(?:\(\))?$")


def extract_symbols(title: str, body: str = "", reasoning: str = "",
                    limit: int = MAX_SYMBOLS) -> list[str]:
    """The identifiers a finding's claim rests on, most specific first.

    Order is the order of trust: a backticked token is what the author of the
    claim marked as code; `Class::CONST` and `$vars` are unmistakable; quoted
    ids and `#id` / `.class` selectors come from markup claims; bare
    ALL_CAPS words and `name(` calls are the weakest and fill what remains.
    Keywords, common words and protocol acronyms are skipped, the list is
    de-duplicated case-sensitively and capped at `limit` (~6): a claim that
    names twenty things is a summary, and searching all of them would spend
    the budget on the least informative.

    For `Class::CONST` the full token is returned, followed by the bare member
    when it is ALL_CAPS — the DEFINITION line says `const CONST`, not
    `Class::CONST`, and that is the line that refutes "undefined constant".
    """
    text = "\n".join(s for s in (title, body, reasoning) if s)
    out: list[str] = []

    def add(sym: str) -> None:
        sym = sym.strip().removesuffix("()")
        bare = sym.lstrip("#.$")
        if len(bare) < 3 or bare.lower() in _STOPWORDS or sym in out:
            return
        if sym.isupper() and sym in _CAPS_STOPWORDS:
            return
        out.append(sym)

    for m in _BACKTICK.finditer(text):
        tok = m.group(1).strip()
        if _IDENT.match(tok):
            add(tok)
            if "::" in tok:
                member = tok.rsplit("::", 1)[1]
                if member.isupper():
                    add(member)
    for m in _SCOPE.finditer(text):
        add(f"{m.group(1)}::{m.group(2)}")
        if m.group(2).isupper():
            add(m.group(2))
    for m in _DOLLAR.finditer(text):
        if m.group(0) != "$this":
            add(m.group(0))
    for m in _QUOTED.finditer(text):
        add(m.group(1))
    for m in _SELECTOR.finditer(text):
        add(m.group(1))
    for m in _CAPS.finditer(text):
        add(m.group(0))
    for m in _CALL.finditer(text):
        add(m.group(1))
    return out[:limit]


# ─── Data ────────────────────────────────────────────────────────────


@dataclass
class SymbolHits:
    """What the search for one symbol found, and where it looked."""

    symbol: str
    #: Where the answer came from: "clone @ <sha>", "clone HEAD (...)", or
    #: "provider code search (default branch)". Empty when nothing could ask.
    source: str = ""
    #: Rendered match blocks, at most `MAX_HITS_PER_SYMBOL`.
    blocks: list[str] = field(default_factory=list)
    #: True when the search RAN and found nothing — distinct from `source == ""`
    #: (nothing could be asked), because only the first is evidence of absence,
    #: and only from a source that sees the PR head.
    searched: bool = False
    #: False when `blocks` come from code that is not the pull request's own
    #: (the clone's HEAD, newer than the PR; the provider's default branch).
    pr_code: bool = True


@dataclass
class FindingEvidence:
    """Everything gathered for one finding."""

    window: str = ""
    window_source: str = ""
    hunks: str = ""
    symbols: list[str] = field(default_factory=list)
    hits: list[SymbolHits] = field(default_factory=list)
    #: Rendered bodies of the functions called near the finding's line, each
    #: headed with where it is defined. "Does the catch's helper return or
    #: exit?" is answered by the helper's body, which the window around the
    #: finding never contains.
    callees: list[str] = field(default_factory=list)
    #: True when any window, hit or callee body above was read from code that
    #: is NOT the pull request's (see `SymbolHits.pr_code`). The verifier
    #: never drops a finding whose evidence carries this flag.
    non_pr_source: bool = False
    #: Literal-table lookups near the line, evaluated by code
    #: (`literal_lookups.resolve_lookups`), one rendered line each.
    lookups: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return (len(self.window) + len(self.hunks)
                + sum(len(b) for h in self.hits for b in h.blocks)
                + sum(len(c) for c in self.callees)
                + sum(len(c) for c in self.lookups))


@dataclass
class EvidenceBundle:
    """Evidence for a list of findings, index-aligned, plus how it went."""

    items: list[FindingEvidence] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


# ─── Git, local ──────────────────────────────────────────────────────


def _git(root: Path, args: list[str], *, max_lines: int = GIT_MAX_OUTPUT_LINES) -> list[str] | None:
    """Run git in `root`; its stdout lines, or None when it failed or timed out.

    Output is read line by line and the process stopped at `max_lines`: a
    symbol like `id` matches half a monorepo and nobody needs the other half.
    Exit status 1 with no output is git grep's "no match", which is `[]`, not
    None.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, errors="replace",
        )
    except OSError:
        return None
    lines: list[str] = []
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            if len(lines) >= max_lines or time.monotonic() > deadline:
                proc.kill()
                break
        proc.wait(timeout=GIT_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001
        proc.kill()
        return None
    if not lines and proc.returncode not in (0, 1):
        return None
    return lines


def _has_commit(root: Path, sha: str) -> bool:
    if not sha or not re.fullmatch(r"[0-9A-Fa-f]{7,64}", sha):
        return False
    try:
        r = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _safe_path(path: str) -> bool:
    return bool(path) and not path.startswith(("/", "-")) and ".." not in Path(path).parts


def _show(root: Path, rev: str, path: str) -> str | None:
    lines = _git(root, ["show", f"{rev}:{path}"], max_lines=20_000)
    return None if lines is None else "\n".join(lines)


_DEFINITION = re.compile(
    r"\b(const|define|function|class|interface|trait|enum|def|var|let|type|global|window\.)\b"
    r"|^\s*[\w$.\[\]'\"-]+\s*[:=]\s*", re.I)


def _grep_blocks(root: Path, rev: str, symbol: str,
                 exclude: list[str] | None = None) -> list[str] | None:
    """`git grep -n -F` for `symbol` at `rev`: up to MAX_HITS_PER_SYMBOL match
    blocks (match plus a line of context each side), definition-looking ones
    first. None = git could not run; [] = it ran and nothing matched."""
    query = symbol.lstrip("#.")
    lines = _git(root, ["grep", "-n", "-F", "-I", "-C", "1", "-e", query, rev,
                        "--", ".", *_EXCLUDE_PATHSPECS, *(exclude or [])])
    if lines is None:
        return None
    prefix = f"{rev}:"
    blocks: list[list[str]] = [[]]
    for ln in lines:
        if ln == "--":
            blocks.append([])
            continue
        blocks[-1].append(ln[len(prefix):] if ln.startswith(prefix) else ln)
    blocks = [b for b in blocks if b]

    def rank(b: list[str]) -> int:
        match_lines = [x for x in b if re.match(r"^.+?:\d+:", x)]
        return 0 if any(_DEFINITION.search(x) for x in match_lines) else 1

    blocks.sort(key=rank)  # stable: file order kept inside a rank
    return ["\n".join(b) for b in blocks[:MAX_HITS_PER_SYMBOL]]


# ─── Callee bodies ───────────────────────────────────────────────────

#: Lines on each side of the finding's line scanned for calls.
CALLEE_RADIUS = 14

#: Callees whose body is shown per finding, and the candidates tried to find
#: them (a candidate that is a built-in or a library call has no definition
#: in the repository and costs one search to find that out).
MAX_CALLEES = 4
MAX_CALLEE_CANDIDATES = 8

#: Lines and characters of one callee's body shown. The character cap is
#: also halved against what is left, so one long callee cannot starve the
#: short ones behind it.
CALLEE_BODY_LINES = 30
CALLEE_BODY_CHARS = 900

_CALL_SITE = re.compile(r"(?:->|::|(?<![\w$]))(\$?[A-Za-z_]\w*)[ \t]*\(")
_NOT_A_CALLEE = frozenset([
    "if", "elseif", "else", "while", "for", "foreach", "switch", "catch", "function",
    "fn", "match", "array", "list", "isset", "empty", "unset", "echo", "print",
    "return", "new", "exit", "die", "include", "require", "include_once",
    "require_once", "use", "and", "or", "not", "def", "class", "typeof", "await",
    "async", "super", "self", "static", "parent",
])


def callee_candidates(lines: list[str], line: int, radius: int = CALLEE_RADIUS) -> list[str]:
    """Names called within `radius` lines of `line`, nearest first, deduplicated.

    A call is `name(`, `$name(` (a closure held in a variable), `->name(` or
    `::name(`. Definitions (`function name(`, `def name(`), comments and
    keywords are not calls. This is a shallow lexical scan on purpose: it
    only nominates names, and a nominee that has no definition in the
    repository (a built-in, a library) is dropped by the lookup that follows.
    """
    if not lines:
        return []
    centre = min(max(int(line or 1), 1), len(lines))
    order = sorted(range(max(1, centre - radius), min(len(lines), centre + radius) + 1),
                   key=lambda n: (abs(n - centre), n))
    seen: dict[str, None] = {}
    for n in order:
        text = lines[n - 1]
        if text.lstrip().startswith(("//", "#", "*", "/*")):
            continue
        for m in _CALL_SITE.finditer(text):
            name = m.group(1)
            before = text[:m.start()].rstrip()
            if before.endswith(("function", "def", "func", "fn", "new")):
                continue
            if name.lstrip("$").lower() in _NOT_A_CALLEE or len(name.lstrip("$")) < 2:
                continue
            seen.setdefault(name, None)
    return list(seen)


def definition_pattern(name: str) -> str:
    """POSIX-ERE for the line that defines `name` (function, method, closure)."""
    bare = re.escape(name.lstrip("$"))
    sp = "[[:space:]]"
    if name.startswith("$"):
        return rf"\${bare}{sp}*={sp}*(static{sp}+)?(function|fn)([^A-Za-z0-9_]|$)"
    return (rf"(function|def|func|fn){sp}+&?{bare}{sp}*[(<]"
            rf"|(const|let|var){sp}+{bare}{sp}*={sp}*(async{sp}*)?(function|\()"
            rf"|(^|[^A-Za-z0-9_]){bare}{sp}*:{sp}*(async{sp}*)?function")


def find_definition_line(lines: list[str], name: str) -> int | None:
    """1-based line in `lines` where `name` is defined, or None."""
    bare = re.escape(name.lstrip("$"))
    if name.startswith("$"):
        rx = re.compile(rf"\${bare}\s*=\s*(?:static\s+)?(?:function|fn)\b")
    else:
        rx = re.compile(
            rf"\b(?:function|def|func|fn)\s+&?{bare}\s*[(<]"
            rf"|\b(?:const|let|var)\s+{bare}\s*=\s*(?:async\s*)?(?:function|\()"
            rf"|\b{bare}\s*:\s*(?:async\s*)?function")
    for n, text in enumerate(lines, 1):
        if not text.lstrip().startswith(("//", "#", "*")) and rx.search(text):
            return n
    return None


def body_from(lines: list[str], start: int, limit: int = CALLEE_BODY_LINES) -> str:
    """Numbered body of the definition that begins at 1-based `start`.

    Brace languages end where the braces balance; indentation languages where
    the indent returns to the definition's level; either way at `limit`
    lines, with a marker when cut. The body is what answers "does this
    return, throw or exit?", so it is cut at the end of the callee and never
    in the middle of it unless it is longer than `limit`.
    """
    last = min(len(lines), start + limit - 1)
    end, closed = last, False
    head = "\n".join(lines[start - 1:min(len(lines), start + 3)])
    if "{" in head:
        depth, opened = 0, False
        for n in range(start, last + 1):
            for ch in lines[n - 1]:
                if ch == "{":
                    depth, opened = depth + 1, True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                end, closed = n, True
                break
    else:
        base = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        for n in range(start + 1, last + 1):
            t = lines[n - 1]
            if t.strip() and len(t) - len(t.lstrip()) <= base:
                end, closed = n - 1, True
                break
        closed = closed or last >= len(lines)
    body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
    return body if closed else body + "\n…[body continues]"


def callee_blocks(
    text: str,
    path: str,
    line: int,
    budget: int,
    repo_definition: Callable[[str], tuple[str, int] | None],
    read_file: Callable[[str], tuple[str, str] | None],
) -> list[str]:
    """Rendered bodies of the functions called next to `line` in `text`.

    Nearest call first. A callee defined in the same file is taken from `text`
    (a closure in a variable is only ever found there); any other must have
    exactly one definition in the repository. Never raises: a callee that
    cannot be resolved is skipped, which is less evidence, not an error.
    """
    if budget < 200:
        return []
    lines = text.splitlines()
    out: list[str] = []
    for name in callee_candidates(lines, line)[:MAX_CALLEE_CANDIDATES]:
        if len(out) >= MAX_CALLEES or budget < 120:
            break
        try:
            where, src, src_lines = path, text, lines
            at = find_definition_line(lines, name)
            if at is None and not name.startswith("$"):
                hit = repo_definition(name)
                if hit is None:
                    continue
                got = read_file(hit[0]) if _safe_path(hit[0]) else None
                if got is None:
                    continue
                where, src = hit[0], got[0]
                src_lines = src.splitlines()
                at = hit[1]
            if at is None:
                continue
            block = (f"`{name}` is defined at {where}:{at}:\n```\n"
                     f"{_clip(body_from(src_lines, at), min(CALLEE_BODY_CHARS, max(200, budget // 2)))}\n```")
        except Exception as exc:  # noqa: BLE001 - one callee never fails the rest
            logger.info("verifier_evidence_callee_failed name=%s err=%s", name, exc)
            continue
        out.append(block)
        budget -= len(block)
    return out


# ─── Window ──────────────────────────────────────────────────────────


def _window(text: str, line: int, budget: int) -> str:
    """Numbered lines around `line`, ±WINDOW_RADIUS, shrunk to fit `budget`."""
    lines = text.splitlines()
    if not lines:
        return ""
    center = min(max(int(line or 1), 1), len(lines))
    radius = WINDOW_RADIUS
    while True:
        lo, hi = max(1, center - radius), min(len(lines), center + radius)
        rendered = "\n".join(f"{n}: {lines[n - 1]}" for n in range(lo, hi + 1))
        if len(rendered) <= budget or radius <= 8:
            return _clip(rendered, budget)
        radius = max(8, radius - 10)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)].rstrip() + "\n…[truncated]"


# ─── Gathering ───────────────────────────────────────────────────────


def _default_app_settings() -> Any:
    from src.config import get_settings
    return get_settings()


def _local_root(pr: Any, app_settings: Any) -> Path | None:
    """The clone for this pull request's repository, or None.

    Reuses the code graph's notion (`graph_context._clone_root`) and its slug
    candidates, so a Bitbucket repository cloned before slugs carried a
    provider prefix is still found."""
    try:
        from src.review.graph_context import _clone_root, graph_slug_candidates
        for slug in graph_slug_candidates(pr):
            root = _clone_root(slug, app_settings)
            if root is not None and (root / ".git").exists():
                return root
    except Exception as exc:  # noqa: BLE001
        logger.info("verifier_evidence_no_clone err=%s", exc)
    return None


def gather_evidence(
    findings: list[Any],
    pr: Any,
    *,
    provider: Any | None = None,
    app_settings: Any | None = None,
    total_budget: int = TOTAL_BUDGET_CHARS,
    per_finding_cap: int = PER_FINDING_CAP_CHARS,
    deadline_seconds: float = GATHER_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> EvidenceBundle:
    """Evidence for each of `findings`, index-aligned. Never raises.

    The budget is split evenly across the findings (never more than
    `per_finding_cap` each) and each finding's share is split about 40 / 15 /
    20 / 25 between the file window, its hunks, the symbol hits and the bodies of the
    functions called next to the line. A share a
    finding does not use is not handed on: the cap is a ceiling on the prompt,
    not a quota to fill.
    """
    bundle = EvidenceBundle()
    stats: dict[str, Any] = {
        "findings": len(findings), "with_window": 0, "windows_clone": 0,
        "windows_provider": 0, "symbols": 0, "symbols_with_hits": 0,
        "symbols_searched_empty": 0, "searches_clone": 0,
        "searches_provider": 0, "errors": 0, "chars": 0, "callees": 0,
    }
    bundle.stats = stats
    if not findings:
        return bundle
    t_end = clock() + deadline_seconds
    try:
        app_settings = app_settings if app_settings is not None else _default_app_settings()
    except Exception:  # noqa: BLE001
        app_settings = None
    root = _local_root(pr, app_settings) if app_settings is not None else None
    head = str(getattr(pr, "head_sha", "") or "")
    revision = resolve_code_revision(pr, root)
    has_head = revision.kind == KIND_HEAD
    # Overlay state for "base + diff": the touched files are rebuilt from the
    # base text and the diff, everything else is read from base as it is.
    overlay = revision.needs_overlay
    by_path = hunks_by_new_path(pr) if overlay else {}
    touched = touched_base_paths(pr) if overlay else set()
    overlay_ok = overlay and len(touched) <= MAX_OVERLAY_FILES
    stats["clone"] = {KIND_HEAD: "head commit", KIND_BASE_PLUS_DIFF: "base + diff",
                      KIND_CLONE_HEAD: "HEAD only (newer than the PR)"}.get(
                          revision.kind, "none")
    # Below ~600 characters a share holds a window of three lines and proves
    # nothing; past that many findings the first ones get evidence and the
    # rest are judged without it (and so kept, by the prompt's own rule).
    share = min(per_finding_cap, total_budget // len(findings))
    funded = len(findings)
    if share < 600:
        share, funded = 600, max(1, total_budget // 600)
    stats["unfunded"] = max(0, len(findings) - funded)

    file_cache: dict[str, tuple[str, str] | None] = {}
    search_cache: dict[str, SymbolHits] = {}
    provider_searches = 0

    def head_text_from_base(path: str) -> str | None:
        """`path` as it is at the PR head, rebuilt from the base commit and the
        diff; None when it cannot be (deleted, hunks do not fit)."""
        hunks = by_path.get(path)
        if not hunks:
            return _show(root, revision.rev, path) if path not in touched else None
        if any(getattr(h, "is_deleted_file", False) for h in hunks):
            return None
        new_file = any(getattr(h, "is_new_file", False) for h in hunks)
        old_path = getattr(hunks[0], "old_file_path", "") or path
        base_text = None if new_file else _show(root, revision.rev, old_path)
        if base_text is None and not new_file:
            return None
        return overlay_file(base_text, hunks)

    def overlaid_blocks(sym: str) -> list[str] | None:
        """Matches for `sym` in the files the diff touches, as they are at head."""
        query = sym.lstrip("#.")
        out: list[str] = []
        for path in sorted(by_path):
            text = head_text_from_base(path)
            if text is None:
                if any(getattr(h, "is_deleted_file", False) for h in by_path[path]):
                    continue
                return None  # a touched file we cannot rebuild: not a complete answer
            rows = text.split("\n")
            for n, row in enumerate(rows, 1):
                if query in row:
                    lo, hi = max(1, n - 1), min(len(rows), n + 1)
                    out.append("\n".join(
                        f"{path}{':' if k == n else '-'}{k}{':' if k == n else '-'}{rows[k - 1]}"
                        for k in range(lo, hi + 1)))
                    if len(out) >= MAX_HITS_PER_SYMBOL:
                        return out
        return out

    def read_file(path: str) -> tuple[str, str] | None:
        if path in file_cache:
            return file_cache[path]
        got: tuple[str, str] | None = None
        if root is not None and has_head and _safe_path(path):
            text = _show(root, head, path)
            if text is not None:
                got = (text, f"clone @ {head[:10]}")
                stats["windows_clone"] += 1
        elif root is not None and overlay and _safe_path(path):
            text = head_text_from_base(path)
            if text is not None:
                got = (text, revision.label)
                stats["windows_clone"] += 1
        if got is None and provider is not None and head and clock() < t_end:
            try:
                text = provider.read_file_at(pr.repo, head, path)
                if text is not None:
                    got = (text, f"provider @ {head[:10]}")
                    stats["windows_provider"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                logger.info("verifier_evidence_read_failed path=%s err=%s", path, exc)
        file_cache[path] = got
        return got

    def search(sym: str) -> SymbolHits:
        nonlocal provider_searches
        if sym in search_cache:
            return search_cache[sym]
        res = SymbolHits(symbol=sym)
        search_cache[sym] = res
        if clock() >= t_end:
            return res
        if root is not None and revision.kind != KIND_NONE:
            rev, label = revision.rev, revision.label
            try:
                if overlay:
                    blocks = _grep_blocks(
                        root, rev, sym,
                        exclude=[f":(exclude,literal){p}" for p in sorted(touched)]
                        if overlay_ok else None)
                    extra = overlaid_blocks(sym) if overlay_ok else None
                    if blocks is None or extra is None:
                        # Not a complete answer about the PR's code.
                        blocks = None
                    else:
                        blocks = (extra + blocks)[:MAX_HITS_PER_SYMBOL]
                else:
                    blocks = _grep_blocks(root, rev, sym)
            except Exception as exc:  # noqa: BLE001
                blocks = None
                stats["errors"] += 1
                logger.info("verifier_evidence_grep_failed sym=%s err=%s", sym, exc)
            if blocks is not None:
                stats["searches_clone"] += 1
                res.source, res.searched, res.blocks = label, True, blocks
                res.pr_code = revision.is_pr_code
                # Zero hits at the PR's own code is an answer; zero at a
                # stand-in commit is not enough to stop asking the provider.
                if blocks or revision.is_pr_code:
                    return res
        if (provider is not None and provider_searches < MAX_PROVIDER_SEARCHES
                and clock() < t_end):
            provider_searches += 1
            try:
                hits = provider.search_code(pr.repo, sym.lstrip("#."), MAX_HITS_PER_SYMBOL)
                stats["searches_provider"] += 1
                res.source = "provider code search (default branch, not the PR head)"
                res.searched = True
                res.pr_code = False
                res.blocks = [
                    f"{h.path}\n" + "\n".join(h.lines[:3]) if h.lines else h.path
                    for h in hits[:MAX_HITS_PER_SYMBOL]
                ]
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                logger.info("verifier_evidence_search_failed sym=%s err=%s", sym, exc)
        return res

    def repo_definition(name: str) -> tuple[str, int] | None:
        """(path, line) of the ONE definition of `name` in the repository, or
        None when there is none, several (a method name shared by many
        classes: showing one of them would mislead), or no clone."""
        if root is None or clock() >= t_end:
            return None
        rev = revision.rev or "HEAD"
        skip = ([f":(exclude,literal){p}" for p in sorted(touched)] if overlay_ok else [])
        out = _git(root, ["grep", "-n", "-I", "-E", "-e", definition_pattern(name), rev,
                          "--", ".", *_EXCLUDE_PATHSPECS, *skip], max_lines=6)
        if not out:
            return None
        found = []
        for ln in out:
            parts = ln[len(rev) + 1:].split(":", 2) if ln.startswith(f"{rev}:") else []
            if len(parts) == 3 and parts[1].isdigit():
                found.append((parts[0], int(parts[1])))
        return found[0] if len(set(found)) == 1 else None

    for idx, f in enumerate(findings):
        ev = FindingEvidence()
        bundle.items.append(ev)
        if idx >= funded:
            continue
        try:
            path = str(getattr(f, "file_path", "") or "")
            win_budget, hunk_budget = int(share * .40), int(share * .15)
            hit_budget, callee_budget = int(share * .20), int(share * .25)
            if path and _safe_path(path):
                got = read_file(path)
                if got is not None:
                    ev.window = _window(got[0], int(getattr(f, "line", 0) or 0), win_budget)
                    ev.window_source = got[1]
                    if not revision.is_pr_code and got[1] == revision.label:
                        ev.non_pr_source = True
                    stats["with_window"] += 1
                    ev.callees = callee_blocks(got[0], path, int(getattr(f, "line", 0) or 0),
                                               callee_budget, repo_definition, read_file)
                    stats["callees"] += len(ev.callees)
                    if ev.callees and not revision.is_pr_code:
                        ev.non_pr_source = True
                    try:
                        ev.lookups = resolve_lookups(
                            got[0].splitlines(), int(getattr(f, "line", 0) or 0))
                    except Exception as exc:  # noqa: BLE001 - evidence, fail open
                        logger.info("verifier_lookups_failed err=%s", exc)
                    stats["lookups"] = stats.get("lookups", 0) + len(ev.lookups)
            hunk_texts = [h.content for h in getattr(pr, "anchor_hunks", None) or pr.hunks
                          if getattr(h, "file_path", "") == path]
            ev.hunks = _clip("\n".join(hunk_texts), hunk_budget)
            ev.symbols = extract_symbols(
                str(getattr(f, "title", "")), str(getattr(f, "body", "")),
                str(getattr(f, "reasoning", "")))
            stats["symbols"] += len(ev.symbols)
            used = 0
            for sym in ev.symbols:
                hits = search(sym)
                remaining = hit_budget - used
                kept: list[str] = []
                for blk in hits.blocks:
                    blk = _clip(blk, max(0, min(600, remaining)))
                    if remaining <= 40 or not blk:
                        break
                    kept.append(blk)
                    remaining -= len(blk)
                    used += len(blk)
                ev.hits.append(SymbolHits(sym, hits.source, kept, hits.searched,
                                          hits.pr_code))
                if kept and not hits.pr_code:
                    ev.non_pr_source = True
                if kept:
                    stats["symbols_with_hits"] += 1
                elif hits.searched:
                    stats["symbols_searched_empty"] += 1
        except Exception as exc:  # noqa: BLE001 - fail open, per finding
            stats["errors"] += 1
            logger.warning("verifier_evidence_failed err=%s", exc)
    stats["chars"] = sum(e.chars for e in bundle.items)
    return bundle


def render_evidence(ev: FindingEvidence | None, *, lang_hint: str = "") -> str:
    """The prompt text for one finding's evidence; "" when there is none."""
    if ev is None:
        return ""
    parts: list[str] = []
    if ev.window:
        warn = ("\n_WARNING: this is NOT the pull request's code - it is newer than "
                "the pull request. Nothing found only here refutes the claim._"
                if ev.non_pr_source and "NEWER" in ev.window_source else "")
        parts.append(f"**File around the line ({ev.window_source}):**{warn}\n"
                     f"```{lang_hint}\n{ev.window}\n```")
    if ev.hunks:
        parts.append(f"**This PR's changes to the file:**\n```diff\n{ev.hunks}\n```")
    if ev.callees:
        parts.append("**Bodies of functions called near the line (what they do "
                     "when called):**\n" + "\n".join(ev.callees))
    if ev.lookups:
        parts.append("**Lookups near the line, evaluated against the array/map "
                     "literal that defines them (computed by the tool, not "
                     "read by a model; compare these with the claim):**\n```\n"
                     + "\n".join(ev.lookups) + "\n```")
    sym_lines: list[str] = []
    for h in ev.hits:
        if h.blocks:
            body = "\n---\n".join(h.blocks)
            warn = ("" if h.pr_code else
                    " - NOT THE PULL REQUEST'S CODE (newer than it): a definition or "
                    "usage found only here proves nothing about the PR and must not "
                    "be a reason to drop the finding")
            sym_lines.append(f"`{h.symbol}` found [{h.source}]{warn}:\n```\n{body}\n```")
        elif h.searched and h.source.startswith("clone @"):
            sym_lines.append(f"`{h.symbol}`: NO match anywhere in the repository "
                             f"[{h.source}].")
        elif h.searched:
            sym_lines.append(f"`{h.symbol}`: no hit [{h.source}] — this proves "
                             "nothing about the PR head.")
        else:
            sym_lines.append(f"`{h.symbol}`: could not be searched.")
    if sym_lines:
        parts.append("**Repository evidence (symbols the claim names):**\n"
                     + "\n".join(sym_lines))
    return "\n\n".join(parts)

