"""`outline`, `read_symbol` and `map`: reading a repository without reading files.

All three answer from the graph and from `git show <indexed_sha>:<path>`, so
the line numbers they print are the line numbers of the revision on the idx
line, whatever the local checkout looks like.
"""

from __future__ import annotations

from collections import defaultdict

from src.mcp_server.dev_profile import common, emit, git_io
from src.mcp_server.dev_profile.access import RepoNotAccessible
from src.mcp_server.dev_profile.paths import is_unsafe_rel_path
from src.mcp_server.dev_profile.signature import MAX_CHARS, derive_signature

_GOOD_KINDS = ("class", "function", "method", "interface", "struct", "enum", "type")
_TAIL_LINES = 10
MAX_BODY_LINES = 300


def _clean_path(path: str) -> str | None:
    """Repo-relative path without `./` or trailing `/`; None when unsafe."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    if p and is_unsafe_rel_path(p):
        return None
    return p


def _one_repo(scope, repo: str, *, need: str):  # noqa: ANN001, ANN202
    """(slug, fresh entry) or (None, error text)."""
    try:
        slug = scope.resolve(repo, need=need)
    except RepoNotAccessible:
        return None, None, common.repo_error(scope, repo)
    entries = common.fresh_entries([slug])
    if not entries:
        return None, None, emit.error([], f"{slug}: no indexed revision recorded yet",
                                      "ask an admin to re-index it")
    return slug, entries[0], None


# ─── outline ─────────────────────────────────────────────────────────

def _nest(rows: list) -> list[tuple[int, object]]:
    """(depth, symbol) with depth from containment of line ranges."""
    out, stack = [], []
    for s in rows:
        end = s.end_line or s.start_line
        while stack and s.start_line > stack[-1]:
            stack.pop()
        out.append((len(stack), s))
        if s.kind in ("class", "interface", "struct", "enum", "type", "namespace", "module"):
            stack.append(end)
    return out


def outline(repo: str, path: str, depth: int = 1, response_format: str = "concise") -> str:
    scope = common.begin()
    slug, fresh, err = _one_repo(scope, repo, need="metadata")
    if err:
        return err
    rel = _clean_path(path)
    if rel is None:
        return emit.error([fresh], "path must be repo-relative, without ..")
    detailed = common.norm_format(response_format)
    depth = common.clamp(depth, 1, 4)
    if rel and not scope.listable(slug, rel):
        return emit.error([fresh], f"nothing indexed at {rel}",
                          "use find or map to locate it")
    with common.open_store(slug) as store:
        symbols = store.symbols_in_file(rel) if rel else []
        if symbols:
            return _file_outline(scope, slug, fresh, rel, symbols, detailed)
        return _dir_outline(scope, store, slug, fresh, rel, depth, detailed)


def _file_outline(scope, slug, fresh, rel, symbols, detailed) -> str:  # noqa: ANN001
    lines_src = None
    if scope.readable(slug, rel) and detailed:
        lines_src = git_io.show_file(slug, fresh.sha, rel)
    lines = [f"{slug} {rel} {len(symbols)} symbols"]
    for depth, s in _nest(symbols):
        end = s.end_line
        span = f"{s.start_line}-{end}" if end and end > s.start_line else f"{s.start_line}"
        if not scope.readable(slug, rel):
            text = ""  # metadata-only access: kind and name, never code text
        else:
            text = emit.clip(s.signature or "", MAX_CHARS)
            if not text and lines_src:
                text = emit.clip(derive_signature(lines_src, s.start_line) or "", MAX_CHARS)
        shown = text if text and s.name in text else (f"{s.name} {text}".strip() if text else s.name)
        lines.append(f"{'  ' * depth}{span} {s.kind} {shown}")
    return emit.render_text("outline", [fresh], lines, detailed=detailed)


def _tree(store, prefix: str) -> dict[str, int]:
    rows = store.query(
        "MATCH (s:Symbol) WHERE s.kind <> 'file_module' AND s.file STARTS WITH $p "
        "RETURN s.file AS file, count(s) AS n",
        params={"p": prefix + "/" if prefix else ""},
    )
    return {str(r["file"]): int(r["n"]) for r in rows if r.get("file")}


def _top_names(store, prefix: str, order: str, limit: int) -> dict[str, list[str]]:
    """file -> its top-level class/function names (source order, or by use)."""
    p = {"p": prefix + "/" if prefix else ""}
    if order == "used":
        rows = store.query(
            "MATCH (s:Symbol)<-[r:CALLS|IMPORTS]-() "
            "WHERE s.kind IN $kinds AND s.file STARTS WITH $p "
            "RETURN s.file AS file, s.name AS name, count(r) AS d "
            "ORDER BY d DESC LIMIT $limit",
            params={**p, "kinds": list(_GOOD_KINDS), "limit": limit})
    else:
        rows = store.query(
            "MATCH (s:Symbol) WHERE s.kind IN ['class','function','interface'] "
            "AND s.file STARTS WITH $p "
            "RETURN s.file AS file, s.name AS name "
            "ORDER BY s.file, s.start_line LIMIT $limit",
            params={**p, "limit": limit})
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r.get("file") and r.get("name"):
            out[str(r["file"])].append(str(r["name"]))
    return out


def _group(files: dict[str, int], prefix: str, depth: int):
    """Fold files into the entries at `depth` levels below `prefix`: files
    directly in `prefix` stay as leaves, everything deeper is counted into its
    directory (cut at `depth`)."""
    base = prefix.split("/") if prefix else []
    dirs: dict[str, list[int]] = {}
    leaves: dict[str, int] = {}
    for f, n in files.items():
        below = f.split("/")[len(base):]
        if len(below) <= 1:
            leaves[f] = n
            continue
        d = "/".join(base + below[:-1][:depth])
        agg = dirs.setdefault(d, [0, 0])
        agg[0] += 1
        agg[1] += n
    return dirs, leaves


def _dir_outline(scope, store, slug, fresh, rel, depth, detailed) -> str:  # noqa: ANN001
    files = {f: n for f, n in _tree(store, rel).items() if scope.listable(slug, f)}
    if not files:
        return emit.error([fresh], f"nothing indexed under {rel or '.'}",
                          "use find, or map for the repository layout")
    names = _top_names(store, rel, "source", 800)
    dirs, leaves = _group(files, rel, depth)
    lines = [f"{slug} {rel or '.'}/ {len(files)} files, {sum(files.values())} symbols"]
    for d, (nf, ns) in sorted(dirs.items()):
        lines.append(f"{d}/  {nf} files, {ns} symbols")
    for f, n in sorted(leaves.items()):
        top = ", ".join(names.get(f, [])[: (8 if detailed else 4)])
        lines.append(f"{f}  {n} symbols" + (f": {top}" if top else ""))
    return emit.render_text("outline", [fresh], lines, detailed=detailed)


# ─── read_symbol ─────────────────────────────────────────────────────

def _pick(rows: list[dict], qualifier: str) -> list[dict]:
    def key(r: dict):  # noqa: ANN202
        q = 1 if qualifier and qualifier.lower() in str(r.get("id") or "").lower() else 0
        return (-q, 0 if r.get("kind") in _GOOD_KINDS else 1,
                -(int(r.get("in_degree") or 0)), str(r.get("file")), r.get("start_line") or 0)
    return sorted(rows, key=key)


def _elide(body: list[str], start: int, max_lines: int, rel: str, repo_arg: str):  # noqa: ANN202
    """(lines, was_cut): the whole body, or head + marker + tail."""
    if len(body) <= max_lines:
        return body, False
    tail = _TAIL_LINES if max_lines >= 20 else max(1, max_lines // 4)
    head = max_lines - tail
    cut = len(body) - head - tail
    a, b = start + head, start + len(body) - tail - 1
    marker = (f"… {cut} lines elided (read_symbol max_lines={min(len(body), MAX_BODY_LINES)}"
              f" or Read {rel}:{a}-{b}) …")
    return body[:head] + [marker] + body[len(body) - tail:], True


def read_symbol(repo: str, name: str, path: str = "", max_lines: int = 60,
                include: str = "callers,callees", response_format: str = "concise") -> str:
    scope = common.begin()
    slug, fresh, err = _one_repo(scope, repo, need="code")
    if err:
        return err
    sym = (name or "").strip()
    if not sym:
        return emit.error([fresh], "name is empty")
    rel = _clean_path(path)
    if rel is None:
        return emit.error([fresh], "path must be repo-relative, without ..")
    detailed = common.norm_format(response_format)
    max_lines = common.clamp(max_lines, 5, MAX_BODY_LINES)
    last = sym.replace("::", ".").split(".")[-1]
    qualifier = sym.replace("::", ".").rsplit(".", 1)[0] if "." in sym.replace("::", ".") else ""

    neighbours: list[str] = []
    with common.open_store(slug) as store:
        rows = store.find_symbols(last, mode="exact", limit=60)
        rows = [r for r in rows if scope.readable(slug, str(r.get("file") or ""))
                and (not rel or str(r.get("file")) == rel or str(r.get("file")).endswith(rel))]
        near: list[dict] = []
        if rows:
            rows = _pick(rows, qualifier)
            neighbours = _neighbours(scope, store, slug, rows[0], include)
        else:
            near = store.find_symbols(last, mode="auto", limit=60)
    if not rows:
        from src.mcp_server.dev_profile import rank
        scored = sorted(((rank.score(r, last), r) for r in near
                         if scope.listable(slug, str(r.get("file") or ""))),
                        key=lambda t: -t[0])
        names = list(dict.fromkeys(str(r["name"]) for s, r in scored if s > 0))[:5]
        return emit.error([fresh], f'no symbol "{sym}" in {slug}'
                          + (f" at {rel}" if rel else ""),
                          f"similar: {', '.join(names)}" if names else "try find",
                          )
    best, *others = rows
    file = str(best["file"])
    start = int(best.get("start_line") or 1)
    end = int(best.get("end_line") or 0) or (start + max_lines - 1)
    src = git_io.show_file(slug, fresh.sha, file)
    if src is None:
        return emit.error([fresh], f"{file} is not readable at {fresh.sha[:8]}",
                          "the clone no longer has that revision; ask an admin to re-index")
    # Redact the WHOLE span before slicing: a multi-line secret is only
    # recognisable whole, and elision must not leave half of one behind.
    try:
        span = emit.redact_text("\n".join(src[start - 1:end])).split("\n")
    except Exception:  # noqa: BLE001 — fail closed
        return emit.withheld([fresh])
    body, _cut = _elide(span, start, max_lines, file, repo)

    sig = emit.clip(best.get("signature") or "", MAX_CHARS) \
        or emit.clip(derive_signature(src, start) or str(best["name"]), MAX_CHARS)
    out = [f"{slug} {file}:{start}-{end} {best['kind']} {best['name']}", f"sig: {sig}"]
    if best.get("docstring"):
        out.append("doc: " + emit.clip(best["docstring"], 600 if detailed else 160))
    if others:
        also = [f"{o['file']}:{o.get('start_line')}" for o in others[:4]]
        out.append("also in: " + ", ".join(also))
    out.append("---")
    out.extend(body)
    out.append("---")
    out.extend(neighbours)
    return emit.render_text(
        "read_symbol", [fresh], out, detailed=detailed,
        budget_tokens=emit.BUDGETS["read_symbol"][1] if max_lines > 60 else None)


def _neighbours(scope, store, slug: str, row: dict, include: str) -> list[str]:  # noqa: ANN001
    want = {p.strip() for p in (include or "").split(",") if p.strip()}
    out: list[str] = []
    for kind in ("callers", "callees"):
        if kind not in want:
            continue
        try:
            found = common.expand(store, str(row["id"]), kind, 1, 30)
        except Exception:  # noqa: BLE001
            continue
        items = [c for c in found if scope.listable(slug, str(c.get("file") or ""))]
        if not items:
            continue
        top = ", ".join(f"{c.get('name')} {c.get('file')}:{c.get('start_line')}"
                        for c in items[:5])
        more = f" (+{len(items) - 5} more; use refs)" if len(items) > 5 else ""
        out.append(f"{kind}({len(items)}): {top}{more}")
    return out


# ─── map ─────────────────────────────────────────────────────────────

def repo_map(repo: str, path: str = "", budget_tokens: int = 800) -> str:
    scope = common.begin()
    slug, fresh, err = _one_repo(scope, repo, need="metadata")
    if err:
        return err
    rel = _clean_path(path)
    if rel is None:
        return emit.error([fresh], "path must be repo-relative, without ..")
    budget = common.clamp(budget_tokens, 100, emit.BUDGETS["map"][1])
    with common.open_store(slug) as store:
        files = {f: n for f, n in _tree(store, rel).items() if scope.listable(slug, f)}
        if not files:
            return emit.error([fresh], f"nothing indexed under {rel or '.'}", "try map without path")
        used = _top_names(store, rel, "used", 400)
    for depth in (1, 2, 3):
        dirs, leaves = _group(files, rel, depth)
        if len(dirs) + len(leaves) >= 8:
            break
    lines = [f"{slug} {rel or '.'}/ {len(files)} files, {sum(files.values())} symbols"]
    rows = []
    for d, (nf, ns) in dirs.items():
        top = []
        for f, ns_ in used.items():
            if f.startswith(d + "/"):
                top.extend(ns_)
        rows.append((ns, f"{d}/  {nf}f {ns}s" + (f"  top: {', '.join(list(dict.fromkeys(top))[:4])}" if top else "")))
    for f, n in leaves.items():
        top = used.get(f, [])
        rows.append((n, f"{f}  {n}s" + (f"  top: {', '.join(top[:4])}" if top else "")))
    lines.extend(t for _n, t in sorted(rows, key=lambda r: -r[0]))
    return emit.render_text("map", [fresh], lines, budget_tokens=budget)
