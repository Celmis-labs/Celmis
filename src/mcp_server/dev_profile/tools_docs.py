"""`docs`: the documentation of the caller's repositories, generated and imported.

Four modes, picked by the arguments:

  * ``path=``            — open one note (by its path or a card's id): its front
    matter, the 1C objects a card names (and whether the graph has them),
    staleness, links and back-links, then the text;
  * ``changed_since=``    — the notes that changed (written, rewritten, removed,
    marked stale), newest first;
  * ``query=``            — search by meaning, narrowed by the filters;
  * none of these         — list the notes the filters select
    (``where="object=Документ.X"`` alone answers "which cards touch this 1C
    object").

The filters are one argument, ``where`` — ``key=value`` pairs: ``type``,
``status`` (draft | verified | needs-review), ``object`` (a 1C object in any
spelling: ``Документ.X``, ``Document.X``, ``Documents.X``), ``id``, ``tag`` and
any front matter key of the team's own (``domain=sales author=ivan lang=uk``).
One argument rather than one per key keeps the tool listing every client pays
for small. Each line says a card's status, so a draft is never read as a
checked fact; a stale card reads ``needs-review``.
"""

from __future__ import annotations

import logging
from typing import Any

from src.kb import catalog, changes, links
from src.kb.objects import canonical
from src.mcp_server.dev_profile import common, cursor, emit
from src.mcp_server.dev_profile.access import RepoNotAccessible

logger = logging.getLogger(__name__)

TOOL = "docs"
DESCRIPTION = ("Generated docs and imported cards: search (query=), list or filter by front matter "
               "(where='type=process status=verified object=Документ.X'), open (path= or id), "
               "changed_since=7d.")
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: Characters of a note's text one open shows (the rest: open it on the web).
MAX_BODY_CHARS = 9000
#: How much a hit's status moves it: a checked card first, a draft last, at
#: equal relevance. A generated note sits between the two.
_STATUS_WEIGHT = {"verified": 1.0, "": 0.97, "needs-review": 0.9, "draft": 0.88}


def _filters(where: str) -> dict[str, str]:
    f = catalog.parse_where(where)
    if f.get("status"):
        from src.kb.cards import normalize_status

        f["status"] = normalize_status(f["status"])
    return f


def _line(slug: str, n: catalog.NoteInfo, score: float | None = None) -> str:
    parts = [f"{slug} {n.path}", n.type or "note"]
    if n.card:
        parts.append(n.status or "draft")
        if n.kb.get("id"):
            parts.append(f"id={n.kb['id']}")
    parts.append(n.title)
    if score is not None:
        parts.append(f"{score:.2f}")
    return " · ".join(parts)


def _visible(scope: Any, slug: str, n: catalog.NoteInfo) -> bool:  # noqa: ANN401
    dec = scope.decisions.get(slug)
    return dec is not None and not n.denied_by(dec)


def _slugs(scope: Any, repo: str) -> list[str]:  # noqa: ANN401
    if repo:
        return [scope.resolve(repo, need="metadata")]
    return scope.slugs(need="metadata", shared_first=True)[: common.MAX_REPOS_PER_CALL]


def _catalog(slug: str) -> list[tuple[catalog.NoteInfo, str]]:
    from src.kb.store import vault_dir
    from src.vault.reader import vault_note_files

    directory = vault_dir(slug)
    out = []
    for full in vault_note_files(directory)[: catalog.MAX_NOTES]:
        got = catalog.load(directory, full)
        if got is not None:
            out.append(got)
    return out


# ─── open one ─────────────────────────────────────────────────────────


