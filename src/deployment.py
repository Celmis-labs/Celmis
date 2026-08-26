"""What this installation promises — tenancy and egress — decided once, at startup.

Five places in this codebase answer "I don't know whose this is" with "then
everyone may": a repository with no access rule is readable by every member,
an MCP call with no auth context is a global admin, a budget whose row cannot
be read is unlimited, a user whose workspace cannot be provisioned lands in
the shared ``default`` tenant. Every one of them is *load-bearing* for a
single-tenant install — a box with one team on it and no rules configured
would be unusable if those paths refused — and every one of them is a
disclosure on a box with three tenants on it.

The two cannot both be the default, so they stop being a default at all:

    single_tenant  (the default, today's behaviour, byte for byte)
        The fall-open paths fall open. An install that upgrades into this
        module notices nothing, which is the point — production runs three
        workspaces with almost no access rules, and flipping the meaning of
        "no rule" under it would lock the operator out of their own product.

    multi_tenant   (opt-in, ``CELMIS_DEPLOYMENT_MODE=multi_tenant``)
        Every one of those five refuses instead. Unknown ownership stops
        being an argument for access.

The switch belongs to the operator, so the only thing this module does
uninvited is *say something*: :func:`warn_if_multi_workspace` names the risk
in one line at startup when an installation has grown past one workspace
while still running in single_tenant.

Why a mode and not a per-site setting: the sites are one decision wearing
five hats. An operator who can turn four of them on is an operator who ships
the fifth open, and the one they forget is the one that leaks.

Unknown values do NOT fall back to single_tenant. A typo in a tenancy switch
that silently means "open" is the exact failure this module exists to remove
(same reasoning as ``embedding_provider`` in src/config.py, which refuses to
start on a misspelt provider rather than quietly using the default).

The second promise: egress
--------------------------
Same shape, same reason. ``CELMIS_EGRESS_MODE`` chooses between

    permissive  (the default, today's behaviour, byte for byte)
        HTTP call sites that build their own ``httpx.Client`` — bypassing
        the allowlist in src/security/egress.py — are allowed to exist.
        :data:`UNGUARDED_HTTP_SITES` lists every one of them.

    strict      (opt-in, ``CELMIS_EGRESS_MODE=strict``)
        The process refuses to start while that list is non-empty.

It is a registry rather than a runtime check because the property is about
source, not about traffic: a client that is never used still cannot be
proven not to be. The registry is kept honest by an AST scan in
tests/security/test_every_http_client_goes_through_the_factory.py, which
fails in both directions — a new raw client that is not listed, and a listed
one that has since been converted.

Read :data:`STRICT_EGRESS_CAVEAT` before quoting strict mode to anyone. It is
a claim about this repository's own HTTP calls and nothing else.
"""

from __future__ import annotations

import logging
import os
import threading
from enum import StrEnum

logger = logging.getLogger(__name__)

#: The one environment variable. Read once; see :func:`get_mode`.
ENV_VAR = "CELMIS_DEPLOYMENT_MODE"


class DeploymentMode(StrEnum):
    """How many tenants this installation admits to holding."""

    SINGLE_TENANT = "single_tenant"
    MULTI_TENANT = "multi_tenant"


#: Unset env → this. Chosen so that an existing install changes nothing.
DEFAULT_MODE = DeploymentMode.SINGLE_TENANT

#: Spellings accepted for each mode, so a hyphen or a stray capital is not a
#: boot failure. Anything outside this table IS one.
_ALIASES: dict[str, DeploymentMode] = {
    "single_tenant": DeploymentMode.SINGLE_TENANT,
    "single-tenant": DeploymentMode.SINGLE_TENANT,
    "singletenant": DeploymentMode.SINGLE_TENANT,
    "single": DeploymentMode.SINGLE_TENANT,
    "multi_tenant": DeploymentMode.MULTI_TENANT,
    "multi-tenant": DeploymentMode.MULTI_TENANT,
    "multitenant": DeploymentMode.MULTI_TENANT,
    "multi": DeploymentMode.MULTI_TENANT,
}

