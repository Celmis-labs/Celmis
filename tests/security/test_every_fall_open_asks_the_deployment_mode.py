"""Five places answered "I don't know whose this is" with "then everyone may".

An audit found them together, and what made them one finding rather than five
is that each degrades in the same direction: no access rows means allow, no
rule for the repo means full access, an ImportError means global admin, an
unreadable budget row means no cap, a failed provisioning means the shared
"default" tenant. An expired licence or a missing module made this system MORE
open, not less.

They are not bugs on a single-tenant box — they are what lets a fresh install
work before anybody has written a rule. So they are not removed; they are
attached to an explicit deployment mode (src/deployment.py), default
single_tenant, which is today's behaviour byte for byte.

This file holds the invariant that the mode is not decoration:

  * structurally — every site asks `fall_open_allowed` with its own site id,
    and that answer *chooses a branch*; a site that merely logged the mode and
    fell open anyway would pass a "does the module get imported" test and fail
    this one. The check reads AST nodes (call names, constant arguments,
    branch tests) and never the source text, so a comment mentioning the guard
    can never satisfy it.

  * behaviourally — with the mode set to multi_tenant, each reachable site is
    asked for access and refuses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src import deployment
from src.deployment import FALL_OPEN_SITES, DeploymentMode

ROOT = Path(__file__).resolve().parents[2]

#: (module, enclosing function, site id, what refusing looks like). The
#: enclosing function is walked whole, so a guard inside a nested def
#: (require_repo_permission._dep) counts. The last column is checked against
#: the code reachable when the guard says False — "raise" an exception,
#: "denied" a RepoAccessDecision.denied(), "not_admin" an identity built with
#: a literal False where the admin flag goes.
SITES: list[tuple[str, str, str, str]] = [
    ("src/api/deps.py", "enforce_repo_permission", "api.deps.repo_permission",
     "raise"),
    ("src/api/deps.py", "require_repo_permission", "api.deps.repo_permission",
     "raise"),
    ("src/api/deps.py", "current_workspace_id", "api.deps.workspace_provision",
     "raise"),
    ("src/access/resolver.py", "resolve_access_sync", "access.resolver.no_rule",
     "denied"),
    ("src/mcp_server/identity.py", "_no_identity", "mcp.identity.no_auth_context",
     "not_admin"),
    ("src/mcp_server/identity.py", "caller_access",
     "mcp.identity.unauthenticated_access", "denied"),
    ("src/llm/budget.py", "get_status", "llm.budget.unreadable", "raise"),
    ("src/llm/budget.py", "month_spend", "llm.budget.unreadable", "raise"),
]


# ─── AST helpers (nodes only — never the source text) ────────────────


def _func(module_path: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / module_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from {module_path} — check this test")


def _guard_calls(node: ast.AST) -> list[ast.Call]:
    """Every `fall_open_allowed(...)` call inside ``node``."""
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        called = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else ""
        )
        if called == "fall_open_allowed":
            out.append(sub)
    return out


def _site_ids(call: ast.Call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _tests_on(node: ast.AST, site: str) -> bool:
    """True if this expression's value depends on fall_open_allowed(site)."""
    return any(site in _site_ids(c) for c in _guard_calls(node))


def _negated(test: ast.AST) -> bool:
    return isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)