def _open(scope: Any, slugs: list[str], ref: str) -> tuple[str, catalog.NoteInfo, str,  # noqa: ANN401
                                                         list] | None:
    """The note at path ``ref``, else the card with id ``ref``: a path wins."""
    want_path = ref.strip().lstrip("/")
    want_id = ref.strip().lower()
    for slug in slugs:
        pairs = _catalog(slug)
        visible = [(i, b) for i, b in pairs if _visible(scope, slug, i)]
        for match in (lambda i: i.path in (want_path, f"kb/{want_path}"),
                      lambda i: str(i.kb.get("id", "")).lower() == want_id):
            for info, body in visible:
                if match(info):
                    return slug, info, body, visible
    return None


def _foreign_for(scope: Any):  # noqa: ANN401, ANN202
    """Notes of another repository the caller may read (None: not theirs to read)."""
    cache: dict[str, list | None] = {}

    def foreign(repo: str) -> list | None:
        if repo not in cache:
            cache[repo] = ([i for i, _ in _catalog(repo) if _visible(scope, repo, i)]
                           if scope.decisions.get(repo) is not None else None)
        return cache[repo]

    return foreign


def _neighbours(scope: Any, slug: str, path: str, pairs: list) -> dict:  # noqa: ANN401
    """Links out of and back into a note, across the repositories the caller may read."""
    from src.kb import store

    bound = {}
    for other in store.bindings_to(slug):
        if other != slug and scope.decisions.get(other) is not None:
            bound[other] = [(i, b) for i, b in _catalog(other) if _visible(scope, other, i)]
    return links.neighbours(path, pairs, repo=slug,
                            code_repo=str((store.load_binding(slug) or {}).get("code_repo") or ""),
                            foreign=_foreign_for(scope), bound=bound)


def _render_open(slug: str, n: catalog.NoteInfo, body: str, pairs: list,
                 scope: Any = None) -> list[str]:  # noqa: ANN401
    kb = n.kb
    lines = [_line(slug, n)]
    meta = [f"{k}={kb[k]}" for k in ("domain", "author", "lang", "source") if kb.get(k)]
    if kb.get("tags"):
        meta.append("tags=" + ",".join(kb["tags"]))
    if meta:
        lines.append(" ".join(meta))
    own = {k: v for k, v in n.fm.items() if k not in (
        "type", "id", "title", "status", "domain", "author", "lang", "source", "tags",
        "objects_1c", "objects", "related", "replaces")}
    if own:
        lines.append("front matter: " + "; ".join(f"{k}={v}" for k, v in list(own.items())[:30]))
    if kb.get("objects"):
        missing = set(kb.get("objects_missing") or [])
        checked = "objects_missing" in kb
        objs = [o + ("" if not checked else (" (not in graph)" if o in missing else " ✓"))
                for o in kb["objects"]]
        lines.append("1C objects: " + " · ".join(objs))
        code = kb.get("code_repo") or slug
        lines.append(f"code: refs repo={code} symbol={kb['objects'][0]} · "
                     f"find repo={code} query={kb['objects'][0].split('.', 1)[-1]}")
    if kb.get("not_objects"):
        lines.append("not 1C names: " + ", ".join(kb["not_objects"]))
    st = kb.get("staleness") or {}
    if st.get("state") == "stale":
        lines.append("staleness: STALE — changed since the source commit: " + ", ".join(st.get("changed", [])))
    elif st.get("state") == "unknown" and kb.get("objects"):
        lines.append(f"staleness: unknown — {st.get('reason', '')}")
    elif st.get("state") == "fresh":
        lines.append("staleness: fresh — none of its objects changed since the source commit")
    nb = _neighbours(scope, slug, n.path, pairs) if scope is not None else links.neighbours(n.path, pairs)

    def _where(x: dict) -> str:
        return f"{x['repo']}:{x['path']}" if x.get("repo") else x["path"]

    if n.generated:
        lines.append("generated by Celmis from the code — a draft, not checked by a person")
    if nb["out"]:
        lines.append("links: " + " · ".join(f"{_where(x)} ({x['kind']})" for x in nb["out"][:20]))
    if nb["back"]:
        lines.append("linked from: " + " · ".join(f"{_where(x)} ({x['kind']})" for x in nb["back"][:20]))
    if nb["dangling"]:
        lines.append("links to nothing: " + ", ".join(nb["dangling"][:10]))
    lines.append("---")
    text = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "\n… (text cut; open it on the web)"
    lines.extend(text.split("\n"))
    return lines