#: The fall-open sites this mode governs: id → what falls open there. The ids
#: are passed to :func:`fall_open_allowed` and are what the guard test
#: (tests/security/test_every_fall_open_asks_the_deployment_mode.py)
#: enumerates, so adding a site here without guarding it fails that test.
FALL_OPEN_SITES: dict[str, str] = {
    "api.deps.repo_permission":
        "a repository with no team grant is readable/writable by any user",
    "api.deps.workspace_provision":
        "a user whose own workspace cannot be provisioned lands in 'default'",
    "access.resolver.no_rule":
        "a repository with no access rule grants full code access",
    "mcp.identity.no_auth_context":
        "an MCP caller with no bearer identity is a global admin",
    "mcp.identity.unauthenticated_access":
        "that same caller is handed full research access to every repo",
    "llm.budget.unreadable":
        "a budget row that cannot be read counts as no cap at all",
}


class DeploymentModeError(RuntimeError):
    """The configured deployment mode is not one this build understands."""


_lock = threading.Lock()
_resolved: DeploymentMode | None = None


def parse_mode(raw: str | None) -> DeploymentMode:
    """Mode named by ``raw``. Empty/None → :data:`DEFAULT_MODE`.

    Raises :class:`DeploymentModeError` on anything else: a value that was
    *meant* to be a tenancy setting and is not one must not resolve to the
    permissive side by accident.
    """
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_MODE
    mode = _ALIASES.get(text)
    if mode is None:
        raise DeploymentModeError(
            f"{ENV_VAR}={raw!r} is not a deployment mode. Use "
            f"'{DeploymentMode.SINGLE_TENANT.value}' (default — today's "
            f"behaviour) or '{DeploymentMode.MULTI_TENANT.value}' (every "
            f"unknown-ownership path refuses)."
        )
    return mode


def get_mode() -> DeploymentMode:
    """The mode this process runs in — read from the environment once.

    Cached deliberately: the answer must be the same for every request of a
    process's life, so a half-applied change cannot leave one code path open
    and another closed.
    """
    global _resolved
    if _resolved is not None:
        return _resolved
    with _lock:
        if _resolved is None:
            _resolved = parse_mode(os.environ.get(ENV_VAR))
            logger.info("deployment_mode=%s", _resolved.value)
        return _resolved


def reset_mode_cache() -> None:
    """Forget every mode this module resolved — tenancy and egress both.

    One function for both because a test that changes the environment and
    resets half of it gets an answer from the previous test. For tests and
    for a deliberate re-read only.
    """
    global _resolved, _egress_resolved
    with _lock:
        _resolved = None
        _egress_resolved = None


def is_single_tenant() -> bool:
    return get_mode() is DeploymentMode.SINGLE_TENANT


def is_multi_tenant() -> bool:
    return get_mode() is DeploymentMode.MULTI_TENANT


def fall_open_allowed(site: str, *, detail: str = "") -> bool:
    """May this fall-open path grant access because ownership is unknown?

    ``True`` only under single_tenant. This is the single predicate every one
    of the five sites asks, so "which mode am I in" is decided in one place
    and the sites cannot drift apart.

    The refusal is logged at WARNING and the permission at DEBUG on purpose:
    on an install with no rules configured the allowed branch is every
    request, and a log line per request is a log nobody reads.
    """
    if site not in FALL_OPEN_SITES:
        logger.warning("fall_open_unregistered_site site=%s", site)
    if get_mode() is DeploymentMode.SINGLE_TENANT:
        logger.debug("fall_open site=%s %s", site, detail)
        return True
    logger.warning(
        "fall_open_refused site=%s mode=multi_tenant %s — %s",
        site, detail, FALL_OPEN_SITES.get(site, "unknown ownership"),
    )
    return False


# ─── egress: does every HTTP call go through the allowlist? ──────────


#: The other environment variable. Same read-once discipline as ENV_VAR.
EGRESS_ENV_VAR = "CELMIS_EGRESS_MODE"


class EgressMode(StrEnum):
    """Whether an unguarded HTTP client is a fact of life or a boot failure."""

    PERMISSIVE = "permissive"
    STRICT = "strict"


#: Unset env → this. An upgrade into this module must change nothing.
DEFAULT_EGRESS_MODE = EgressMode.PERMISSIVE

