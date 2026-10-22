"""`repos`: what the caller can read, and how fresh each index is."""

from __future__ import annotations

from src.mcp_server.dev_profile import common, cursor, emit, freshness

#: One page is exactly what the idx line can describe: freshness per repo is
#: the point of this tool, so no listed repository may fall into "+N more".
DEFAULT_LIMIT = freshness.MAX_ENTRIES


def run(query: str = "", cursor_: str = "") -> str:
    scope = common.begin()
    q = "".join(ch for ch in (query or "").lower() if ch.isalnum())
    slugs = [s for s in scope.slugs()
             if not q or q in "".join(ch for ch in s.lower() if ch.isalnum())]
    h = cursor.fingerprint(tool="repos", q=q)
    off, note = cursor.resolve(cursor_, h, {})
    page = slugs[off:off + DEFAULT_LIMIT]
    entries = common.fresh_entries(page)
    # `shared` after the level marks a repository another workspace shares with
    # this one: the reader learns that its text is not their own organisation's.
    lines = [f"{s} {scope.level(s)}" + (" shared" if scope.is_shared(s) else "")
             for s in page]
    if any(scope.is_shared(s) for s in page):
        # The listing names it and reads nothing of it, so it is not in the call log
        # (the owner would see a `repos` call as a read); the answer still says so.
        from src.mcp_server import callctx

        callctx.note_shown_share()
    # Repositories with no nameable revision are listed but have no idx entry.
    return emit.render_list(
        "repos", entries, lines, total=len(slugs), offset=off, query_hash=h,
        note=note, empty="no repositories you can read match" if q else
        "no repositories you can read; ask a superadmin for access",
        more_hint="query=", pin={},
    )
