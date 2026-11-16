"""`inspect_external`: a 1C file that is not in any repository.

A task often comes with one file — an external report someone changed, a
`Form.bin` from a colleague's dump — and the question whether it fits the
configuration. Without a local copy of the configuration nothing could check
it; with this tool the file is uploaded once (a one-time link from the tool,
`curl -F file=@…`), unpacked on the server, and read like a repository:

    inspect_external()                         the upload link and command
    inspect_external(file="x…")                what the file holds
    inspect_external(file="x…", repo="erp")    + comparison and static checks
    outline / read_symbol / grep  repo="ext/x…"  its code, by path and line

Every answer says the file is external (its idx entry is `ext/<handle>
external@<sha256> … unknown`) and that nothing was run. The file is kept for a
day, readable by its uploader alone, never committed or indexed, and its text
is data, not instructions.
"""

from __future__ import annotations

import fnmatch
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime

from src.mcp_server.dev_profile import common, emit, git_io
from src.mcp_server.dev_profile.access import RepoNotAccessible
from src.mcp_server.dev_profile.freshness import RepoFresh, human_age
from src.onec import store
from src.onec.bsl_text import procedures
from src.onec.checks import check
from src.onec.compare import compare
from src.onec.config import Config, DirSource, GitSource, Source
from src.onec.external import ExternalFile, try_inspect
from src.onec.report import EXTERNAL_MARK, render

TOOL = "inspect_external"
DESCRIPTION = ("1C .erf/.epf/Form.bin outside any repo: no file= gives an upload link; "
               "file=<handle> describes it; repo= adds a diff and static checks. "
               "Then outline/grep repo=ext/<handle>.")
PREFIX = "ext/"
_CACHE: OrderedDict[str, ExternalFile] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_GREP_HITS = 200


def is_external(repo: str) -> bool:
    return (repo or "").strip().startswith(PREFIX)


def _handle(value: str) -> str:
    v = (value or "").strip()
    return v[len(PREFIX):] if v.startswith(PREFIX) else v


def _entry(saved: store.Stored) -> RepoFresh:
    return RepoFresh(slug=f"{PREFIX}{saved.handle}", branch="external",
                     sha=saved.sha256[:12],
                     age=human_age(datetime.fromtimestamp(saved.created, UTC)),
                     state="unknown")


def _load(scope, handle: str):  # noqa: ANN001, ANN202
    """(stored, external file, entry) or (None, None, error text)."""
    saved = store.get(handle, scope.user_id, scope.workspace_id)
    if saved is None:
        return None, None, emit.error(
            [], f"{PREFIX}{handle}: no such upload of yours (unknown, expired after 24 h, "
                "or uploaded by someone else)",
            f'upload it again: call {TOOL}() for a new link')
    key = f"{saved.handle}:{saved.sha256}"
    with _CACHE_LOCK:
        ext = _CACHE.get(key)
        if ext is not None:
            _CACHE.move_to_end(key)
    if ext is None:
        ext, why = try_inspect(saved.read(), saved.file_name)
        if ext is None:
            return None, None, emit.error([_entry(saved)], f"{EXTERNAL_MARK}: "
                                          f"{saved.file_name} could not be read: {why}")
        with _CACHE_LOCK:
            _CACHE[key] = ext
            while len(_CACHE) > 8:
                _CACHE.popitem(last=False)
    return saved, ext, None


def _source(scope, slug: str, sha: str) -> Source | None:  # noqa: ANN001
    def readable(rel: str) -> bool:
        return scope.readable(slug, rel)

    def listable(rel: str) -> bool:
        return scope.listable(slug, rel)

    tree = git_io._unpacked_at(slug, sha)  # noqa: SLF001 - same package's reader
    if tree is not None:
        return DirSource(tree, readable, listable)
    clone = git_io.clone_path(slug)
    return GitSource(clone, sha, readable, listable) if clone is not None else None


