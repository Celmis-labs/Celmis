"""`grep`: ranked text search in the indexed revision, committed files only."""

from __future__ import annotations

import re

from src.mcp_server.dev_profile import common, cursor, emit, git_io
from src.mcp_server.dev_profile.access import RepoNotAccessible

DEFAULT_LIMIT = 20
MAX_LIMIT = 60
MAX_REPOS = 10
PER_REPO_HITS = 60
LINE_CHARS = 160
MORE_HINT = "repo= / path_glob= / a longer pattern"

_TEST = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|(_test\.|\.test\.|\.spec\.)", re.I)
_DOC = re.compile(r"\.(md|rst|txt|adoc)$", re.I)
_CONFIG = re.compile(
    r"\.(ya?ml|json|toml|ini|cfg|conf|properties|xml|gradle|tf|lock)$|"
    r"(^|/)(Dockerfile|docker-compose[^/]*|Makefile|\.github/|\.gitlab-ci)", re.I)
_DEF = re.compile(r"^\s*(def|class|function|func|fn|interface|type|const|let|var|public|private)\b")


def path_class(path: str) -> int:
    """0 source, 1 config, 2 tests, 3 docs (lower sorts first)."""
    if _DOC.search(path):
        return 3
    if _TEST.search(path):
        return 2
    if _CONFIG.search(path):
        return 1
    return 0


def _enclosing(slug: str, rows: list[tuple]) -> dict[tuple[str, int], str]:
    """(path, line) -> name of the smallest symbol whose span holds the line."""
    out: dict[tuple[str, int], str] = {}
    files = list(dict.fromkeys(p for p, _l in rows))[:20]
    try:
        with common.open_store(slug) as store:
            for f in files:
                syms = store.symbols_in_file(f)
                for p, ln in rows:
                    if p != f:
                        continue
                    best = None
                    for s in syms:
                        end = s.end_line or s.start_line
                        if s.start_line <= ln <= end and (
                                best is None or (end - s.start_line)
                                < ((best.end_line or best.start_line) - best.start_line)):
                            best = s
                    if best is not None:
                        out[(p, ln)] = best.name
    except Exception:  # noqa: BLE001 — enrichment only
        return out
    return out


def run(pattern: str, repo: str = "", path_glob: str = "", regex: bool = False,
        limit: int = DEFAULT_LIMIT, cursor_: str = "",
        response_format: str = "concise") -> str:
    scope = common.begin()
    pat = pattern or ""
    if not pat.strip():
        return emit.error(None, "pattern is empty")
    if regex:
        try:
            re.compile(pat)
        except re.error as exc:
            return emit.error(None, f"invalid regex: {exc}")
    if repo and repo.strip():
        try:
            slugs = [scope.resolve(repo, need="code")]
        except RepoNotAccessible:
            return common.repo_error(scope, repo)
    else:
        slugs = scope.slugs(need="code")
    note_lines: list[str] = []
    if len(slugs) > MAX_REPOS:
        note_lines.append(f"searched {MAX_REPOS} of {len(slugs)} repos; pass repo= to target one")
        slugs = slugs[:MAX_REPOS]
    detailed = common.norm_format(response_format)
    limit = common.clamp(limit, 1, MAX_LIMIT)
    fresh = {e.slug: e for e in common.fresh_entries(slugs)}
    h = cursor.fingerprint(tool="grep", p=pat, repo=repo, g=path_glob, rx=regex,
                           limit=limit, detailed=detailed)
    ignore_case = pat == pat.lower() and not regex

    missing: list[str] = []

    def one(slug: str) -> list[tuple]:
        if slug not in fresh:
            return []
        if not git_io.has_commit(slug, fresh[slug].sha):
            missing.append(slug)  # git would answer "no matches": say so instead
            return []
        hits = git_io.grep(slug, fresh[slug].sha, pat, regex=regex, path_glob=path_glob,
                           max_hits=PER_REPO_HITS, ignore_case=ignore_case)
        return [(slug, x) for x in hits if scope.readable(slug, x.path)]

    flat = [t for part in common.map_repos(slugs, one).values() for t in part]
    flat.sort(key=lambda t: (path_class(t[1].path), 0 if _DEF.match(t[1].text) else 1,
                             t[0], t[1].path, t[1].line))
    for slug in sorted(missing):
        note_lines.append(f"{slug}: revision {fresh[slug].sha[:8]} not in the clone, "
                          "nothing searched; ask an admin to re-index")
    pin = {s: fresh[s].sha for s in dict.fromkeys(t[0] for t in flat) if s in fresh}
    off, note = cursor.resolve(cursor_, h, pin)
    page = flat[off:off + limit]
    by_repo: dict[str, list[tuple]] = {}
    for slug, x in page:
        by_repo.setdefault(slug, []).append((x.path, x.line))
    names: dict[str, dict] = {s: _enclosing(s, rows) for s, rows in by_repo.items()}
    lines = []
    for slug, x in page:
        text = emit.clip(x.text, LINE_CHARS)
        line = f"{slug} {x.path}:{x.line}  {text}"
        enc = names.get(slug, {}).get((x.path, x.line))
        if enc:
            line += f"  [in {enc}]"
        lines.append(line)
    shown = list(dict.fromkeys(s for s, _x in page)) or slugs
    entries = [fresh[s] for s in dict.fromkeys([*shown, *sorted(missing)]) if s in fresh]
    return emit.render_list(
        "grep", entries, lines, total=len(flat), offset=off, query_hash=h, note=note,
        head=note_lines,
        empty="nothing searched" if missing and not flat else
        "no committed text matches this pattern (secret files are never searched)",
        more_hint=MORE_HINT, detailed=detailed, raised=limit > DEFAULT_LIMIT, pin=pin)
