"""``GET /api/capabilities`` — which edition is this, and what is switched on.

WHY THIS EXISTS
---------------
There is no server-side edition gate, so the frontend has nothing to ask and
guesses instead: it renders every page in ``SECTION_TABS`` and lets the user
discover by 403 which of them this installation actually has. Worse,
``web/components/app-shell.tsx`` fetches on first paint and a missing module
takes down the whole shell rather than the one page that needed it. This
endpoint is the thing the shell can ask BEFORE it paints.

WHAT IT IS NOT
--------------
**This is not an authorization boundary.** A feature reported ``available``
says the routes are mounted in this process — not that the caller may use
them. Every real endpoint keeps its own 401/403, and that is still the only
thing standing between a caller and the data. If a future change starts
reading this document to decide access, the decision has moved to a payload
that is served unauthenticated, which is the opposite of where it belongs.

HOW "AVAILABLE" IS DECIDED
--------------------------
From the app's own route table, at request time — not from a constant listing
what we believe shipped. If ``main.py`` stops mounting a router (both the MCP
and the provider-webhook mounts are already inside ``try/except`` and can be
off in a perfectly healthy process), this answer changes with it and nobody
has to remember to edit a list.

What IS written down is the map from a product feature to the API prefixes
that back it and the client routes it gates — the part no amount of
introspection can infer, because "``/api/deps`` is the Dependencies page" is
product knowledge. That map is held honest from the other end: the route table
is the authority on what exists, and
``tests/api/test_capabilities_says_what_is_mounted.py`` fails both when a
prefix is mounted that no feature claims AND when a feature claims a prefix
that is not mounted. Drift becomes a red build on the commit that causes it
rather than a page that silently never appears.

FAILING SAFE, IN THE DIRECTION THAT IS ACTUALLY SAFE HERE
---------------------------------------------------------
``src/retrieval/vector_store.py`` settles the house rule: unknown fails towards
refusing. That rule governs **whose data a read may return**, and it applies
here by construction — this endpoint reads no tenant data at all, so it has
nothing to leak by getting the tenant wrong. It does not know who is asking
and deliberately cannot find out.

The advisory half is ranked the other way round, because the failure modes are
not symmetric. A capability wrongly reported ON costs a 403 the user already
gets today. A capability wrongly reported OFF hides a working page from a live
installation — the "correct change that locks them out" that outranks
everything. So the client contract is:

    hide a page only when this document explicitly says ``false``;
    no document, no answer, or a key you do not recognise means show it.

Which is why the endpoint's own last line of defence is to return a
``degraded`` document with **no** claims rather than a 500 or a document full
of ``false``: an empty ``pages`` map hides nothing and leaves today's behaviour
exactly as it is.

WHAT AN ANONYMOUS CALLER MAY SEE
--------------------------------
It is read before sign-in, so most of the body is public. Included for
everyone: the product name, the API version, a coarse edition label, and which
feature modules are mounted. That is an installation fingerprint, and it is a
weaker one than this app already hands out for free — ``/openapi.json`` and
``/docs`` are mounted BEHIND A SESSION as of the same change that added this file.
Nothing public here that is not already public there.

**The deployment mode is the exception, and it needs presenting a token.**
The brief called it part of the fingerprint. Reading ``src/deployment.py``
says otherwise: the mode is not a marketing label, it is the answer to
"do the five fall-open paths on this box fall open?" — a repository with no
access rule granting full code access, and an MCP caller with no bearer
identity being treated as a global admin, are two of the six sites it
governs. Publishing ``single_tenant`` to an anonymous caller is publishing
which unauthenticated attack to try first, next to a feature list confirming
``/mcp`` is mounted. It is discoverable by probing, so this is not a secret —
but it costs an attacker a probe and it costs us nothing to withhold, because
**every UI that needs the mode is behind a login already** (the workspace
switcher, the team pages). So a valid bearer token adds it and anything
else — absent, malformed, expired — gets ``mode: "unknown"`` with
``source: "requires_auth"``, which is a different and honest answer from
``source: "unavailable"`` (the module is not in this build at all).

Deliberately excluded, and each for a reason rather than a hunch:

* **Anything tenant-shaped** — no workspace names, no ids, and *not the count
  either*, at any authentication level. Cardinality is the disclosure: "this
  installation has one customer" is a fact about a business. Note that both
  ``_is_single_tenant()`` in ``routers/llm.py`` and
  ``deployment.count_workspaces()`` answer the mode question by counting rows;
  neither is called here. That also keeps a database query off the anonymous
  first-paint path, which is its own reason.
* **The build SHA** — pins the exact source an attacker is reading. It already
  has an authenticated home in ``/api/ops/*``; repeating it here would only
  widen the audience.
* **Configured endpoints, providers, model names, key presence** — deployment
  detail, and a map of what to attack next.
* **Why a probe failed** — exception text carries paths and versions. The
  answer is the boolean; the reason goes to the log.

Auth-method discovery is not here on purpose: the login page already reads
NextAuth's own ``/api/auth/providers``, and a second source for the same fact
is a fact that can disagree with itself.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["capabilities"])

#: Bump when a client would have to be changed to read the payload correctly.
SCHEMA_VERSION = 1


# ─── The one thing that is written down ──────────────────────────────
#
# A feature is available when EVERY prefix it needs is mounted. `all`, not
# `any`: half of Code review present is a page that loads and then fails on
# the second request, which is the failure this endpoint exists to prevent.


@dataclass(frozen=True)
class Feature:
    key: str
    #: Route prefixes that must be mounted for the feature to work.
    prefixes: tuple[str, ...]
    #: Client routes this feature gates. Empty for backend-only capabilities
    #: (inbound webhooks, push delivery) — real capabilities with no page.
    pages: tuple[str, ...] = ()
    #: Whether absence means "a smaller edition" rather than "a broken
    #: process". False for the shell's own infrastructure, and for the
    #: interactive API docs, which an operator may harden away without having
    #: bought anything less. Such a feature is still reported; it just does
    #: not drag the derived edition label down to "partial".
    counts_towards_edition: bool = True


FEATURES: tuple[Feature, ...] = (
    # Infrastructure. If any of this is missing the app is not serving.
    #
    # Deliberately NO pages, though /login, /dashboard and /settings are
    # obviously "core". A page listed here is a page this document can report
    # as false, and the shell's own chrome is precisely what must never be
    # hideable: a detection bug that hides /reviews costs one feature, the
    # same bug on /login costs the installation. The shell is not a gateable
    # feature, so it is not offered as one.
    Feature("core", ("/api/auth", "/api/capabilities", "/api/workspaces"), (),
            counts_towards_edition=False),
    Feature("health", ("/healthz", "/readyz", "/api/health"), (),
            counts_towards_edition=False),
    Feature("metrics", ("/metrics",), (), counts_towards_edition=False),
    # FastAPI's own Swagger. Reported so an operator can see from outside
    # whether the schema is reachable at all.
    #
    # It no longer describes an unauthenticated disclosure, and that changed
    # in the same hour this file was written: /openapi.json and /docs are
    # served behind a session now, and /redoc is gone entirely — it was not
    # even mounted deliberately, it arrived by having the webhook sub-app's
    # route list copied wholesale onto this app. Claiming a prefix that no
    # longer exists made this feature report itself missing, which is the
    # failure mode this file's own test was written to catch.
    Feature("api_explorer", ("/docs", "/openapi.json"), (),
            counts_towards_edition=False),

    # Product surfaces, each mapped to the client routes it gates.
    Feature("repositories", ("/api/repos", "/api/connections"),
            ("/repositories", "/connections")),
    Feature("code_review",
            ("/api/reviews", "/api/review-policies", "/api/agents", "/api/compliance"),
            ("/reviews", "/admin/review-policies", "/admin/agents",
             "/admin/compliance", "/admin/deprecations")),
    Feature("qa", ("/api/qa", "/api/projects", "/api/chats", "/api/search"),
            ("/projects", "/chats", "/search")),
    # The two `/docs` are unrelated and both correct: the PREFIX `/docs` above
    # is FastAPI's Swagger UI, the PAGE `/docs` here is the Next.js
    # documentation browser backed by `/api/docs`. Prefixes and pages are
    # different namespaces, so they cannot collide — but a reader deserves to
    # be told that once rather than work it out twice.
    Feature("docs", ("/api/docs",), ("/docs",)),
    Feature("dependencies", ("/api/deps",), ("/dependencies",)),
    Feature("intel", ("/api/intel",), ("/admin/intel",)),
    Feature("agent", ("/api/claude", "/api/agent-sessions"), ("/claude",)),
    Feature("automation", ("/api/automation",), ("/automation",)),
    Feature("monitoring", ("/api/alerts", "/api/notifications"),
            ("/alerts", "/admin/notifications")),
    Feature("jobs", ("/api/jobs",), ("/admin/jobs",)),
    Feature("audit", ("/api/audit",), ("/admin/audit",)),
    Feature("ops", ("/api/ops",), ("/admin/health", "/admin/logs")),
    Feature("billing", ("/api/usage", "/api/spend"), ("/admin/usage",)),
    Feature("team", ("/api/teams", "/api/access", "/api/users", "/api/invites"),
            ("/admin/workspaces", "/admin/teams", "/admin/access")),
    Feature("llm_config", ("/api/llm", "/api/models", "/api/vector-store"),
            ("/settings/llm", "/settings/models")),
    Feature("gdpr", ("/api/gdpr",), ("/admin/gdpr",)),

    # Backend-only capabilities: no page of their own, but a client that knows
    # they are off can say so instead of offering a setup flow that dead-ends.
    Feature("mcp", ("/api/mcp", "/mcp"), ("/settings/mcp",)),
    Feature("oauth_provider", ("/oauth", "/.well-known"), ("/admin/oauth-clients",)),
    # The receiver and the setup endpoint that tells you where to point the
    # provider are one feature: a mounted receiver nobody can find the URL of
    # is the "looked broken rather than unconfigured" state routers/webhooks.py
    # was written to end.
    Feature("inbound_webhooks", ("/webhook", "/api/webhooks")),
    Feature("push", ("/api/push",)),
    Feature("feedback", ("/api/feedback",)),
)


# ─── Reading the route table ─────────────────────────────────────────


def _walk(routes: Any, depth: int = 0) -> set[str]:
    """Every path registered on `routes`, however the version nests them.

    Written generically after the obvious version bit: on FastAPI 0.141 an
    `include_router` produces a private `_IncludedRouter` wrapper that carries
    NO `.path` and hides its children behind `.original_router`. The naive
    `for r in app.routes: r.path` finds 61 objects of which 41 are opaque —
    it does not raise, it just quietly reports that almost nothing is mounted,
    and every optional feature would have been declared missing.
    """
    found: set[str] = set()
    if depth > 8:  # cycles are not expected; recursion that never ends is worse
        return found
    for route in routes or ():
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            # A leaf, and that includes a Mount: `Mount("/mcp", app)` exposes
            # the sub-app's routes under `.routes`, but their paths are
            # relative to the mount. Descending would read the MCP server's
            # own `/.well-known/...` as a TOP-LEVEL prefix of this API and
            # report a feature that is not there. The mount point is the fact
            # worth having, so recursion stops where a path appears.
            found.add(path)
            continue
        inner = getattr(route, "routes", None)
        if inner is None:
            inner = getattr(getattr(route, "original_router", None), "routes", None)
        if inner is not None:
            found |= _walk(inner, depth + 1)
    return found


def _paths(app: Any) -> set[str]:
    """Registered paths, read two independent ways and UNIONed.

    Neither source is complete on its own, and each covers the other's gap:

    * `_walk` sees mounts and `include_in_schema=False` routes, but reads
      `original_router`, which is private and one FastAPI rename from
      returning almost nothing.
    * `app.openapi()` is public API and cannot be broken by that rename, but
      it omits `Mount`s entirely (`/mcp` is invisible to it) and skips routes
      excluded from the schema.

    Union rather than "prefer one, fall back to the other", because a fallback
    needs a rule for when to fire and the obvious rule — "the walk looks too
    short" — cannot tell a broken traversal from a genuinely small app. It
    fired on a three-router installation and dropped its mounts. A union needs
    no such judgement: both sources only ever report routes that really are
    registered, so adding them together can gain a truth and cannot invent one.

    The OpenAPI schema is memoised by FastAPI on first call, so the cost lands
    on one request per process rather than on every anonymous first paint.
    """
    found: set[str] = set()
    try:
        found |= _walk(getattr(app, "routes", ()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("capabilities_route_walk_failed err=%s", exc)
    try:
        found |= set(app.openapi().get("paths", {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("capabilities_openapi_read_failed err=%s", exc)
    return found


def mounted_prefixes(app: Any) -> set[str]:
    """The first one or two path segments of everything mounted.

    ``/api/repos/{slug}/pulls`` → ``/api/repos``; ``/healthz`` → ``/healthz``;
    ``/.well-known/oauth-protected-resource`` → ``/.well-known``. This is the
    granularity `FEATURES` claims at, and the granularity the coverage test
    checks, so both sides read the same function rather than two spellings of
    the same regex.
    """
    prefixes: set[str] = set()
    for path in _paths(app):
        if not path.startswith("/"):
            continue
        segments = [s for s in path.split("/") if s and "{" not in s]
        if not segments:
            continue
        head = f"/{segments[0]}"
        # Only /api is a namespace rather than a feature; everything else
        # (/oauth, /webhook, /mcp, /healthz) is already named at its first
        # segment. Bare "/api" is never itself a feature, so it is not emitted
        # — it would be an unclaimable prefix that the coverage test could
        # only be satisfied by adding a meaningless entry to FEATURES.
        if head == "/api" and len(segments) > 1:
            prefixes.add(f"/api/{segments[1]}")
        else:
            prefixes.add(head)
    return prefixes


# ─── Deployment mode, from a module that may not exist yet ───────────

#: A mode is a short opaque label. Anything longer or stranger is either not a
#: mode or is carrying detail we did not agree to publish, so it is dropped
#: rather than forwarded — this endpoint is anonymous and the module is one we
#: have never read.
_MODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

#: Tried in order; the first that yields a usable string wins. `src.deployment`
#: landed while this was being written and the accessor turned out to be
#: `get_mode()` returning a `DeploymentMode` str-Enum — hence `_coerce_label`
#: reading `.value`. The rest of the list stays: it costs nothing, and every
#: miss lands on "unknown", which is a true statement and breaks nothing.
#:
#: Only cheap, side-effect-free names are probed. `count_workspaces()` and
#: `run_startup_checks()` are in that module too and are deliberately NOT
#: here — one queries the database and the other can raise on a bad secret.
_MODE_ACCESSORS = (
    "current_mode", "deployment_mode", "get_mode", "mode",
    "MODE", "DEPLOYMENT_MODE",
)
_EDITION_ACCESSORS = ("edition", "current_edition", "get_edition", "EDITION")


def _coerce_label(value: Any) -> str | None:
    """A safe short label, or None. Accepts a str, an Enum, or a `.value`."""
    for candidate in (value, getattr(value, "value", None), getattr(value, "name", None)):
        if isinstance(candidate, str):
            text = candidate.strip().lower().replace(" ", "_")
            if _MODE_RE.match(text):
                return text
    return None


def _read_label(module: Any, names: tuple[str, ...]) -> str | None:
    for name in names:
        attribute = getattr(module, name, None)
        if attribute is None:
            continue
        try:
            value = attribute() if callable(attribute) else attribute
        except Exception as exc:  # noqa: BLE001
            logger.warning("capabilities_deployment_accessor_failed name=%s err=%s",
                           name, exc)
            continue
        label = _coerce_label(value)
        if label is not None:
            return label
    return None


def _is_authenticated(authorization: str | None) -> bool:
    """Does the caller hold a token this installation signed?

    A deliberately low bar — signature and expiry only, no user lookup. It
    gates one short string, not data, and putting a user-store read on the
    anonymous first-paint path would cost more than the string is worth.

    Every failure returns False: an unreadable token is not a token. Unknown
    fails towards refusing, which here means "you get the public document".
    """
    try:
        if not authorization or not authorization.lower().startswith("bearer "):
            return False
        from src.api.jwt_auth import decode_token

        return bool(decode_token(authorization.split(" ", 1)[1].strip()))
    except Exception:  # noqa: BLE001 — any failure to verify is a failure to verify
        return False


def _load_deployment() -> Any | None:
    """`src.deployment`, or None when this build ships without it.

    Imported inside the call, not at module scope: `src/deployment.py` did not
    exist while this was being written, and a top-level import of a missing
    module would take the whole API down at startup rather than degrade one
    field of one response.

    `import_module` rather than `from src import deployment`, because the
    latter is answered by an attribute already set on the `src` package once
    anything else has imported it — which makes "this build ships without the
    module" a state that cannot be reached, including by the test for it.
    """
    try:
        import importlib

        return importlib.import_module("src.deployment")
    except Exception:  # noqa: BLE001 — not present in this build; that is a valid state
        return None


def _deployment_mode() -> tuple[str, str]:
    """``(mode, source)`` — the auth-gated half. Callers must already have
    decided the requester may know; see :func:`capabilities`."""
    deployment = _load_deployment()
    if deployment is None:
        return "unknown", "unavailable"
    mode = _read_label(deployment, _MODE_ACCESSORS)
    if mode is None:
        logger.warning(
            "capabilities_deployment_mode_unreadable attrs=%s — reporting unknown",
            [n for n in _MODE_ACCESSORS if hasattr(deployment, n)],
        )
        return "unknown", "src.deployment"
    return mode, "src.deployment"


def _module_edition() -> str | None:
    """An edition name owned by `src.deployment`, if it has one.

    Read for everyone, unlike the mode. An edition is a product label — the
    installation fingerprint this endpoint is allowed to be — whereas the mode
    describes the tenancy posture. Splitting them is what stops the same
    installation from naming two different editions depending on who asked,
    which is what happened while the edition was read only as a by-product of
    the auth-gated mode lookup.
    """
    deployment = _load_deployment()
    return None if deployment is None else _read_label(deployment, _EDITION_ACCESSORS)


# ─── The payload ─────────────────────────────────────────────────────


class FeatureOut(BaseModel):
    available: bool
    #: Client routes this feature gates; empty for backend-only capabilities.
    pages: list[str] = []


class DeploymentOut(BaseModel):
    #: Short label owned by `src.deployment` ("single_tenant"/"multi_tenant"
    #: today); "unknown" whenever `source` is not "src.deployment".
    mode: str
    #: Why `mode` says what it says — "src.deployment" (read it),
    #: "requires_auth" (present a token and ask again), or "unavailable" (the
    #: module is not in this build). Three answers, because "unknown" alone
    #: cannot tell a client whether retrying with credentials would help.
    source: str


class CapabilitiesOut(BaseModel):
    schema_version: int
    product: str
    api_version: str
    edition: str
    edition_source: str
    deployment: DeploymentOut
    features: dict[str, FeatureOut]
    #: Flattened client route → is it backed by a mounted feature. The one-line
    #: read for a navigation filter. Hide ONLY on an explicit false.
    pages: dict[str, bool]
    #: True when detection itself failed. `features`/`pages` are then empty,
    #: which under the contract above hides nothing.
    degraded: bool = False


def _empty(reason: str) -> CapabilitiesOut:
    logger.error("capabilities_degraded reason=%s", reason)
    return CapabilitiesOut(
        schema_version=SCHEMA_VERSION, product="celmis", api_version="unknown",
        edition="unknown", edition_source="degraded",
        deployment=DeploymentOut(mode="unknown", source="unavailable"),
        features={}, pages={}, degraded=True,
    )


def build_capabilities(app: Any, *, authenticated: bool = False) -> CapabilitiesOut:
    """The document for `app`. Importable so the coverage test can read the
    same answer the endpoint serves rather than a re-implementation of it.

    `authenticated` adds the deployment mode and nothing else — see
    :func:`capabilities`. It defaults to the anonymous document, so a future
    caller that forgets the argument under-discloses rather than over-.
    """
    try:
        prefixes = mounted_prefixes(app)
        if not prefixes:
            return _empty("no routes discovered")

        features = {
            f.key: FeatureOut(
                available=all(p in prefixes for p in f.prefixes),
                pages=list(f.pages),
            )
            for f in FEATURES
        }

        # A page is offered when SOME available feature claims it. Two features
        # sharing a page (none do today) must not have the quieter one win.
        pages: dict[str, bool] = {}
        for f in FEATURES:
            for page in f.pages:
                pages[page] = pages.get(page, False) or features[f.key].available

        # The mode names whether the fall-open paths fall open — withheld from
        # anonymous callers. `_deployment_mode()` is not even called for them,
        # so there is no branch where the value exists and merely fails to be
        # printed. The edition is not gated: it is a product label, which is
        # exactly the fingerprint this endpoint is allowed to be.
        mode, mode_source = (
            _deployment_mode() if authenticated else ("unknown", "requires_auth")
        )
        module_edition = _module_edition()

        # Derived, so it cannot claim a completeness the build does not have:
        # "full" only when every OPTIONAL feature is mounted. A name from
        # `src.deployment` wins — it knows what was actually sold.
        if module_edition:
            edition, edition_source = module_edition, "src.deployment"
        else:
            missing = [f.key for f in FEATURES
                       if f.counts_towards_edition and not features[f.key].available]
            edition = "full" if not missing else "partial"
            edition_source = "derived"
            if missing:
                logger.info("capabilities_edition_partial missing=%s", missing)

        return CapabilitiesOut(
            schema_version=SCHEMA_VERSION,
            product="celmis",
            api_version=str(getattr(app, "version", "") or "unknown"),
            edition=edition,
            edition_source=edition_source,
            deployment=DeploymentOut(mode=mode, source=mode_source),
            features=features,
            pages=pages,
        )
    except Exception as exc:  # noqa: BLE001
        # The shell fetches this before it paints; a 500 here is a blank app,
        # not a missing menu entry. There is no failure worth that.
        logger.exception("capabilities_build_failed err=%s", exc)
        return _empty("build raised")


@router.get("/capabilities", response_model=CapabilitiesOut)
def capabilities(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> CapabilitiesOut:
    """Which edition this is and what is switched on.

    **Answers without credentials** — it is read before sign-in — and a bearer
    token is optional rather than accepted-or-401: presenting a valid one adds
    the deployment mode, presenting none or a bad one still returns the public
    document. This must never 401, because a shell that cannot read it is a
    shell that cannot decide what to paint.

    Advisory only: it never grants access, and a client must hide a page only
    on an explicit `false`.
    """
    # `no-store` because a copy that outlives a deploy would hide a page that
    # just arrived; `Vary` because the body does depend on Authorization and a
    # shared cache must not hand one caller's document to the next.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    return build_capabilities(request.app, authenticated=_is_authenticated(authorization))


__all__ = [
    "FEATURES",
    "SCHEMA_VERSION",
    "CapabilitiesOut",
    "Feature",
    "build_capabilities",
    "mounted_prefixes",
    "router",
]