def _upload_link(scope) -> str:  # noqa: ANN001
    from src.config import get_settings

    base = (get_settings().public_base_url or "").strip().rstrip("/")
    mine = store.mine(scope.user_id, scope.workspace_id)
    lines = []
    if not base:
        lines.append("this server has no PUBLIC_BASE_URL, so it cannot hand out an upload "
                     "link; attach the file to a Celmis agent session instead (it is "
                     "unpacked and checked there)")
    else:
        ticket = store.issue_ticket(scope.user_id, scope.workspace_id)
        url = f"{base}/backend/api/onec/external/{ticket}"
        lines += [
            "upload the file (one-time link, valid 15 min, up to "
            f"{store.MAX_BYTES // (1024 * 1024)} MB; it is kept 24 h, for you only):",
            f'  curl -sS -F "file=@<path to .erf/.epf/Form.bin>" {url}',
            f'the answer names a handle; then call {TOOL}(file="<handle>", '
            'repo="<configuration repository>")',
        ]
    if mine:
        lines.append("your uploads: " + ", ".join(f"{s.handle} {s.file_name}" for s in mine[:10]))
    return "\n".join(lines)


def run(file: str = "", repo: str = "", object: str = "") -> str:  # noqa: A002
    scope = common.begin()
    if not (file or "").strip():
        return emit.render_text(TOOL, [], _upload_link(scope).split("\n"))
    saved, ext, problem = _load(scope, _handle(file))
    if problem:
        return problem
    entries = [_entry(saved)]
    hint = (f"read its code: outline(repo=\"{PREFIX}{saved.handle}\", path=\"\"), "
            f"read_symbol(repo=\"{PREFIX}{saved.handle}\", name=…), "
            f"grep(repo=\"{PREFIX}{saved.handle}\", pattern=…)")
    comparison = checks = None
    no_config = ""
    if (repo or "").strip():
        try:
            slug = scope.resolve(repo, need="code")
        except RepoNotAccessible:
            return common.repo_error(scope, repo)
        fresh = common.fresh_entries([slug])
        if not fresh:
            return emit.error(entries, f"{slug}: no indexed revision recorded yet")
        entries.append(fresh[0])
        source = _source(scope, slug, fresh[0].sha)
        cfg = Config(source, label=f"{slug}@{fresh[0].sha[:8]}") if source else None
        if cfg is None or not cfg.ok:
            no_config = (f"{slug} is not a 1C configuration dump (no Configuration.xml, "
                         "no EDT src/Configuration): nothing was compared")
            cfg = None
        else:
            comparison = compare(ext, cfg, object or "")
        checks = check(ext, cfg)
    else:
        no_config = ('pass repo="<configuration repository>" to compare the file with it '
                     "and check its calls, objects and queries")
    text = render(ext, comparison, checks, read_hint=hint, no_config=no_config)
    return emit.render_text(TOOL, entries, text.rstrip("\n").split("\n"))


# ─── the file read like a repository ─────────────────────────────────

def _tree(ext: ExternalFile) -> dict[str, list[str]]:
    return {path: text.replace("\r\n", "\n").split("\n") for path, text in ext.texts.items()}


def outline(repo: str, path: str, depth: int = 1, response_format: str = "concise") -> str:
    scope = common.begin()
    saved, ext, problem = _load(scope, _handle(repo))
    if problem:
        return problem
    entries = [_entry(saved)]
    rel = (path or "").strip().strip("/")
    tree = _tree(ext)
    slug = f"{PREFIX}{saved.handle}"
    if rel in tree:
        lines = [f"{EXTERNAL_MARK}", f"{slug} {rel} {len(tree[rel])} lines"]
        if rel.endswith(".bsl"):
            for p in procedures(ext.texts[rel]):
                kind = "function" if p.kind.lower() in ("функция", "function") else "procedure"
                lines.append(f"{p.start}-{p.end} {kind} {p.signature()}")
        return emit.render_text("outline", entries, lines)
    under = [p for p in sorted(tree) if not rel or p.startswith(rel + "/")]
    if not under:
        return emit.error(entries, f"{slug}: no file or directory {rel or '.'}",
                          f'outline(repo="{slug}", path="") lists every file')
    lines = [f"{EXTERNAL_MARK}", f"{slug} {rel or '.'} {len(under)} files"]
    for p in under:
        n = len(procedures(ext.texts[p])) if p.endswith(".bsl") else 0
        lines.append(f"{p} {len(tree[p])} lines" + (f", {n} procedures" if n else ""))
    return emit.render_text("outline", entries, lines)


