"""``GET /api/capabilities`` — the edition probe an unauthenticated shell reads.

Three properties, in the order they would hurt if they broke:

1. **Coverage.** Every prefix mounted on the real app is claimed by some
   feature, and every prefix a feature claims is really mounted. Without the
   first half, a new router is a page the client never learns about; without
   the second, a typo in `FEATURES` makes a feature permanently unavailable
   and *hides a working page* — the failure that outranks the bug.
2. **Derivation.** Availability follows the route table, so an app built
   without a router reports that feature off. This is what stops the answer
   being a constant that agrees with reality only on the day it was written.
3. **Disclosure.** An anonymous caller gets features and no deployment mode,
   the endpoint never 401s whatever it is handed, and the payload has no key
   outside the published contract.

These build the app rather than reading its source, because the thing being
pinned is what the endpoint *answers*. A test that grepped
`src/api/routers/capabilities.py` for "workspace" would pass on a file whose
only mention is the comment explaining that it does not disclose one.
"""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from src.api.routers import capabilities as caps

#: Long enough for `issue_token`; deliberately not a placeholder marker.
SECRET = "test-capabilities-secret-0123456789abcdef"


@pytest.fixture(scope="module")
def app():
    """The real application, plus the one line main.py is asked to add."""
    import os

    os.environ.setdefault("CELMIS_JWT_SECRET", SECRET)
    os.environ.setdefault("GEMINI_API_KEY", "test-key-12345678")
    from src.api.main import build_app

    application = build_app()
    application.include_router(caps.router)
    return application


@pytest.fixture(scope="module")
def client(app):
    return TestClient(app)


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("CELMIS_JWT_SECRET", SECRET)
    from src.api import jwt_auth

    monkeypatch.setattr(jwt_auth, "_secret_cache", None, raising=False)
    raw, _ = jwt_auth.issue_token(user_id="u-1", email="someone@example.com")
    return raw


# ─── 1. coverage: the map and the route table must agree ─────────────


def test_every_mounted_prefix_is_claimed_by_a_feature(app):
    """A router nobody classified is a page the client cannot be told about.

    This is the whole anti-drift mechanism: `FEATURES` is written down, so the
    only thing keeping it true is that adding a router without classifying it
    turns this red on the commit that does it.
    """
    mounted = caps.mounted_prefixes(app)
    claimed = {p for f in caps.FEATURES for p in f.prefixes}
    unclaimed = sorted(mounted - claimed)
    assert not unclaimed, (
        f"mounted but claimed by no feature: {unclaimed}. Add each to a Feature "
        f"in src/api/routers/capabilities.py — with pages=() if it backs no page."
    )


def test_no_feature_claims_a_prefix_that_is_not_mounted(app):
    """The dangerous direction. A misspelt prefix can never be satisfied, so
    its feature reports `available: false` for ever and a client that trusts
    this document hides a page that works perfectly well."""
    mounted = caps.mounted_prefixes(app)
    phantom = sorted({p for f in caps.FEATURES for p in f.prefixes} - mounted)
    assert not phantom, (
        f"claimed by a feature but not mounted: {phantom}. Every one of these "
        f"permanently hides the pages of its feature."
    )


def test_the_real_installation_reports_every_feature_available(app):
    """Nothing is missing from THIS build, so nothing may be reported missing.

    The regression this catches is a detection change that quietly starts
    answering "off" in production — which the endpoint's own consumers would
    render as a shrinking menu rather than as an error.
    """
    doc = caps.build_capabilities(app)
    off = sorted(k for k, v in doc.features.items() if not v.available)
    assert not off, f"reported off on a complete build: {off}"
    assert doc.edition == "full"
    assert doc.degraded is False
    assert not [p for p, ok in doc.pages.items() if not ok]


# ─── 2. derivation: the answer follows the routes ────────────────────


