"""A new route on a restricted surface cannot arrive without its role gate.

The product rule: `/memories` (and what feeds it) is for editors and above,
`/productivity` is for owners and admins, and anything that stores or spends a
connection (Jira, git, LLM keys) is for owners and admins. The role matrices
test the routes that exist; this test is about the next one. It walks every
router the API mounts, and for each route whose path belongs to a restricted
surface it demands one of that surface's gate dependencies somewhere in the
route's dependency tree. A route that names a surface in its path but is not in
the table fails too: add it with its gate, or choose a path that is not about
that surface.

The table covers both halves of the product: the review features (memories,
learning, productivity, task context, connections) and the repository-access
and MCP-token surface (access rules, access requests, token issuing and the
call log, OAuth client registration). WHICH repositories a role may see on
these surfaces is `tests/security/test_every_review_feature_obeys_repository_
access.py` and `tests/security/test_access_matrix.py`; the MCP tools are walked
by `tests/security/test_every_mcp_tool_output_is_redacted.py` (redaction, every
tool of every server) and `tests/security/test_no_mcp_tool_bypasses_a_role_
gated_surface.py` (no tool carries a role-gated feature around its gate).

The gates are matched by name, so a rename of one has to be made here as well.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest
from fastapi.routing import APIRoute

#: path prefix -> the dependencies, any one of which closes the surface.
SURFACES: dict[str, set[str]] = {
    "/api/memories": {"require_memories_access"},
    "/api/learning": {"require_memories_access"},
    "/api/analytics/productivity": {"require_workspace_admin"},
    "/api/task-context": {"require_workspace_admin"},
    # repository access and per-person MCP tokens
    "/api/access/": {"require_workspace_admin"},
    "/api/admin/access-requests": {"require_superadmin"},
    "/api/admin/mcp-": {"_require_issuer"},
    # Repository sharing, the owner's side: the list is for the active workspace's
    # administrators, every route that names one share for the administrators of
    # the workspace that OWNS it (the next test says which is which).
    "/api/sharing": {"require_workspace_admin", "require_share_admin"},
    "/oauth/clients": {"require_workspace_admin"},
    "/oauth/register": {"require_workspace_admin"},
    # knowledge packs: a workspace role first (reader / author), or a login for
    # the invite-link routes; what a person may do to a PACK is its audience
    # and grants (tests/api/test_the_codebase_routes_answer_by_role_and_audience.py).
    # The /publish routes answer to a pack publish token and nothing else
    # (tests/api/test_a_pack_token_publishes_one_pack_and_nothing_else.py)
    "/api/codebase": {"require_codebase_reader", "require_pack_author", "require_signed_in",
                      "require_pack_token"},
}

#: words that make a path "about" a restricted surface even if its prefix is new.
SURFACE_WORDS = ("memor", "productivity", "task-context", "task_context", "learning", "jira",
                 "mcp", "access", "oauth")

#: (method, path) -> gates, for the few routes that sit on a surface by name but
#: have their own rule. The connection list is open on purpose: it says only
#: whether a provider is connected, and withholds the account from non-admins.
EXCEPTIONS: dict[tuple[str, str], set[str]] = {
    ("GET", "/api/connections"): {"get_current_user"},
    # A person's own view of their own access, and their own request for it:
    # a login, answering for the caller only (tests/security/test_access_matrix.py).
    ("GET", "/api/access/my"): {"get_current_user"},
    ("GET", "/api/access/rules"): {"get_current_user"},
    ("POST", "/api/access-requests"): {"get_current_user"},
    ("GET", "/api/access-requests/me"): {"get_current_user"},
    ("DELETE", "/api/access-requests/me"): {"get_current_user"},
    # Self-service token: a login, and the handler answers 403 unless
    # CELMIS_MCP_SELF_SERVICE is on (tests/api/test_mcp_tokens_endpoints.py).
    ("POST", "/api/mcp/token"): {"get_current_user"},
    # Project-scoped MCP tokens live under their project's path; superadmin only.
    ("GET", "/api/projects/{project_id}/mcp-tokens"): {"require_superadmin"},
    ("POST", "/api/projects/{project_id}/mcp-tokens"): {"require_superadmin"},
    ("DELETE", "/api/projects/{project_id}/mcp-tokens/{token_id}"): {"require_superadmin"},
}

#: Protocol endpoints: discovery documents are public by specification, the
#: consent and token endpoints authenticate in their handlers (a session, a
#: client secret, a grant) and are exercised by tests/security/test_the_consent_
#: page_does_not_take_dictation.py and tests/api/test_mcp_tokens_endpoints.py.
PROTOCOL: set[tuple[str, str]] = {
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-protected-resource/mcp/dev"),
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/authorize/consent"),
    ("POST", "/oauth/token"),
}

#: Routes that write or remove a credential: owner/admin of the workspace.
CREDENTIAL_PREFIX = "/api/connections/"
CREDENTIAL_GATE = {"require_workspace_admin"}


def _router_modules() -> list:
    import src.api.routers as pkg

    found = []
    for info in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        if hasattr(mod, "router"):
            found.append(mod)
    try:  # the enterprise package is optional in a trimmed build
        from src.ee.analytics import productivity_router
        from src.ee.analytics import router as analytics_router
        from src.ee.sharing import router as sharing_grants_router

        found += [analytics_router, productivity_router, sharing_grants_router]
    except ImportError:  # pragma: no cover
        pass
    return found


def _names(dependant, into: set[str]) -> set[str]:
    for sub in dependant.dependencies:
        into.add(getattr(sub.call, "__name__", repr(sub.call)))
        _names(sub, into)
    return into


def _routes() -> list[tuple[str, str, set[str]]]:
    out = []
    for mod in _router_modules():
        for route in mod.router.routes if hasattr(mod, "router") else mod.routes:
            if not isinstance(route, APIRoute):
                continue
            gates = _names(route.dependant, set())
            for method in sorted(route.methods or ()):
                out.append((method, route.path, gates))
    return out


ROUTES = _routes()


def test_the_walk_finds_the_surfaces_it_guards() -> None:
    """Guards the guard: an empty walk would make every test below pass."""
    paths = {path for _, path, _ in ROUTES}
    for prefix in ("/api/memories", "/api/task-context", "/api/connections", "/api/learning",
                   "/api/access/", "/api/admin/mcp-tokens", "/api/admin/mcp-calls",
                   "/api/admin/mcp-shares", "/api/sharing/shares",
                   "/api/codebase/packs", "/api/codebase/invites"):
        assert any(p.startswith(prefix) for p in paths), prefix
    assert len(ROUTES) > 150


@pytest.mark.parametrize(
    ("method", "path", "gates"),
    [r for r in ROUTES
     if any(r[1].startswith(p) for p in SURFACES) and (r[0], r[1]) not in EXCEPTIONS],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_route_on_a_restricted_surface_has_that_surfaces_gate(method, path, gates) -> None:
    wanted = next(g for prefix, g in SURFACES.items() if path.startswith(prefix))
    assert gates & wanted, f"{method} {path} is missing one of {sorted(wanted)}; it has {sorted(gates)}"


@pytest.mark.parametrize(
    ("method", "path", "gates"),
    [r for r in ROUTES if r[1].startswith(CREDENTIAL_PREFIX) and r[0] != "GET"],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_route_that_saves_or_removes_a_credential_is_for_owners_and_admins(
    method, path, gates,
) -> None:
    assert gates & CREDENTIAL_GATE, f"{method} {path}: {sorted(gates)}"


def test_the_connection_list_is_the_one_open_door_and_it_is_still_a_login() -> None:
    gates = {(m, p): g for m, p, g in ROUTES}[("GET", "/api/connections")]
    assert gates & EXCEPTIONS[("GET", "/api/connections")]


def test_a_path_that_names_a_restricted_surface_is_in_the_table() -> None:
    stray = [
        f"{method} {path}"
        for method, path, _ in ROUTES
        if any(word in path.lower() for word in SURFACE_WORDS)
        and not any(path.startswith(prefix) for prefix in SURFACES)
        and (method, path) not in EXCEPTIONS
        and (method, path) not in PROTOCOL
    ]
    assert not stray, (
        "These routes are about a restricted surface but belong to none of the SURFACES "
        f"prefixes, so nothing proves their role gate: {stray}")


def test_every_exception_and_protocol_entry_is_a_route_that_exists() -> None:
    """An entry for a route that was renamed would silently exempt nothing and
    hide the new name from the table."""
    mounted = {(m, p) for m, p, _ in ROUTES}
    missing = [e for e in (*EXCEPTIONS, *PROTOCOL) if e not in mounted]
    assert not missing, f"not mounted any more: {missing}"


def test_the_two_projects_are_both_in_the_table() -> None:
    """The review features and the access/MCP surface share one walk."""
    mounted = {p for _, p, _ in ROUTES}
    assert any(p.startswith("/api/learning") for p in mounted)
    assert any(p.startswith("/api/admin/mcp-tokens") for p in mounted)
    for prefix in SURFACES:
        assert any(p.startswith(prefix) for p in mounted), f"{prefix} matches no route"


def test_the_project_token_routes_are_for_the_superadmin_alone() -> None:
    gates = {(m, p): g for m, p, g in ROUTES}
    for (method, path), wanted in EXCEPTIONS.items():
        if path.startswith("/api/projects/") and path.endswith(("mcp-tokens", "{token_id}")):
            assert gates[(method, path)] & wanted, (method, path)


def test_a_route_that_names_one_share_is_gated_on_the_workspace_that_owns_it() -> None:
    """`require_workspace_admin` alone would let the administrator of whatever
    workspace the caller works in read or end somebody else's share. The gate that
    loads the share and compares its OWNER is the one these routes need, and the
    list (which names no share) is the one that asks about the active workspace."""
    sharing = [(m, p, g) for m, p, g in ROUTES if p.startswith("/api/sharing/")]
    assert len(sharing) >= 5, "the owner's routes are mounted"
    for method, path, gates in sharing:
        if "{share_id}" in path:
            assert "require_share_admin" in gates, (method, path, sorted(gates))
            assert "require_workspace_admin" not in gates, (method, path, sorted(gates))
        else:
            assert "require_workspace_admin" in gates, (method, path, sorted(gates))


def test_the_owners_side_of_sharing_mounts_no_route_that_creates_or_extends_a_share() -> None:
    """Creating and extending is the licensed part (`src/ee`); the free router that
    every build mounts must not offer either, or the licence would gate nothing."""
    mounted = {(m, p) for m, p, _ in ROUTES if p.startswith("/api/sharing/")}
    assert ("POST", "/api/sharing/shares") not in mounted
    assert not [r for r in mounted if r[1].endswith("/extend")], mounted
    # Narrowing is the only change the free router makes to a share.
    assert {m for m, p in mounted if p == "/api/sharing/shares/{share_id}"} == {"GET", "PATCH"}


def test_the_licensed_routes_of_sharing_are_gated_on_the_right_workspace() -> None:
    """`/api/sharing-grants` is on the `/api/sharing` surface by its prefix, which only
    proves that one of the two gates is there. Making a share is for the administrators
    of the ACTIVE workspace (the owner of the repository being shared); extending names a
    share and is for the administrators of the workspace that OWNS it."""
    grants = {(m, p): g for m, p, g in ROUTES if p.startswith("/api/sharing-grants")}
    assert set(grants) == {("POST", "/api/sharing-grants"),
                           ("POST", "/api/sharing-grants/{share_id}/extend"),
                           ("GET", "/api/sharing-grants/workspaces")}, sorted(grants)
    made = grants[("POST", "/api/sharing-grants")]
    assert "require_workspace_admin" in made and "require_share_admin" not in made
    extended = grants[("POST", "/api/sharing-grants/{share_id}/extend")]
    assert "require_share_admin" in extended and "require_workspace_admin" not in extended
    # The picker of the create dialog is for the same people as making a share.
    listed = grants[("GET", "/api/sharing-grants/workspaces")]
    assert "require_workspace_admin" in listed and "require_share_admin" not in listed
