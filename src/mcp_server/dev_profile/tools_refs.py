"""`refs`: who calls (or imports) a symbol, across the repositories of a group.

Beside the group, `refs` follows a repository another workspace shares with the
caller in BOTH directions (`shares.linked_repos`): the callers of a symbol of the
shared repository are looked for in the caller's own repositories, and the callers
of a symbol of an own repository in the shared ones. It is the same text scan as
for the group siblings, run in parallel, and it stays heuristic: there is no call
graph between repositories, and none between workspaces.
"""

from __future__ import annotations

import logging

from src.access.shares import MAX_LINKED, linked_repos
from src.mcp_server.dev_profile import common, cursor, emit, git_io, rank
from src.mcp_server.dev_profile.access import RepoNotAccessible

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 60
DIRECTIONS = ("callers", "callees", "imports")
MAX_SIBLINGS = 5
#: Most repositories one `refs` scans for text mentions, siblings and the other
#: side of a share together (`shares.MAX_LINKED`).
MAX_SCANNED = MAX_LINKED
MORE_HINT = "cross_repo=false / depth=1 / limit="


def _symbol_ids(scope, slug: str, store, symbol: str) -> list[str]:  # noqa: ANN001
    """The graph ids of the symbols called `symbol` in files the caller may list.

    A symbol that is defined only in a file that is hidden (a deny rule, the share's
    list, the secret floor) is no symbol here: otherwise `refs` answered "0 callers"
    for it and "no symbol" for a name that is nowhere, which tells the caller that
    the hidden file defines that name. `find` and `read_symbol` already answer so."""
    last = symbol.replace("::", ".").split(".")[-1]
    return [str(r["id"]) for r in store.find_symbols(last, mode="exact", limit=40)
            if scope.listable(slug, str(r.get("file") or ""))]


def _imports(store, ids: list[str], limit: int) -> list[dict]:
    if not ids:
        return []
    return store.query(
        "MATCH (a:Symbol)-[:IMPORTS]->(b:Symbol) WHERE b.id IN $ids "
        "RETURN DISTINCT a.id AS id, a.name AS name, a.kind AS kind, a.file AS file, "
        "a.start_line AS start_line, 1 AS hops ORDER BY a.file LIMIT $limit",
        params={"ids": ids, "limit": limit})


def _siblings(scope, slug: str) -> list[str]:
    """Every other repository (a readable one) that shares a group with `slug`.
    The caller looks in the first `MAX_SIBLINGS` and says how many it left out.

    Groups are objects of ONE workspace. A shared repository is in the groups of its
    owner, who has told nobody here about them, so it has no siblings for the caller;
    and a shared repository is never a sibling of an own one by group (it reaches
    `refs` through `linked_repos`, which is a decision about the share, not a group).
    In `single_tenant` the group lookup is not filtered by workspace at all, which is
    why this is checked here and not left to the lookup."""
    if scope.is_shared(slug):
        return []
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
                if s != slug and s in readable and s not in out and not scope.is_shared(s):
                    out.append(s)
        return out
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
        ids = _symbol_ids(scope, slug, store, sym)
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
        every_sibling = _siblings(scope, slug)
        linked = [r for r in linked_repos(scope, slug) if r not in every_sibling]
        scanned = every_sibling[:MAX_SIBLINGS]
        scanned += linked[:max(0, MAX_SCANNED - len(scanned))]
        sib_fresh = {e.slug: e for e in common.fresh_entries(scanned)}
        # `get` records the touch (a shared repository by its share id) and runs in
        # this thread: the call record is a context variable and the pool's threads
        # do not carry it.
        looked = [s for s in scanned if s in sib_fresh]
        for sib in looked:
            scope.get(sib, need="code")

        def scan(sib: str) -> list | None:
            if not git_io.has_commit(sib, sib_fresh[sib].sha):
                return None
            return [h for h in git_io.grep(sib, sib_fresh[sib].sha, sym, word=True,
                                           first_only=True, max_hits=20, timeout=3.0)
                    if scope.readable(sib, h.path)]

        # Up to MAX_SCANNED repositories at 3 s each in a loop would outlast the
        # call's time limit; the pool does them eight at a time.
        found = common.map_repos(looked, scan)
        counts = []
        for sib in looked:
            if sib not in found:
                continue
            hits = found[sib]
            if hits is None:
                counts.append(f"{sib} revision {sib_fresh[sib].sha[:8]} not in clone")
            elif hits:
                counts.append(f"{sib} {len(hits)} files")
                entries.append(sib_fresh[sib])
                pin[sib] = sib_fresh[sib].sha
                items.extend(f"{sib} {h.path}:{h.line} text-ref  {emit.clip(h.text, 100)}"
                             for h in hits[:3])
        if counts:
            head.append("text refs in sibling repos (heuristic): " + ", ".join(counts))
        every = len(every_sibling) + len(linked)
        if every > len(scanned):
            noun = "repos" if linked else "sibling repos"
            head.append(f"text refs looked for in {len(scanned)} of {every} "
                        f"{noun}; use grep with repo= for the rest")

    h = cursor.fingerprint(tool="refs", repo=slug, symbol=sym, direction=direction,
                           depth=depth, cross=cross_repo, limit=limit)
    off, note = cursor.resolve(cursor_, h, pin)
    return emit.render_list(
        "refs", entries, items[off:off + limit], total=len(items), offset=off,
        query_hash=h, head=head, note=note,
        empty=f"no {direction} found (static analysis can miss dynamic calls; try grep)",
        more_hint=MORE_HINT, detailed=detailed, raised=limit > DEFAULT_LIMIT, pin=pin)
