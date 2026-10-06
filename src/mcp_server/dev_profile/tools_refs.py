"""`refs`: who calls (or imports) a symbol, across the repositories of a group."""

from __future__ import annotations

import logging

from src.mcp_server.dev_profile import common, cursor, emit, git_io, rank
from src.mcp_server.dev_profile.access import RepoNotAccessible

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 60
DIRECTIONS = ("callers", "callees", "imports")
MAX_SIBLINGS = 5
MORE_HINT = "cross_repo=false / depth=1 / limit="


def _symbol_ids(store, symbol: str) -> list[str]:
    last = symbol.replace("::", ".").split(".")[-1]
    return [str(r["id"]) for r in store.find_symbols(last, mode="exact", limit=40)]


def _imports(store, ids: list[str], limit: int) -> list[dict]:
    if not ids:
        return []
    return store.query(
        "MATCH (a:Symbol)-[:IMPORTS]->(b:Symbol) WHERE b.id IN $ids "
        "RETURN DISTINCT a.id AS id, a.name AS name, a.kind AS kind, a.file AS file, "
        "a.start_line AS start_line, 1 AS hops ORDER BY a.file LIMIT $limit",
        params={"ids": ids, "limit": limit})


def _siblings(scope, slug: str) -> list[str]:
    """Other repositories (readable ones) that share a group with `slug`."""
    try:
        from src.groups import get_group_manager
        from src.mcp_server import tenancy
        from src.sync.git_providers import parse_repo_url

        ws = scope.workspace_id if tenancy.enforced() else None
        groups = get_group_manager().groups_containing(slug, ws)
        out: list[str] = []
        readable = set(scope.slugs(need="code"))
        for g in groups:
            for r in g.repos:
                try:
                    s = parse_repo_url(r).slug
                except Exception:  # noqa: BLE001
                    s = r
                if s != slug and s in readable and s not in out:
                    out.append(s)
        return out[:MAX_SIBLINGS]
    except Exception as exc:  # noqa: BLE001
        logger.debug("dev_refs_siblings_failed repo=%s err=%s", slug, exc)
        return []


def run(repo: str, symbol: str, direction: str = "callers", depth: int = 1,
        cross_repo: bool = True, limit: int = DEFAULT_LIMIT, cursor_: str = "",
        response_format: str = "concise") -> str:
    scope = common.begin()
    try:
        slug = scope.resolve(repo, need="metadata")
    except RepoNotAccessible:
        return common.repo_error(scope, repo)
    fresh = common.fresh_entries([slug])
    sym = (symbol or "").strip()
    if not sym:
        return emit.error(fresh, "symbol is empty")
    if direction not in DIRECTIONS:
        return emit.error(fresh, f"direction must be one of {', '.join(DIRECTIONS)}")
    depth = common.clamp(depth, 1, 3)
    limit = common.clamp(limit, 1, MAX_LIMIT)
    detailed = common.norm_format(response_format)
    pin = {e.slug: e.sha for e in fresh}

    with common.open_store(slug) as store:
        ids = _symbol_ids(store, sym)
        if not ids:
            near = [r for r in store.find_symbols(sym, mode="auto", limit=60)
                    if scope.listable(slug, str(r.get("file") or ""))]
            names = list(dict.fromkeys(
                str(r["name"]) for r in sorted(near, key=lambda r: -rank.score(r, sym))
                if rank.score(r, sym) > 0))[:5]
            return emit.error(fresh, f'no symbol "{sym}" in {slug}',
                              f"similar: {', '.join(names)}" if names else "try find")
        if direction == "imports":
            rows = _imports(store, ids, 100)
        else:
            rows = common.expand(store, sym, direction, depth, 100)
    rows = [r for r in rows if scope.listable(slug, str(r.get("file") or ""))]
    rows.sort(key=lambda r: (int(r.get("hops") or 1), str(r.get("file")),
                             int(r.get("start_line") or 0)))
    items = []
    for r in rows:
        line = f"{slug} {r.get('file')}:{r.get('start_line')} {r.get('kind')} {r.get('name')}"
        if int(r.get("hops") or 1) > 1:
            line += f"  ({r['hops']} hops)"
        items.append(line)

    head = [f"{direction} of {sym}: {len(rows)} in {slug}"]
    entries = list(fresh)
    if cross_repo and direction == "callers":
        sibs = _siblings(scope, slug)
        sib_fresh = {e.slug: e for e in common.fresh_entries(sibs)}
        counts = []
        for sib in sibs:
            if sib not in sib_fresh:
                continue
            scope.get(sib, need="code")
            if not git_io.has_commit(sib, sib_fresh[sib].sha):
                counts.append(f"{sib} revision {sib_fresh[sib].sha[:8]} not in clone")
                continue
            hits = [h for h in git_io.grep(sib, sib_fresh[sib].sha, sym, word=True,
                                           first_only=True, max_hits=20, timeout=3.0)
                    if scope.readable(sib, h.path)]
            if hits:
                counts.append(f"{sib} {len(hits)} files")
                entries.append(sib_fresh[sib])
                pin[sib] = sib_fresh[sib].sha
                items.extend(f"{sib} {h.path}:{h.line} text-ref  {emit.clip(h.text, 100)}"
                             for h in hits[:3])
        if counts:
            head.append("text refs in sibling repos (heuristic): " + ", ".join(counts))

    h = cursor.fingerprint(tool="refs", repo=slug, symbol=sym, direction=direction,
                           depth=depth, cross=cross_repo, limit=limit)
    off, note = cursor.resolve(cursor_, h, pin)
    return emit.render_list(
        "refs", entries, items[off:off + limit], total=len(items), offset=off,
        query_hash=h, head=head, note=note,
        empty=f"no {direction} found (static analysis can miss dynamic calls; try grep)",
        more_hint=MORE_HINT, detailed=detailed, raised=limit > DEFAULT_LIMIT, pin=pin)
