"""`grep`: ranked text search in the indexed revision, committed files only."""

from __future__ import annotations

import re
import time

from src.mcp_server.dev_profile import common, cursor, emit, git_io
from src.mcp_server.dev_profile.access import RepoNotAccessible

DEFAULT_LIMIT = 20
MAX_LIMIT = 60
PER_REPO_HITS = 60
LINE_CHARS = 160
CALL_DEADLINE = 15.0  # seconds: past it the remaining repositories are skipped and named
ENRICH_REPOS = 10  # repositories whose enclosing symbols are looked up per page
NAMED = 5  # slugs named in a note before "+N more"
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


def _named(slugs: list[str]) -> str:
    out = ", ".join(sorted(slugs)[:NAMED])
    return out + (f" +{len(slugs) - NAMED} more" if len(slugs) > NAMED else "")


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
    detailed = common.norm_format(response_format)
    limit = common.clamp(limit, 1, MAX_LIMIT)
    h = cursor.fingerprint(tool="grep", p=pat, repo=repo, g=path_glob, rx=regex,
                           limit=limit, detailed=detailed, u=scope.user_id,
                           w=scope.workspace_id)
    if repo and repo.strip():
        try:
            everyone = [scope.resolve(repo, need="code")]
        except RepoNotAccessible:
            return common.repo_error(scope, repo)
    else:
        # Shared repositories first: they open the first batch, so a token that
        # reaches many repositories reads the shared one on page 1.
        everyone = scope.slugs(need="code", shared_first=True)
    # The repositories are read in batches of `MAX_REPOS_PER_CALL`; the cursor
    # carries where the batch starts (and a digest of the repositories before it).
    start, moved = cursor.start_of(cursor_, h, everyone)
    cap = common.MAX_REPOS_PER_CALL
    slugs = everyone[start:start + cap]
    scope.note_shared(slugs)
    total_repos = len(everyone)
    end = start + len(slugs)
    note_lines: list[str] = []
    if total_repos > cap:
        nxt = min(cap, total_repos - end)
        then = f"cursor=<next> continues with the next {nxt}" if end < total_repos else \
            "this is the last part"
        note_lines.append(common.cut_note(len(slugs), total_repos, start=start, then=then,
                                          advice=common.REPO_TARGETS_ONE))
    fresh = {e.slug: e for e in common.fresh_entries(slugs)}
    ignore_case = pat == pat.lower() and not regex

    missing: list[str] = []
    timed_out: list[str] = []
    skipped: list[str] = []
    deadline = time.monotonic() + CALL_DEADLINE

    def one(slug: str) -> list[tuple]:
        if slug not in fresh:
            return []
        if time.monotonic() > deadline:
            skipped.append(slug)
            return []
        if not git_io.has_commit(slug, fresh[slug].sha):
            missing.append(slug)  # git would answer "no matches": say so instead
            return []
        hits = git_io.grep(slug, fresh[slug].sha, pat, regex=regex, path_glob=path_glob,
                           max_hits=PER_REPO_HITS, ignore_case=ignore_case,
                           timed_out=timed_out)
        return [(slug, x) for x in hits if scope.readable(slug, x.path)]

    flat = [t for part in common.map_repos(slugs, one).values() for t in part]
    flat.sort(key=lambda t: (path_class(t[1].path), 0 if _DEF.match(t[1].text) else 1,
                             t[0], t[1].path, t[1].line))
    for slug in sorted(missing):
        note_lines.append(f"{slug}: revision {fresh[slug].sha[:8]} not in the clone, "
                          "nothing searched; ask an admin to re-index")
    unindexed = [s for s in slugs if s not in fresh]
    if unindexed:
        note_lines.append(f"{len(unindexed)} of these have no index yet, nothing searched: "
                          f"{_named(unindexed)}")
    late = sorted(set(timed_out) | set(skipped))
    if late:
        note_lines.append(f"{len(late)} repos timed out or were skipped (call deadline "
                          f"{int(CALL_DEADLINE)} s), hits may be missing: {_named(late)}; "
                          "repeat the call or use repo=")
    # Pinned to every repository of the batch, not only those with hits: a push
    # to one of them can add hits ahead of the reader's offset.
    pin = {s: e.sha for s, e in fresh.items()}
    if moved:
        off, note = 0, moved
    else:
        off, note = cursor.resolve(cursor_, h, pin)
        if note == cursor.STALE_NOTE and start:
            note = cursor.STALE_PART_NOTE
    page = flat[off:off + limit]
    by_repo: dict[str, list[tuple]] = {}
    for slug, x in page:
        by_repo.setdefault(slug, []).append((x.path, x.line))
    names: dict[str, dict] = {s: _enclosing(s, rows) for s, rows in list(by_repo.items())[:ENRICH_REPOS]}
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
    then = ""
    if end < total_repos:
        nxt = cursor.encode(h, 0, {}, batch=end, before=everyone[:end])
        then = (f"… repositories {end + 1}-{min(end + cap, total_repos)} of {total_repos} "
                f"not searched yet · cursor={nxt}")
    in_part = " in these repositories" if total_repos > cap else ""
    return emit.render_list(
        "grep", entries, lines, total=len(flat), offset=off, query_hash=h, note=note,
        head=note_lines,
        empty="nothing searched" if missing and not flat else
        f"no committed text matches this pattern{in_part} (secret files are never searched)",
        more_hint=MORE_HINT, detailed=detailed, raised=limit > DEFAULT_LIMIT, pin=pin,
        batch=start, before=everyone[:start], then=then)