# ─── search ───────────────────────────────────────────────────────────


def _search(scope: Any, slugs: list[str], query: str, filters: dict[str, str],  # noqa: ANN401
            limit: int) -> tuple[list[tuple[float, str, catalog.NoteInfo, str]], str]:
    """(scored hits, note). Falls back to words in the text when the vector
    index cannot answer (no embedding key, nothing indexed yet)."""
    from src.api.auto_review import workspace_for_repo_slug
    from src.retrieval.tier1_vault import CollectionMissing, VaultRetriever

    by_ws: dict[str | None, list[str]] = {}
    for s in slugs:
        by_ws.setdefault(workspace_for_repo_slug(s), []).append(s)
    note_type = filters.get("type", "")
    kb_filters = {k: v for k, v in filters.items() if k != "type"}
    hits: list[tuple[float, str, catalog.NoteInfo, str]] = []
    try:
        for ws, group in by_ws.items():
            if ws is None:
                continue
            rt = VaultRetriever(workspace_id=ws)
            found = rt.search(query, repos=group, note_type=note_type or None,
                              kb_filters=kb_filters, top_k=max(limit * 3, 20))
            for h in found:
                info = catalog.NoteInfo(path=h.note_path, title=h.title or h.module or h.note_path,
                                        type=h.type, card=bool(h.kb), kb=h.kb,
                                        status=catalog.effective_status(h.kb) if h.kb else "",
                                        source_path=h.path or "",
                                        source_paths=[str(f) for f in (h.kb or {}).get("files") or []]
                                        if (h.kb or {}).get("origin") == "generate" else [])
                if not _visible(scope, h.repo, info):
                    continue
                text = h.content.split("\n\n", 1)[-1]
                hits.append((h.score * _STATUS_WEIGHT.get(info.status, 0.9), h.repo, info, text))
    except CollectionMissing:
        return _lexical(scope, slugs, query, filters), "no search index yet; matched words in the text"
    except Exception as exc:  # noqa: BLE001
        logger.warning("dev_docs_vector_search_failed err=%s", type(exc).__name__)
        return _lexical(scope, slugs, query, filters), "search by meaning unavailable; matched words in the text"
    return _prefer_cards(hits), ""


def _replaced(hits: list) -> set[tuple[str, str]]:
    """(repository, path) of every note a VERIFIED card in ``hits`` replaces.

    A card's ``replaces`` names a note of its own vault, or — a card kept in a
    knowledge base of its own and bound to a code repository — one of the code
    repository's, as a plain path (its own vault first, then the code
    repository's) or as ``<repo>:<path>``. The key is the repository the note
    is in, so a card in one repository hides the generated note of another.
    """
    from src.kb.links import _REPO_REF
    from src.kb.store import vault_dir

    out: set[tuple[str, str]] = set()
    for _, slug, n, _ in hits:
        if not (n.card and n.status == "verified"):
            continue
        code = str(n.kb.get("code_repo") or "")
        for r in n.kb.get("replaces") or []:
            ref = str(r).strip()
            m = _REPO_REF.match(ref) if "://" not in ref else None
            repo, path = (m.group(1), m.group(2)) if m else ("", ref)
            if not path.endswith(".md"):
                path += ".md"
            if repo:
                out.add((repo, path))
                continue
            own = False
            if ".." not in path.split("/"):
                try:
                    own = (vault_dir(slug) / path.lstrip("/")).is_file()
                except OSError:
                    own = False
            out.add((slug if own or not code else code, path))
    return out


def _prefer_cards(hits: list) -> list:
    """Drop a generated note a VERIFIED card in the same answer replaces."""
    replaced = _replaced(hits)
    out = [h for h in hits if (h[1], h[2].path) not in replaced]
    out.sort(key=lambda h: -h[0])
    return out


