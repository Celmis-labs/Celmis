"""`find`: ranked symbol search across every repository the caller can read."""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.mcp_server.dev_profile import common, cursor, emit, git_io, rank
from src.mcp_server.dev_profile.signature import MAX_CHARS, derive_signature

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
CANDIDATES_PER_REPO = 200
POOL = 400
MODES = ("auto", "exact", "prefix", "fuzzy")
MORE_HINT = "repo= / kind= / mode=exact"


@dataclass(frozen=True)
class Hit:
    slug: str
    row: dict
    score: float

    @property
    def key(self) -> tuple:
        r = self.row
        return (self.slug, r.get("file"), r.get("start_line"), r.get("name"))


def search(scope, slugs: list[str], query: str, *, kind: str, mode: str,  # noqa: ANN001
           ) -> list[Hit]:
    """Scored, deduplicated, best-first hits over `slugs` (at most POOL)."""

    def one(slug: str) -> list[Hit]:
        with common.open_store(slug) as store:
            rows = store.find_symbols(
                query, mode=mode, kind=kind or None, limit=CANDIDATES_PER_REPO)
        out = []
        for row in rows:
            if not scope.listable(slug, str(row.get("file") or "")):
                continue
            s = rank.score(row, query)
            if s > 0:
                out.append(Hit(slug, row, s))
        return out

    merged: dict[tuple, Hit] = {}
    for hits in common.map_repos(slugs, one).values():
        for h in hits:
            merged.setdefault(h.key, h)
    ordered = sorted(
        merged.values(),
        key=lambda h: (-h.score, h.slug, str(h.row.get("file")), h.row.get("start_line") or 0))
    return ordered[:POOL]


def diversify(hits: list[Hit], page_size: int) -> list[Hit]:
    """Reorder so no repository takes more than half of any page.

    Deterministic, so a cursor offset into the result stays meaningful. One
    repository with a thousand `user` variables must not push every other
    repository's answer off the first page.
    """
    if len({h.slug for h in hits}) < 2:
        return hits
    cap = max(1, math.ceil(page_size / 2))
    remaining, out = list(hits), []
    while remaining:
        counts: dict[str, int] = {}
        page, rest = [], []
        for h in remaining:
            if len(page) < page_size and counts.get(h.slug, 0) < cap:
                page.append(h)
                counts[h.slug] = counts.get(h.slug, 0) + 1
            else:
                rest.append(h)
        if len(page) < page_size and rest:  # one repo left: relax the cap
            take = rest[:page_size - len(page)]
            page += take
            rest = rest[len(take):]
        out += page
        remaining = rest
    return out


def line_for(scope, hit: Hit, sha: str, cache: dict, *, detailed: bool) -> str:  # noqa: ANN001
    r = hit.row
    start = int(r.get("start_line") or 0)
    end = r.get("end_line")
    span = f"{start}-{end}" if end and int(end) > start else f"{start}"
    head = f"{hit.slug} {r.get('file')}:{span} {r.get('kind')}"
    if not scope.readable(hit.slug, str(r.get("file") or "")):
        text = f"{head} {r.get('name')}"  # names and paths only
    else:
        sig = emit.clip(r.get("signature") or "", MAX_CHARS)
        if not sig:
            key = (hit.slug, r.get("file"))
            if key not in cache:
                cache[key] = git_io.show_file(hit.slug, sha, str(r.get("file")))
            lines = cache[key]
            sig = emit.clip((derive_signature(lines, start) if lines else None)
                            or str(r.get("name")), MAX_CHARS)
        text = f"{head} {sig}"
    degree = int(r.get("in_degree") or 0)
    if degree:
        text += f"  ← {degree} caller{'s' if degree != 1 else ''}"
    if detailed and r.get("docstring") and scope.readable(hit.slug, str(r.get("file"))):
        doc = emit.clip(r["docstring"], 160)
        text += f"\n  doc: {doc}"
    return text


def run(query: str, repo: str = "", kind: str = "", mode: str = "auto",
        limit: int = DEFAULT_LIMIT, cursor_: str = "",
        response_format: str = "concise") -> str:
    scope = common.begin()
    q = (query or "").strip()
    if not q:
        return emit.error(None, "query is empty",
                          "pass a symbol name, e.g. find(query=\"OrderService\")")
    if mode not in MODES:
        return emit.error(None, f"mode must be one of {', '.join(MODES)}")
    slugs, err = common.pick_repos(scope, repo)
    if err:
        return err
    detailed = common.norm_format(response_format)
    limit = common.clamp(limit, 1, MAX_LIMIT)
    fresh = {e.slug: e for e in common.fresh_entries(slugs)}
    h = cursor.fingerprint(tool="find", q=q, repo=repo, kind=kind, mode=mode,
                           limit=limit, detailed=detailed)

    hits = diversify(search(scope, slugs, q, kind=kind, mode=mode), limit)
    # Pinned to the repositories the ranked list is made of: a push to an
    # unrelated repository must not reset the reader's page 2.
    pin = {s: fresh[s].sha for s in dict.fromkeys(x.slug for x in hits) if s in fresh}
    off, note = cursor.resolve(cursor_, h, pin)
    page = hits[off:off + limit]
    cache: dict = {}
    lines = [line_for(scope, hit, fresh[hit.slug].sha if hit.slug in fresh else "",
                      cache, detailed=detailed) for hit in page]
    shown = list(dict.fromkeys(hit.slug for hit in page)) or slugs
    entries = [fresh[s] for s in shown if s in fresh]
    empty = f'no symbol matching "{q}"' + (f" of kind {kind}" if kind else "")
    return emit.render_list(
        "find", entries, lines, total=len(hits), offset=off, query_hash=h, note=note,
        empty=empty + "; try mode=fuzzy, a shorter name, or grep for a literal",
        more_hint=MORE_HINT, detailed=detailed, raised=limit > DEFAULT_LIMIT, pin=pin,
    )