def _refusal_path(fn: ast.AST, site: str) -> list[ast.AST] | None:
    """The nodes reachable exactly when ``fall_open_allowed(site)`` is False.

    ``None`` when no branch depends on the guard at all — asking and ignoring
    the answer is the failure this exists to catch.
    """
    found = False
    path: list[ast.AST] = []
    for owner in ast.walk(fn):
        # `x if fall_open_allowed(...) else y` — the refusal is `y`.
        if isinstance(owner, ast.IfExp) and _tests_on(owner.test, site):
            found = True
            path.append(owner.orelse if not _negated(owner.test) else owner.body)
            continue
        for field in ("body", "orelse", "finalbody"):
            block = getattr(owner, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if not (isinstance(stmt, ast.If) and _tests_on(stmt.test, site)):
                    continue
                found = True
                if _negated(stmt.test):
                    path.extend(stmt.body)          # `if not allowed: refuse`
                elif stmt.orelse:
                    path.extend(stmt.orelse)        # `if allowed: … else: refuse`
                elif stmt.body and isinstance(stmt.body[-1], (ast.Return, ast.Raise)):
                    path.extend(block[i + 1:])      # `if allowed: return` … refuse
    return path if found else None


def _looks_like_refusing(nodes: list[ast.AST], kind: str) -> bool:
    for node in nodes:
        for sub in ast.walk(node):
            if kind == "raise" and isinstance(sub, ast.Raise):
                return True
            if kind == "denied" and isinstance(sub, ast.Call) and (
                (isinstance(sub.func, ast.Attribute) and sub.func.attr == "denied")
                or (isinstance(sub.func, ast.Name) and sub.func.id == "denied")
            ):
                return True
            if kind == "not_admin" and isinstance(sub, ast.Call) and any(
                isinstance(a, ast.Constant) and a.value is False for a in sub.args
            ):
                return True
    return False


# ─── structural: the guard is there, and it decides something ────────


@pytest.mark.parametrize("module_path,func_name,site,refusal", SITES,
                         ids=[f"{m.split('/')[-1]}:{f}" for m, f, _, _ in SITES])
def test_the_fall_open_asks_the_mode(module_path: str, func_name: str, site: str,
                                     refusal: str):
    fn = _func(module_path, func_name)
    calls = _guard_calls(fn)
    assert calls, (
        f"{module_path}:{func_name}() falls open without asking "
        f"fall_open_allowed() — it would grant access in every deployment mode"
    )
    ids = {i for c in calls for i in _site_ids(c)}
    assert site in ids, (
        f"{module_path}:{func_name}() guards on {sorted(ids)}, not {site!r} — "
        f"the site id is what the operator reads in the refusal log"
    )


@pytest.mark.parametrize("module_path,func_name,site,refusal", SITES,
                         ids=[f"{m.split('/')[-1]}:{f}" for m, f, _, _ in SITES])
def test_the_answer_chooses_a_branch(module_path: str, func_name: str, site: str,
                                     refusal: str):
    """Asking and ignoring the answer is the failure this test exists for."""
    fn = _func(module_path, func_name)
    path = _refusal_path(fn, site)
    assert path is not None, (
        f"{module_path}:{func_name}() calls fall_open_allowed({site!r}) but no "
        f"branch depends on the result — the mode would be logged and ignored"
    )
    assert path, (
        f"{module_path}:{func_name}(): nothing runs when "
        f"fall_open_allowed({site!r}) is False — the permissive branch is "
        f"skipped and the function falls through to the same outcome"
    )
    assert _looks_like_refusing(path, refusal), (
        f"{module_path}:{func_name}(): the path taken when "
        f"fall_open_allowed({site!r}) is False does not refuse "
        f"({refusal!r} expected) — both sides of the mode grant access"
    )


def test_every_registered_site_is_guarded_somewhere():
    """FALL_OPEN_SITES is the operator-facing list of what the mode governs.
    An entry nobody guards is a promise the code does not keep."""
    guarded = {site for _, _, site, _ in SITES}
    assert set(FALL_OPEN_SITES) == guarded, (
        "src/deployment.py:FALL_OPEN_SITES and this test disagree about which "
        "sites exist: " + str(set(FALL_OPEN_SITES) ^ guarded)
    )


# ─── the mode itself ─────────────────────────────────────────────────


@pytest.fixture()
def mode(monkeypatch):
    """Set CELMIS_DEPLOYMENT_MODE for one test, cache reset both ways."""
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv(deployment.ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(deployment.ENV_VAR, value)
        deployment.reset_mode_cache()
        return deployment.get_mode()
    yield _set
    monkeypatch.undo()
    deployment.reset_mode_cache()


def test_the_default_is_todays_behaviour(mode):
    """Production runs three workspaces with almost no rules configured. If an
    upgrade changed the default, every one of them would be denied at once."""
    assert mode(None) is DeploymentMode.SINGLE_TENANT
    assert deployment.is_single_tenant() is True
    assert deployment.fall_open_allowed("access.resolver.no_rule") is True


def test_multi_tenant_refuses_at_every_site(mode):
    assert mode("multi_tenant") is DeploymentMode.MULTI_TENANT
    for site in FALL_OPEN_SITES:
        assert deployment.fall_open_allowed(site) is False, site


def test_a_misspelt_mode_is_not_the_open_one(mode):
    """A typo in a tenancy switch must not resolve to 'everyone may'."""
    with pytest.raises(deployment.DeploymentModeError):
        mode("mutli_tenant")
    with pytest.raises(deployment.DeploymentModeError):
        deployment.parse_mode("off")


def test_spelling_variants_are_accepted(mode):
    assert mode("MULTI-TENANT") is DeploymentMode.MULTI_TENANT
    assert mode("  single-tenant ") is DeploymentMode.SINGLE_TENANT


# ─── behaviour: each reachable site actually refuses ─────────────────


def test_a_repo_with_no_grant_is_refused(mode, monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from src.api import deps as deps_mod

    # The third argument is the tenant: a grant may be stored under either
    # spelling of the repository, and only the workspace knows which one
    # this repository was registered under.
    async def _no_grants(repo_slug, user, workspace_id=None):
        return None, False

    monkeypatch.setattr(deps_mod, "_effective_repo_permission", _no_grants)
    user = type("U", (), {"id": "u1", "is_admin": False})()

    mode(None)
    asyncio.run(deps_mod.enforce_repo_permission("acme/api", user))  # allowed

    mode("multi_tenant")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(deps_mod.enforce_repo_permission("acme/api", user))
    assert exc.value.status_code == 403


def test_a_repo_with_no_rule_is_refused(mode):
    from src.access.resolver import resolve_access_sync

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        def execute(self, *_a, **_k):
            return _Result()

    kw = dict(user_id="u1", is_admin=False, workspace_id="ws-a", repos=["acme/api"])

    mode(None)
    assert resolve_access_sync(_Session(), **kw)["acme/api"].code_visible is True

    mode("multi_tenant")
    denied = resolve_access_sync(_Session(), **kw)["acme/api"]
    assert denied.researchable is False
    assert denied.path_visible("README.md") is False


def test_an_mcp_caller_with_no_identity_is_not_an_admin(mode):
    from src.mcp_server.identity import caller_access, resolve_caller

    mode(None)
    caller, access = caller_access(["acme/api"])
    assert caller.is_admin is True and access["acme/api"].code_visible is True

    mode("multi_tenant")
    caller, access = caller_access(["acme/api"])
    assert caller.is_admin is False, "no bearer identity resolved to global admin"
    assert resolve_caller().is_admin is False
    assert access["acme/api"].researchable is False


def test_an_unreadable_budget_is_not_an_unlimited_one(mode, monkeypatch):
    from src.llm import budget as budget_mod

    def _broken():
        raise RuntimeError("database is down")

    monkeypatch.setattr(budget_mod, "_engine", _broken)

    mode(None)
    assert budget_mod.get_status("ws-a").cap_usd == 0.0     # unlimited, as before
    assert budget_mod.enforce("ws-a").enabled is False

    mode("multi_tenant")
    with pytest.raises(budget_mod.BudgetUnavailable):
        budget_mod.get_status("ws-a")
    with pytest.raises(budget_mod.BudgetExceeded):
        budget_mod.enforce("ws-a")  # the subclass every caller already handles


# ─── the startup warning ─────────────────────────────────────────────


def test_more_than_one_workspace_in_single_tenant_says_so(mode, monkeypatch):
    mode(None)
    monkeypatch.setattr(deployment, "count_workspaces", lambda: 3)
    msg = deployment.warn_if_multi_workspace()
    assert msg and "3" in msg and deployment.ENV_VAR in msg

    monkeypatch.setattr(deployment, "count_workspaces", lambda: 1)
    assert deployment.warn_if_multi_workspace() is None

    monkeypatch.setattr(deployment, "count_workspaces", lambda: None)
    assert deployment.warn_if_multi_workspace() is None, "a DB that is down is not a warning"

    mode("multi_tenant")
    monkeypatch.setattr(deployment, "count_workspaces", lambda: 3)
    assert deployment.warn_if_multi_workspace() is None


def test_counting_workspaces_never_raises(monkeypatch):
    """It runs at startup, before the database is necessarily up."""
    monkeypatch.setattr("src.access.resolver._sync_engine",
                        lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    assert deployment.count_workspaces() is None


# ─── the signing secret ──────────────────────────────────────────────


def test_the_repository_no_longer_ships_a_signing_secret_at_all():
    """This used to assert that the shipped placeholder was REFUSED.

    The premise is gone, and that is the better outcome: `.env.example` now
    ships `CELMIS_JWT_SECRET=` with the command to generate one beside it,
    and docker-compose uses `${CELMIS_JWT_SECRET:?...}` so a missing value
    stops `compose up` by name — before a container starts, rather than
    after the API refuses to serve.

    A refusal at runtime is the safety net. Not having a usable placeholder
    in a public file is the actual fix, and this test now guards that.
    """
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    m = re.search(r"^CELMIS_JWT_SECRET=(.*)$", env_example, re.M)
    assert m, "the example no longer mentions the signing secret at all"
    shipped = m.group(1).split("#")[0].strip()
    assert shipped == "", (
        f"the example ships a usable signing secret ({shipped!r}) — anyone "
        f"reading the repository could mint an admin session on a stack that "
        f"copied it"
    )

    for line in compose.splitlines():
        if line.strip().startswith("#") or "CELMIS_JWT_SECRET:" not in line:
            continue
        assert ":?" in line, (
            f"compose still supplies a fallback signing secret: {line.strip()}"
        )


def test_the_refusal_still_works_for_anything_placeholder_shaped(monkeypatch):
    """The net under the fix above: whatever an operator invents, the values
    that read as placeholders are still refused at startup."""
    from src.api import jwt_auth

    for value in ("change-me-in-production", "changeme", "your-secret-here",
                  "replace-me", "insecure", "x"):
        assert jwt_auth.secret_problem(value) is not None, value
        monkeypatch.setenv("CELMIS_JWT_SECRET", value)
        monkeypatch.setattr(jwt_auth, "_secret_cache", None)
        with pytest.raises(jwt_auth.WeakJwtSecretError):
            jwt_auth.assert_secret_usable()


def test_a_real_secret_still_starts(monkeypatch):
    """Production runs on a 64-char random value and must keep booting."""
    from src.api import jwt_auth

    real = "01T8HK4XRt5RfEIFS1ch3UlSQFdqVDjRK4gh0Hj7PFMa7hUPg50dl7cdR59LA0Q"
    assert jwt_auth.secret_problem(real) is None
    monkeypatch.setenv("CELMIS_JWT_SECRET", real)
    monkeypatch.setattr(jwt_auth, "_secret_cache", None)
    jwt_auth.assert_secret_usable()
    assert jwt_auth._get_secret() == real.encode()


def test_an_install_that_never_set_one_is_not_blocked(monkeypatch):
    """No env var → Celmis generates and stores its own key. That is a real
    secret, and the startup gate must not refuse it."""
    from src.api import jwt_auth

    monkeypatch.delenv("CELMIS_JWT_SECRET", raising=False)
    jwt_auth.assert_secret_usable()


def test_a_short_secret_is_refused_at_startup(monkeypatch):
    from src.api import jwt_auth

    monkeypatch.setenv("CELMIS_JWT_SECRET", "hunter2")
    with pytest.raises(jwt_auth.WeakJwtSecretError):
        jwt_auth.assert_secret_usable()