def _lexical(scope: Any, slugs: list[str], query: str, filters: dict[str, str]) -> list:  # noqa: ANN401
    words = [w for w in query.lower().split() if len(w) > 1]
    out = []
    for slug in slugs:
        for info, body in _catalog(slug):
            if not _visible(scope, slug, info) or not catalog.matches(info, filters):
                continue
            hay = f"{info.title}\n{body}".lower()
            got = sum(1 for w in words if w in hay)
            if words and got:
                score = got / len(words) * _STATUS_WEIGHT.get(info.status, 0.9)
                out.append((score, slug, info, body))
    return _prefer_cards(out)


# ─── entry ────────────────────────────────────────────────────────────


def run(query: str = "", repo: str = "", where: str = "", path: str = "", changed_since: str = "",
        limit: int = DEFAULT_LIMIT, cursor_: str = "") -> str:
    scope = common.begin()
    try:
        slugs = _slugs(scope, repo)
    except RepoNotAccessible:
        hint = scope.similar(repo)
        return emit.error([], f"no repository {repo!r} you can read",
                          f"did you mean: {', '.join(hint)}" if hint else "call repos")
    scope.note_shared(slugs)
    entries = common.fresh_entries(slugs[:8])
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    filters = _filters(where)

    if path.strip():
        got = _open(scope, slugs, path)
        if got is None:
            return emit.error(entries, f"no note {path!r} in the documentation you can read",
                              "list with docs, or search with docs query=")
        slug, info, body, pairs = got
        return emit.render_text(TOOL, common.fresh_entries([slug]),
                                _render_open(slug, info, body, pairs, scope))

    if changed_since:
        when = changes.parse_since(changed_since)
        if when is None:
            return emit.error(entries, "changed_since: use 7d, 12h, 30m or an ISO date")
        from src.kb.store import vault_dir

        rows: list[tuple[str, str]] = []
        for slug in slugs:
            visible = {i.path for i, _ in _catalog(slug) if _visible(scope, slug, i)}
            for e in changes.read(vault_dir(slug), since=when, limit=limit * 4):
                p = str(e.get("path", ""))
                if p in visible or p.startswith("kb/"):
                    rows.append((str(e.get("at", "")), f"{slug} {p} · {e.get('change')} · {e.get('at')}"
                                 + (f" · by {e['by']}" if e.get("by") else "")))
        rows.sort(reverse=True)
        lines = [text for _, text in rows]
        return emit.render_list(TOOL, entries, lines[:limit], total=min(len(lines), limit),
                                empty="no notes changed since then")

    h = cursor.fingerprint(tool=TOOL, q=query, f=sorted(filters.items()), r=slugs)
    off, note = cursor.resolve(cursor_, h, {})
    if query.strip():
        hits, why = _search(scope, slugs, query.strip(), filters, limit)
        lines = [_line(slug, n, score) for score, slug, n, _ in hits]
        note = "; ".join(x for x in (note, why) if x)
    else:
        lines = []
        for slug in slugs:
            notes = [i for i, _ in _catalog(slug) if _visible(scope, slug, i) and catalog.matches(i, filters)]
            notes.sort(key=lambda i: (not i.card, -_STATUS_WEIGHT.get(i.status, 0.0), i.type,
                                      i.title.lower()))
            lines.extend(_line(slug, i) for i in notes)
    if filters.get("object") and canonical(filters["object"]) is None:
        note = "; ".join(x for x in (note, f"{filters['object']!r} is not Type.Name of a 1C object") if x)
    page = lines[off:off + limit]
    return emit.render_list(TOOL, entries, page, total=len(lines), offset=off, query_hash=h,
                            note=note, pin={},
                            empty="nothing matches; drop a filter, or list types with docs",
                            more_hint="narrow with where='type=… status=…'")
