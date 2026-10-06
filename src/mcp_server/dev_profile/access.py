"""Who may see which repository, for the dev tools.

The dev tools never call `caller_access` / `resolve_access` themselves: they
ask :func:`mcp_scope` for the repositories the current caller can research and
go through :meth:`DevScope.get` for every one they touch. A repository the
caller may not research is simply ABSENT — the same words
(:data:`NOT_ACCESSIBLE`) for "does not exist" and "not yours", so a name cannot
be probed for existence.

:func:`mcp_scope` resolves through ``src.access.effective.mcp_scope``: a
repository without a rule is closed, a token lists exactly which repos it may
read (the superadmin's list is authoritative), and only repositories of the
token's workspace exist for the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from src.access.effective import NOT_ACCESSIBLE, RepoNotAccessible  # noqa: F401

logger = logging.getLogger(__name__)

Need = Literal["metadata", "code"]


@dataclass
class DevScope:
    """The repositories one MCP call may research, and how deeply."""

    decisions: dict = field(default_factory=dict)  # slug -> RepoAccessDecision
    user_id: str = ""
    workspace_id: str = ""
    #: Slugs touched by this call, in first-touch order (for the audit hook).
    touched: list[str] = field(default_factory=list)
    #: Who the caller is, for the audit and for write gating (read-only here).
    token_id: str | None = None
    kind: str = ""
    allow_write: bool = False

    def slugs(self, *, need: Need = "metadata") -> list[str]:
        out = []
        for slug, dec in sorted(self.decisions.items()):
            if need == "code" and not dec.code_visible:
                continue
            out.append(slug)
        return out

    def get(self, slug: str, *, need: Need = "code"):  # noqa: ANN201
        dec = self.decisions.get(slug)
        if dec is None or (need == "code" and not dec.code_visible):
            raise RepoNotAccessible
        if slug not in self.touched:
            self.touched.append(slug)
            try:
                from src.mcp_server import callctx

                callctx.note_repos(slug)
            except Exception:  # noqa: BLE001 - audit bookkeeping never blocks a read
                pass
        return dec

    def level(self, slug: str) -> str:
        dec = self.decisions.get(slug)
        return "code" if dec is not None and dec.code_visible else "metadata"

    def listable(self, slug: str, path: str) -> bool:
        """May the NAME of `path` be shown (outlines, hit lists)?

        Code-visible repos follow `path_visible`; metadata-only repos show
        every path a deny rule does not conceal. A secret file's name is never
        shown either way.
        """
        from src.mcp_server.dev_profile.paths import is_secret_path

        dec = self.decisions.get(slug)
        if dec is None or is_secret_path(path):
            return False
        if dec.code_visible:
            return dec.path_visible(path)
        return not dec.path_denied(path)

    def readable(self, slug: str, path: str) -> bool:
        """May the CONTENT of `path` be shown (bodies, grep lines)?"""
        from src.mcp_server.dev_profile.paths import is_secret_path

        dec = self.decisions.get(slug)
        return (dec is not None and dec.code_visible
                and not is_secret_path(path) and dec.path_visible(path))

    def resolve(self, name: str, *, need: Need = "metadata") -> str:
        """The indexed slug a caller's spelling of a repository means.

        Exact slug, then a punctuation-insensitive match (`acme/shop` for
        `github_acme-shop`), then a unique suffix match (`shop`). Candidates
        are only repositories the caller can research, so an ambiguous answer
        cannot disclose a neighbour's repo. Raises :class:`RepoNotAccessible`
        for nothing matching AND for several matching.
        """
        pool = self.slugs(need="metadata")
        want = (name or "").strip()
        if want in pool:
            self.get(want, need=need)
            return want

        def norm(v: str) -> str:
            return "".join(ch for ch in v.lower() if ch.isalnum())

        n = norm(want)
        if not n:
            raise RepoNotAccessible
        hits = [s for s in pool if norm(s) == n]
        if not hits:
            hits = [s for s in pool if norm(s).endswith(n) or n.endswith(norm(s))]
        if len(hits) != 1:
            raise RepoNotAccessible
        self.get(hits[0], need=need)
        return hits[0]

    def similar(self, name: str, limit: int = 3) -> list[str]:
        """Accessible repository names close to `name` (for the error line)."""
        import difflib

        return difflib.get_close_matches(name, self.slugs(), n=limit, cutoff=0.4)


def indexed_slugs() -> list[str]:
    """Every repository with a clone AND a graph on this installation."""
    from src.config import InvalidRepoSlug, get_settings

    settings = get_settings()
    root = settings.repos_dir
    if not root.exists():
        return []
    out: list[str] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or not (sub / ".git").exists():
            continue
        try:
            if settings.repo_graph_path(sub.name).exists():
                out.append(sub.name)
        except InvalidRepoSlug:
            continue
    return out


def mcp_scope(candidates=None) -> DevScope:  # noqa: ANN001
    """The current MCP caller's research scope over the indexed repositories."""
    from src.access.effective import mcp_scope as effective_scope

    slugs = list(candidates) if candidates is not None else indexed_slugs()
    base = effective_scope(slugs)
    return DevScope(
        decisions=dict(base.decisions),
        user_id=base.user_id or "",
        workspace_id=base.workspace_id or "",
        token_id=base.token_id,
        kind=base.kind,
        allow_write=base.allow_write,
    )


def require_dev_scope() -> None:
    """Refuse a token that was not issued for the dev profile.

    The transport already demands `read:code` (see `build_dev_mcp`); this is
    the same check inside the tool, for a call that reaches it another way. No
    token at all (stdio, in-process) is the trusted local case, as everywhere
    else in this server.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    from src.mcp_server.dev_contract import SCOPE_DEV

    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001
        return
    if token is None:
        return
    scopes = set(token.scopes or [])
    if SCOPE_DEV not in scopes and "admin" not in scopes:
        raise ToolError(
            f"this token has no '{SCOPE_DEV}' scope; ask the superadmin for a "
            "developer token")