def read_symbol(repo: str, name: str, path: str = "", max_lines: int = 60,
                cursor: str = "") -> str:
    scope = common.begin()
    saved, ext, problem = _load(scope, _handle(repo))
    if problem:
        return problem
    entries = [_entry(saved)]
    slug = f"{PREFIX}{saved.handle}"
    want = (name or "").strip().split(".")[-1].lower()
    rel = (path or "").strip().strip("/")
    found = []
    for p_path, text in ext.texts.items():
        if not p_path.endswith(".bsl") or (rel and p_path != rel):
            continue
        for p in procedures(text):
            if p.name.lower() == want:
                found.append((p_path, p, text))
    if not found:
        return emit.error(entries, f"{slug}: no procedure {name}"
                          + (f" in {rel}" if rel else ""),
                          f'outline(repo="{slug}", path="<module>") lists them')
    p_path, p, text = found[0]
    body = text.replace("\r\n", "\n").split("\n")[p.start - 1:p.end]
    off = 0
    if cursor.strip().isdigit():
        off = min(int(cursor), max(0, len(body) - 1))
    cap = max(1, min(int(max_lines or 60), 300))
    page = body[off:off + cap]
    lines = [EXTERNAL_MARK, f"{slug} {p_path}:{p.start}-{p.end} {p.signature()}"]
    if p.directives:
        lines.append(" ".join(p.directives))
    lines += page
    left = len(body) - off - len(page)
    if left > 0:
        lines.append(f"… +{left} more lines ({p.start + off + len(page)}-{p.end}) · "
                     f"cursor={off + len(page)}")
    if len(found) > 1:
        lines.append("also in: " + ", ".join(f"{a}:{b.start}" for a, b, _t in found[1:6]))
    return emit.render_text("read_symbol", entries, lines)


def grep(pattern: str, repo: str, path_glob: str = "", regex: bool = False,
         limit: int = 20, cursor: str = "") -> str:
    scope = common.begin()
    saved, ext, problem = _load(scope, _handle(repo))
    if problem:
        return problem
    entries = [_entry(saved)]
    slug = f"{PREFIX}{saved.handle}"
    pat = pattern or ""
    if not pat.strip():
        return emit.error(entries, "pattern is empty")
    flags = re.IGNORECASE if (pat == pat.lower() and not regex) else 0
    try:
        rx = re.compile(pat if regex else re.escape(pat), flags)
    except re.error as exc:
        return emit.error(entries, f"invalid regex: {exc}")
    hits = []
    for p_path, lines in sorted(_tree(ext).items()):
        if path_glob and not fnmatch.fnmatch(p_path, path_glob):
            continue
        for i, ln in enumerate(lines):
            if rx.search(ln):
                hits.append(f"{slug} {p_path}:{i + 1} {ln.strip()[:200]}")
                if len(hits) >= _GREP_HITS:
                    break
    off = int(cursor) if cursor.strip().isdigit() else 0
    size = max(1, min(int(limit or 20), 100))
    page = hits[off:off + size]
    out = [EXTERNAL_MARK, *page] if page else [EXTERNAL_MARK, "no matches"]
    if off + size < len(hits):
        out.append(f"… +{len(hits) - off - size} more · cursor={off + size}")
    return emit.render_text("grep", entries, out)
