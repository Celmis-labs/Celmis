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
}

#: words that make a path "about" a restricted surface even if its prefix is new.
SURFACE_WORDS = ("memor", "productivity", "task-context", "task_context", "learning", "jira")

#: (method, path) -> gates, for the few routes that sit on a surface by name but
#: have their own rule. The connection list is open on purpose: it says only
#: whether a provider is connected, and withholds the account from non-admins.
EXCEPTIONS: dict[tuple[str, str], set[str]] = {
    ("GET", "/api/connections"): {"get_current_user"},
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

        found += [analytics_router, productivity_router]
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
    for prefix in ("/api/memories", "/api/task-context", "/api/connections"):
        assert any(p.startswith(prefix) for p in paths), prefix
    assert len(ROUTES) > 150


@pytest.mark.parametrize(
    ("method", "path", "gates"),
    [r for r in ROUTES if any(r[1].startswith(p) for p in SURFACES)],
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
    ]
    assert not stray, (
        "These routes are about a restricted surface but belong to none of the SURFACES "
        f"prefixes, so nothing proves their role gate: {stray}")