#: Two spellings, and deliberately no "on"/"off" pair: `EGRESS_MODE=off` reads
#: as "no egress" to half the people who type it and as "guard disabled" to
#: the other half, and the half that guesses wrong gets the permissive side.
#: Case and surrounding whitespace are forgiven; nothing else is.
_EGRESS_ALIASES: dict[str, EgressMode] = {
    "permissive": EgressMode.PERMISSIVE,
    "strict": EgressMode.STRICT,
}

#: Every module under src/ that builds its own ``httpx.Client`` — or calls a
#: module-level verb like ``httpx.get(...)``, which constructs an ephemeral
#: unguarded client inside httpx on every call — instead of going through
#: :func:`src.http.build_client`, with the reason it stays raw.
#:
#: This began as a work list of 23 modules. The conversion wave emptied it of
#: everything convertible: sites reaching hosts on the shipped public
#: allowlist became a plain ``build_client(...)``, and sites reaching
#: operator-configured infrastructure (the LiteLLM proxy, the sandbox
#: service, a self-hosted GitLab, the constant LLM key-check endpoints) name
#: their one host via ``extra_allowed_hosts``, derived from the same config
#: or constant the request is built from. What remains is not a TODO pile but
#: the sites where conversion is WRONG: their destination host is itself
#: tenant data, and src/http.py's rule is that ``extra_allowed_hosts`` must
#: never come from user input — an allowlist fed from the row that also names
#: the destination guards nothing, and would even wave private addresses
#: through by name, past the transport's own SSRF refusal.
#:
#: The guard test still asserts this dict matches the AST scan exactly, in
#: both directions, so a new raw client cannot appear unlisted and a
#: converted one cannot stay listed.
#:
#: Only two files may build a client without appearing here — src/http.py and
#: src/security/egress.py, which are where the guarded client comes from.
UNGUARDED_HTTP_SITES: dict[str, str] = {
    "src/mcp_client/registry.py":
        "permanent until MCP hosts become operator config: tools/list and "
        "tools/call go to whatever URL a tenant wrote into "
        "RepoReviewPolicy.mcp_sources, so there is no config-owned host to "
        "put in extra_allowed_hosts — deriving one from the row itself would "
        "make the allowlist decorative",
    "src/notifications/dispatch.py":
        "permanent for the same reason: the webhook/Slack/Discord POST goes "
        "to the URL each workspace typed into its channel config — the "
        "tenant-supplied-destination shape again, where a host exception "
        "derived from the destination guards nothing",
}

#: What strict mode does NOT promise. Verified against the pinned versions in
#: this venv (google-genai 1.75.0, aiohttp installed), not assumed:
#:
#:   * google-genai builds its own client. The SYNC path is an httpx.Client
#:     and could be covered by handing it our transport through
#:     ``HttpOptions.client_args``; we do not do that yet. The ASYNC path —
#:     which is the one src/api/routers/qa.py streams through — uses aiohttp
#:     whenever aiohttp is importable, and it is, so no httpx transport can
#:     see it at all. On Vertex/ADC credentials the SDK uses google-auth's
#:     AuthorizedSession (requests), which is a third stack again.
#:   * the litellm SDK, called in-process from src/llm/completion.py and
#:     src/llm/client.py, makes its own clients outside src/ where the guard
#:     test cannot look.
#:   * qdrant-client likewise.
#:
#: So: strict mode means "no code in this repository opens an HTTP client
#: that skips the allowlist". It does not mean the process cannot talk to the
#: internet. The only statement that covers the SDKs is an OS-level firewall
#: (pf / Little Snitch / a network policy), and that is the one to put in
#: front of a customer who asks for a guarantee.
STRICT_EGRESS_CAVEAT = (
    "strict egress covers this repository's own httpx calls only: "
    "google-genai (aiohttp on the async path, google-auth on Vertex), the "
    "in-process litellm SDK and qdrant-client each carry their own HTTP "
    "stack and cannot be seen by an httpx transport. A Gemini install "
    "therefore cannot claim strict egress for completions. The only complete "
    "control is an OS-level firewall."
)


class EgressModeError(RuntimeError):
    """The configured egress mode is not one this build understands, or the
    build cannot honour the mode it was given."""


_egress_resolved: EgressMode | None = None


