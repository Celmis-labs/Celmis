"""HTTP routes of a repository, read from source text at the indexed revision.

No extractor records decorators or router calls, so the symbol graph cannot
answer "which endpoints does this service expose". This module answers it the
way a person would: look for the framework's registration syntax.

It is a HEURISTIC and says so: one `git grep` per repository finds candidate
lines, each line is parsed for method and path, and the handler is the next
declaration after a decorator (Python, PHP attributes) or the identifier passed
to the call (Express, Laravel, Go). A route registered through a loop, a
constant or a framework this module does not know is not found. Callers get a
`framework` per route so they can tell what was recognised.

Frameworks: FastAPI / Flask / Starlette decorators, Express / Fastify
`router.get(`, Laravel `Route::get(`, Symfony `#[Route(`, Go `HandleFunc` and
gin/echo `.GET(`.

The module does no I/O of its own: `grep` and `show` are passed in, so it is
usable from any layer and testable without a git checkout.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

#: One regex for every framework: a single `git grep` pass per repository.
CANDIDATE_REGEX = (
    r"^[[:space:]]*@[A-Za-z_][A-Za-z0-9_.]*\."
    r"(get|post|put|delete|patch|head|options|route|websocket|api_route)\("
    r"|(router|app|route|routes|server|api|fastify)\."
    r"(get|post|put|delete|patch|all|head|options)\([[:space:]]*['\"`]"
    r"|Route::[A-Za-z]+\("
    r"|#\[Route\("
    r"|(HandleFunc|Handle)\("
    r"|\.(GET|POST|PUT|DELETE|PATCH)\([[:space:]]*\""
)

SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".php",
                   ".go", ".java", ".kt")

_STR = r"""['"`]([^'"`]*)['"`]"""
_PY_DECORATOR = re.compile(
    r"^\s*@[A-Za-z_][\w.]*\.(get|post|put|delete|patch|head|options|route|websocket|api_route)\(")
_JS_CALL = re.compile(
    r"\b(?:router|app|route|routes|server|api|fastify)\."
    r"(get|post|put|delete|patch|all|head|options)\(\s*" + _STR)
_LARAVEL = re.compile(r"Route::([A-Za-z]+)\(\s*" + _STR)
_SYMFONY = re.compile(r"#\[Route\(\s*" + _STR)
_GO_STD = re.compile(r"\b(?:HandleFunc|Handle)\(\s*\"([^\"]*)\"\s*,\s*([A-Za-z_][\w.]*)?")
_GO_GIN = re.compile(r"\.(GET|POST|PUT|DELETE|PATCH)\(\s*\"([^\"]*)\"\s*,\s*([A-Za-z_][\w.]*)?")
_METHODS_ARG = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
_DECL = re.compile(
    r"^\s*(?:(?:public|private|protected|static|async|export|default)\s+)*"
    r"(?:async\s+def|def|function|func(?:\s*\([^)]*\))?)\s+([A-Za-z_]\w*)")
_JS_HANDLER = re.compile(r",\s*(?:async\s+)?([A-Za-z_][\w.]*)\s*\)\s*;?\s*$")
_LARAVEL_HANDLER = re.compile(r"\[\s*([\w\\]+)::class\s*,\s*['\"](\w+)['\"]\s*\]")
_LARAVEL_VERBS = {"get", "post", "put", "delete", "patch", "any", "options", "match"}
_MAX_DECL_LOOKAHEAD = 12


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: str
    file: str
    line: int
    framework: str

    def as_dict(self) -> dict:
        return asdict(self)


GrepFn = Callable[..., list]
ShowFn = Callable[[str], list[str] | None]


def _next_declaration(lines: list[str] | None, line: int) -> str:
    """Name of the first function declared after the decorator at `line`."""
    if not lines:
        return ""
    for text in lines[line: line + _MAX_DECL_LOOKAHEAD]:  # `line` is 1-based: [line] is the next one
        m = _DECL.match(text)
        if m:
            return m.group(1)
    return ""


def _parse(text: str, path: str, line: int, lines: list[str] | None) -> list[Route]:
    m = _PY_DECORATOR.match(text)
    if m:
        verb = m.group(1)
        sm = re.search(_STR, text)
        if sm is None and lines:  # `@router.post(` with the path on the next line
            sm = re.search(_STR, " ".join(lines[line: line + 3]))
        route_path = sm.group(1) if sm else ""
        methods = [verb.upper()]
        if verb in {"route", "api_route"}:
            mm = _METHODS_ARG.search(text)
            methods = ([x.strip(" '\"").upper() for x in mm.group(1).split(",") if x.strip(" '\"")]
                       if mm else ["GET"])
        elif verb == "websocket":
            methods = ["WS"]
        handler = _next_declaration(lines, line)
        return [Route(me, route_path, handler, path, line, "python-decorator") for me in methods]
    m = _JS_CALL.search(text)
    if m:
        hm = _JS_HANDLER.search(text)
        return [Route(m.group(1).upper(), m.group(2), hm.group(1) if hm else "(inline)",
                      path, line, "express")]
    m = _LARAVEL.search(text)
    if m and m.group(1).lower() in _LARAVEL_VERBS:
        hm = _LARAVEL_HANDLER.search(text)
        handler = f"{hm.group(1).rsplit(chr(92), 1)[-1]}::{hm.group(2)}" if hm else "(inline)"
        return [Route(m.group(1).upper(), m.group(2), handler, path, line, "laravel")]
    m = _SYMFONY.search(text)
    if m:
        return [Route("ANY", m.group(1), _next_declaration(lines, line), path, line, "symfony")]
    m = _GO_GIN.search(text)
    if m:
        return [Route(m.group(1), m.group(2), m.group(3) or "(inline)", path, line, "go-gin")]
    m = _GO_STD.search(text)
    if m:
        return [Route("ANY", m.group(1), m.group(2) or "(inline)", path, line, "go-http")]
    return []


def extract_routes(
    grep: GrepFn, show: Callable[[str], list[str] | None], *,
    path_filter: Callable[[str], bool] | None = None, max_routes: int = 300,
) -> list[Route]:
    """Routes found by `grep(CANDIDATE_REGEX)`, ordered by file and line.

    `grep(pattern)` returns objects with `.path`, `.line`, `.text`; `show(path)`
    returns a file's lines (for decorator handlers). `path_filter` drops files
    the caller may not see. Deduplicated on (method, path, file, line).
    """
    hits = grep(CANDIDATE_REGEX)
    cache: dict[str, list[str] | None] = {}
    seen: set[tuple] = set()
    routes: list[Route] = []
    for h in sorted(hits, key=lambda x: (x.path, x.line)):
        if not h.path.endswith(SOURCE_SUFFIXES):
            continue
        if path_filter is not None and not path_filter(h.path):
            continue
        lines: list[str] | None = None
        if h.text.lstrip().startswith(("@", "#[")):
            if h.path not in cache:
                cache[h.path] = show(h.path)
            lines = cache[h.path]
        for r in _parse(h.text, h.path, h.line, lines):
            key = (r.method, r.path, r.file, r.line)
            if key in seen:
                continue
            seen.add(key)
            routes.append(r)
            if len(routes) >= max_routes:
                return routes
    return routes