def _small_app() -> FastAPI:
    """An installation with repositories but no Q&A and no code review."""
    from src.api.routers import auth, connections, repos, workspaces

    application = FastAPI(version="9.9.9")
    for router in (caps.router, auth.router, workspaces.router,
                   repos.router, connections.router):
        application.include_router(router)
    return application


def test_a_missing_module_reports_its_feature_off_and_hides_its_pages():
    """The claim the endpoint exists to make. If this passed on a hardcoded
    list it would pass on a lie, so it is asserted against an app that really
    was built without those routers."""
    doc = caps.build_capabilities(_small_app())

    assert doc.features["repositories"].available is True
    assert doc.pages["/repositories"] is True

    assert doc.features["qa"].available is False
    assert doc.features["code_review"].available is False
    # …and the client is told which routes to drop, not just which key is off.
    assert doc.pages["/projects"] is False
    assert doc.pages["/chats"] is False
    assert doc.pages["/reviews"] is False

    assert doc.edition == "partial"
    assert doc.api_version == "9.9.9"


def test_a_partly_mounted_feature_is_off_rather_than_on():
    """`all`, not `any`. Half of Code review is a page that loads and then
    fails on its second request — worse than a page that is not offered."""
    from src.api.routers import reviews

    application = FastAPI()
    application.include_router(caps.router)
    application.include_router(reviews.router)   # /api/reviews, but not the rest
    doc = caps.build_capabilities(application)

    assert doc.features["code_review"].available is False


def test_a_mounted_subapp_does_not_invent_top_level_prefixes():
    """`Mount` is a leaf.

    A mounted sub-app's routes have paths relative to the mount, so descending
    into them would read the MCP server's own `/.well-known/...` as a
    top-level prefix of this API — a feature reported present because a
    different application has a route by that name.
    """
    inner = FastAPI()

    @inner.get("/impostor")
    def _impostor() -> dict:
        return {}

    application = FastAPI()
    application.include_router(caps.router)
    application.router.routes.append(Mount("/mcp", app=inner))

    prefixes = caps.mounted_prefixes(application)
    assert "/mcp" in prefixes, "the mount point itself is the fact worth having"
    assert "/impostor" not in prefixes


def test_route_discovery_survives_this_fastapi_version(app):
    """`include_router` produces an opaque `_IncludedRouter` on FastAPI 0.141:
    no `.path`, children behind a private `.original_router`. The naive walk
    does not raise, it silently finds almost nothing — so the traversal is
    pinned by a count that a regression would collapse."""
    assert len(caps._paths(app)) > 100


# ─── 3. disclosure: what an anonymous caller may see ─────────────────


def test_it_answers_without_credentials(client):
    """It is read on first paint, before sign-in. A 401 here is a blank app."""
    r = client.get("/api/capabilities")
    assert r.status_code == 200, r.text[:300]
    assert r.json()["features"]["qa"]["available"] is True


def test_anonymous_callers_are_not_told_the_deployment_mode(client):
    """The mode is not a marketing label: `src/deployment.py` makes it the
    answer to "do the fall-open paths fall open", and `single_tenant` next to
    a feature list confirming /mcp is mounted names the unauthenticated attack
    to try first. Withheld — and `source` says retrying with a token helps."""
    body = client.get("/api/capabilities").json()

    assert body["deployment"]["mode"] == "unknown"
    assert body["deployment"]["source"] == "requires_auth"
    assert "single_tenant" not in client.get("/api/capabilities").text


def test_a_signed_in_caller_is_told_the_deployment_mode(client, token):
    body = client.get(
        "/api/capabilities", headers={"Authorization": f"Bearer {token}"},
    ).json()

    from src.deployment import get_mode

    assert body["deployment"]["source"] == "src.deployment"
    assert body["deployment"]["mode"] == get_mode().value