def parse_egress_mode(raw: str | None) -> EgressMode:
    """Mode named by ``raw``. Empty/None → :data:`DEFAULT_EGRESS_MODE`.

    Refuses anything else, for the reason :func:`parse_mode` does: a misspelt
    security switch must not resolve to the permissive side.
    """
    text = (raw or "").strip().lower()
    if not text:
        return DEFAULT_EGRESS_MODE
    mode = _EGRESS_ALIASES.get(text)
    if mode is None:
        raise EgressModeError(
            f"{EGRESS_ENV_VAR}={raw!r} is not an egress mode. Use "
            f"'{EgressMode.PERMISSIVE.value}' (default — today's behaviour) "
            f"or '{EgressMode.STRICT.value}' (refuse to start while any HTTP "
            f"call site bypasses the allowlist)."
        )
    return mode


def get_egress_mode() -> EgressMode:
    """The egress mode this process runs in — read from the environment once."""
    global _egress_resolved
    if _egress_resolved is not None:
        return _egress_resolved
    with _lock:
        if _egress_resolved is None:
            _egress_resolved = parse_egress_mode(os.environ.get(EGRESS_ENV_VAR))
            logger.info("egress_mode=%s", _egress_resolved.value)
        return _egress_resolved


def is_strict_egress() -> bool:
    return get_egress_mode() is EgressMode.STRICT


def assert_egress_guarded() -> None:
    """Refuse to run in strict mode while an unguarded HTTP client exists.

    No-op under permissive, which is the default: this must be something an
    operator turns on, never something an upgrade turns on for them.

    Read :data:`STRICT_EGRESS_CAVEAT` — passing this check is a statement
    about src/, not about the process.
    """
    if get_egress_mode() is not EgressMode.STRICT:
        return
    if not UNGUARDED_HTTP_SITES:
        return
    listed = "\n".join(
        f"  {path} — {why}" for path, why in sorted(UNGUARDED_HTTP_SITES.items())
    )
    raise EgressModeError(
        f"{EGRESS_ENV_VAR}=strict, but {len(UNGUARDED_HTTP_SITES)} module(s) "
        f"still build an httpx client that skips the egress allowlist:\n"
        f"{listed}\n"
        f"Convert them to src.http.build_client, or run with "
        f"{EGRESS_ENV_VAR}={EgressMode.PERMISSIVE.value}. Note: "
        f"{STRICT_EGRESS_CAVEAT}"
    )


# ─── startup checks ──────────────────────────────────────────────────


def count_workspaces() -> int | None:
    """How many workspaces exist, or ``None`` if that cannot be answered.

    Never raises: this runs during startup, and a database that is not up yet
    must not stop the API from booting.
    """
    try:
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session

        from src.access.resolver import _sync_engine
        from src.db.models import Workspace

        with Session(_sync_engine()) as s:
            return int(s.execute(select(func.count()).select_from(Workspace)).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace_count_unavailable err=%s", exc)
        return None


def warn_if_multi_workspace() -> str | None:
    """Say the quiet part at startup: this box holds more than one tenant and
    is running in the mode where unknown ownership means access.

    Returns the message logged, or ``None`` when there is nothing to say. A
    mode nobody notices is a mode nobody sets.
    """
    if get_mode() is not DeploymentMode.SINGLE_TENANT:
        return None
    n = count_workspaces()
    if n is None or n <= 1:
        return None
    msg = (
        f"deployment_mode_risk workspaces={n} mode=single_tenant — this "
        f"installation holds {n} workspaces while running single_tenant, "
        f"where a repository with no access rule, an MCP call with no bearer "
        f"identity and a budget row that cannot be read all resolve to FULL "
        f"access across tenants; set {ENV_VAR}=multi_tenant to make those "
        f"paths refuse instead."
    )
    logger.warning("%s", msg)
    return msg


def assert_credential_key_usable() -> None:
    """A Fernet key that is the wrong SHAPE must stop the boot, not the request.

    `openssl rand -hex 32` is 32 bytes of entropy and the obvious thing to
    reach for — it is what every other secret here wants. Fernet does not take
    it: it needs those 32 bytes url-safe-base64 encoded (44 chars), and it only
    says so when something first tries to decrypt, which is a request, not a
    boot. The operator sees `Failed to fetch` in the UI and has no path from
    that to the key format.

    So it is checked here, once, with the fix in the message.
    """
    from src.config import get_settings

    raw = (getattr(get_settings(), "credential_master_key", "") or "").strip()
    if not raw:
        return  # absent is a separate, already-handled story
    try:
        from cryptography.fernet import Fernet

        Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as exc:  # noqa: BLE001 — any shape problem is the same problem
        raise RuntimeError(
            "CREDENTIAL_MASTER_KEY is not a usable Fernet key "
            f"({type(exc).__name__}). It must be 32 bytes in url-safe base64 "
            "(44 characters) — NOT hex. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"\n'
            "Note: a NEW value does not reset stored credentials, it makes them "
            "permanently undecryptable."
        ) from exc


def warn_if_sandbox_is_unusable() -> str | None:
    """Say, at startup, that the sandbox will refuse every job — or nothing.

    SANDBOX_TOKEN is the shared secret between the api and the sandbox
    container, and the sandbox exits rather than start without it: "every
    request would be accepted, and this process runs commands". It used to
    exist in `.env.example` as a COMMENT ONLY, so `scripts/init-env.sh` — which
    takes its list from that file — could not generate it. The documented
    one-line setup reported success, and the failure surfaced later, in a
    container nobody was watching.

    Deliberately a WARNING here and `${SANDBOX_TOKEN:-}` in compose, not
    `:?`. Compose resolves interpolation for the whole file before starting
    anything, so a required-variable marker on an optional feature takes the
    database down with it — which is the blast radius
    tests/security/test_sandbox_deploy_path.py exists to prevent. The api
    serves indexing, Q&A and review perfectly well without a sandbox; only
    the jobs that run commands are lost, and this line says which.
    """
    import os

    if os.environ.get("SANDBOX_TOKEN", "").strip():
        return None
    message = (
        "SANDBOX_TOKEN is not set — the sandbox container will refuse to "
        "start, so agent sessions and apply-fix will fail. Everything else "
        "works. Run ./scripts/init-env.sh to generate one."
    )
    logger.warning("sandbox_token_missing — %s", message)
    return message


def run_startup_checks(*, strict_secret: bool = True) -> dict[str, object]:
    """Everything this module wants said or refused before serving traffic.

    Call once from the API startup hook. Returns a small report (handy for
    /readyz and for tests); raises only when the process must not serve:
    an unrecognised mode, a JWT signing secret that is a shipped placeholder,
    or strict egress asked for on a build that cannot deliver it.
    """
    mode = get_mode()
    report: dict[str, object] = {"mode": mode.value}
    assert_credential_key_usable()
    if strict_secret:
        from src.api.jwt_auth import assert_secret_usable

        assert_secret_usable()
        report["jwt_secret"] = "ok"
    egress = get_egress_mode()
    report["egress_mode"] = egress.value
    report["unguarded_http_sites"] = len(UNGUARDED_HTTP_SITES)
    assert_egress_guarded()
    if egress is EgressMode.STRICT:
        # Said at startup, every start, because the operator who set strict is
        # the one about to repeat the promise to someone else.
        logger.warning("egress_mode=strict caveat — %s", STRICT_EGRESS_CAVEAT)
        report["egress_caveat"] = STRICT_EGRESS_CAVEAT
    sandbox = warn_if_sandbox_is_unusable()
    if sandbox:
        report["sandbox"] = sandbox
    warning = warn_if_multi_workspace()
    if warning:
        report["warning"] = warning
    report["workspaces"] = count_workspaces()
    return report


__all__ = [
    "DEFAULT_EGRESS_MODE",
    "DEFAULT_MODE",
    "EGRESS_ENV_VAR",
    "ENV_VAR",
    "FALL_OPEN_SITES",
    "STRICT_EGRESS_CAVEAT",
    "UNGUARDED_HTTP_SITES",
    "warn_if_sandbox_is_unusable",
    "DeploymentMode",
    "DeploymentModeError",
    "EgressMode",
    "EgressModeError",
    "assert_egress_guarded",
    "count_workspaces",
    "fall_open_allowed",
    "get_egress_mode",
    "get_mode",
    "is_multi_tenant",
    "is_single_tenant",
    "is_strict_egress",
    "parse_egress_mode",
    "parse_mode",
    "reset_mode_cache",
    "run_startup_checks",
    "warn_if_multi_workspace",
]