@pytest.mark.parametrize("header", [
    "Bearer not-a-token",
    "Bearer ",
    "Basic dXNlcjpwYXNz",
    "garbage",
])
def test_a_bad_token_still_gets_the_public_document(client, header):
    """Optional auth, not accept-or-401. An expired session must not turn the
    shell blank — it degrades to the anonymous answer, which is complete
    enough to paint with."""
    r = client.get("/api/capabilities", headers={"Authorization": header})

    assert r.status_code == 200, r.text[:200]
    assert r.json()["deployment"]["mode"] == "unknown"
    assert r.json()["features"]["qa"]["available"] is True


def test_only_the_mode_depends_on_who_is_asking(client, token):
    """One installation, one edition and one feature list, whoever asks.

    The mode is the single auth-gated field. This pins a real slip: the
    edition was read as a by-product of the auth-gated mode lookup, so an
    anonymous caller silently got the derived label while a signed-in one got
    the module's — the same box naming two editions.
    """
    anon = client.get("/api/capabilities").json()
    authed = client.get(
        "/api/capabilities", headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert {k: v for k, v in anon.items() if k != "deployment"} == \
           {k: v for k, v in authed.items() if k != "deployment"}
    assert anon["deployment"] != authed["deployment"]


def test_the_payload_carries_nothing_outside_its_contract(client):
    """Pinning the key set is what keeps the next well-meant addition — a
    workspace count, a build SHA, the configured Qdrant URL — from arriving on
    an endpoint that answers strangers."""
    body = client.get("/api/capabilities").json()

    assert set(body) == {
        "schema_version", "product", "api_version", "edition", "edition_source",
        "deployment", "features", "pages", "degraded",
    }
    assert set(body["deployment"]) == {"mode", "source"}
    for name, feature in body["features"].items():
        assert set(feature) == {"available", "pages"}, name


def test_every_string_on_the_wire_comes_from_a_fixed_vocabulary(client, token, app):
    """Nothing runtime-derived may appear in the body — at either
    authentication level.

    Asserted as a closed vocabulary rather than as a blocklist of scary
    substrings. A blocklist only catches the leak somebody already imagined,
    and the first version of this test banned "workspaces" and failed on the
    page path `/admin/workspaces`. A vocabulary catches the leak nobody
    imagined: a workspace name, a repo slug, a configured Qdrant URL or a
    build SHA is not in `FEATURES`, so it cannot pass.

    Both `_is_single_tenant()` in routers/llm.py and
    `deployment.count_workspaces()` answer the tenancy question by counting
    rows. Neither may be wired in here — which also keeps a database query off
    the anonymous first-paint path.
    """
    from src.deployment import DeploymentMode

    allowed = (
        {"schema_version", "product", "api_version", "edition", "edition_source",
         "deployment", "features", "pages", "degraded", "mode", "source",
         "available"}
        | {f.key for f in caps.FEATURES}
        | {p for f in caps.FEATURES for p in f.pages}
        | {"celmis", str(app.version), "full", "partial", "unknown"}
        | {"derived", "src.deployment", "requires_auth", "unavailable", "degraded"}
        | {m.value for m in DeploymentMode}
    )

    def strings(node) -> set[str]:
        if isinstance(node, str):
            return {node}
        if isinstance(node, dict):
            return set(node) | {s for v in node.values() for s in strings(v)}
        if isinstance(node, list):
            return {s for v in node for s in strings(v)}
        return set()

    for headers in ({}, {"Authorization": f"Bearer {token}"}):
        body = client.get("/api/capabilities", headers=headers).json()
        unexpected = strings(body) - allowed
        assert not unexpected, (sorted(unexpected), headers)


def test_the_shell_chrome_is_never_offered_as_hideable(client):
    """/login and /dashboard are not gateable features. A detection bug that
    hides /reviews costs one page; the same bug on /login costs the
    installation, so those routes are simply not in the map."""
    pages = client.get("/api/capabilities").json()["pages"]

    for route in ("/login", "/dashboard", "/settings", "/onboarding"):
        assert route not in pages


def test_caching_headers_keep_one_callers_document_to_itself(client):
    r = client.get("/api/capabilities")

    assert r.headers["cache-control"] == "no-store"
    assert r.headers["vary"] == "Authorization"


# ─── failing safe, in the direction that is safe here ────────────────


def test_a_degraded_document_hides_nothing(app, monkeypatch):
    """The last line of defence. If detection breaks, the answer must be a
    document that makes no claims — not a 500 (a blank shell) and not a
    document full of `false` (every page hidden at once)."""
    monkeypatch.setattr(caps, "mounted_prefixes",
                        lambda _app: (_ for _ in ()).throw(RuntimeError("boom")))
    doc = caps.build_capabilities(app)

    assert doc.degraded is True
    assert doc.pages == {}
    assert doc.features == {}
    # The contract is "hide only on an explicit false", so an empty map hides
    # nothing and today's behaviour survives the outage.
    assert not [p for p, ok in doc.pages.items() if not ok]


def test_no_routes_discovered_is_also_degraded_rather_than_everything_off():
    """Discovering nothing is a detection failure, not an installation with no
    features — so it degrades rather than answering `false` to everything.

    The app has to be built with its docs disabled to reach this: a bare
    `FastAPI()` still carries /openapi.json, /docs and /redoc, which is why
    "did the walk return anything at all" is a usable emptiness check and
    "did it return few things" was not.
    """
    blank = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    assert not caps.mounted_prefixes(blank)

    doc = caps.build_capabilities(blank)
    assert doc.degraded is True
    assert doc.pages == {}


def test_a_missing_deployment_module_is_unavailable_not_a_crash(monkeypatch):
    """It was genuinely missing while this router was written, and a build may
    ship without it. `unavailable` is a different answer from `requires_auth`
    on purpose: one says "ask again with a token", the other "there is nothing
    to ask"."""
    monkeypatch.setitem(sys.modules, "src.deployment", None)

    assert caps._load_deployment() is None
    assert caps._deployment_mode() == ("unknown", "unavailable")
    assert caps._module_edition() is None


@pytest.mark.parametrize("value, expected", [
    ("single_tenant", "single_tenant"),
    ("Multi-Tenant", "multi-tenant"),
    ("  saas  ", "saas"),
    ("", None),
    ("x" * 64, None),                       # too long to be a mode
    ("/etc/passwd", None),                  # not a label
    ("mode\nInjected: header", None),
    (None, None),
    (123, None),
])
def test_a_mode_label_is_validated_before_it_is_published(value, expected):
    """`src/deployment.py` is a module this router has no ownership of, read
    through a guessed accessor. Whatever comes back is checked into a short
    opaque label or dropped — a value that was not a mode must not become one
    on the wire."""
    assert caps._coerce_label(value) == expected


def test_an_enum_mode_is_read_as_its_value_not_its_repr():
    """`get_mode()` returns an enum, not a string, and which kind is not this
    router's to depend on: it was `(str, Enum)` when this was written and is
    `StrEnum` now, and those disagree about what `str()` gives —
    "DeploymentMode.SINGLE_TENANT" versus "single_tenant". The label must be
    the value under either."""
    from src.deployment import DeploymentMode

    assert caps._coerce_label(DeploymentMode.SINGLE_TENANT) == "single_tenant"
    assert caps._coerce_label(DeploymentMode.MULTI_TENANT) == "multi_tenant"


def test_the_probe_never_touches_the_database_or_the_secret():
    """`src.deployment` also exposes `count_workspaces()` (a query) and
    `run_startup_checks()` (raises on a weak secret). Neither may be probed:
    this endpoint is on the anonymous first-paint path."""
    forbidden = {"count_workspaces", "run_startup_checks", "warn_if_multi_workspace"}

    assert not forbidden & set(caps._MODE_ACCESSORS + caps._EDITION_ACCESSORS)
