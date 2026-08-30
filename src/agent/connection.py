"""Claude subscription connection — who is signed in, with whose token, and
whether Anthropic still accepts it.

Cursor-style auth: the user runs `claude setup-token` once on their own
machine and pastes the long-lived OAuth token (sk-ant-oat…). We store it
Fernet-encrypted in the credential store under provider="claude_code":

    personal slot   user_id = <user.id>      — the member's own subscription
    workspace slot  user_id = ws:{ws_id}     — admin-shared for the workspace

Resolution: personal first, then workspace. The workspace slot is explicit
opt-in in the UI (sharing one person's subscription across members may violate
Anthropic's consumer terms — the UI says so before saving).

Verifying the token — and why the probe looks like this
------------------------------------------------------
A save used to be gated by `token_looks_valid` alone: a prefix and a length.
Nobody asked Anthropic, and `status()` reported presence only, so an expired,
revoked or copy-truncated token stored as "connected" and the operator found
out at the first review. That is the fourth setting in this project that
looked saved and did nothing, so the probe below was established from the tree
rather than from documentation:

  * `src/agent/runner.py:_build_options` hands the token to the CLI subprocess
    as the CLAUDE_CODE_OAUTH_TOKEN environment variable.
  * The installed claude-agent-sdk (0.2.139) never makes a request with it.
    The only mention of the variable in the whole package is
    `_internal/session_resume.py`, which reads it to decide whether it still
    has to seed the subprocess's credentials file from the macOS Keychain. So
    the SDK is not the thing to replicate — the CLI it spawns is.
  * In the CLI binary (2.1.235) that variable IS an access token: the login
    flow assigns `process.env.CLAUDE_CODE_OAUTH_TOKEN = e.accessToken`, and
    every first-party request built from a claude.ai credential sends
    ``{Authorization: `Bearer ${accessToken}`, "anthropic-beta":
    "oauth-2025-04-20"}``. That header pair is what goes out below.
  * The CLI does have cheap read-only endpoints — GET /api/oauth/profile,
    POST /api/oauth/validate, GET /api/oauth/claude_cli/roles — and not one of
    them is usable for this token. The binary says why in its own words:
    "OAuth token has no scope accepted by /api/oauth/validate (needs
    user:profile, user:office, or user:ccr_inference; env-var and setup-token
    sessions default to user:inference only)". A `claude setup-token` token
    carries `user:inference` and nothing else, so probing any of those would
    reject perfectly good tokens.

The only surface this credential can reach is therefore inference, and the
cheapest honest probe is the smallest possible turn: one word in, one token
out, carrying the same Claude Code system line the CLI sends (the binary keeps
a fixed set of three permitted first lines and picks one per mode). It is the
exact path a review takes, which is the point — a green check here means the
credential the review will use works, not that some adjacent endpoint likes it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

PROVIDER = "claude_code"

# Long-lived OAuth tokens minted by `claude setup-token`.
TOKEN_PREFIX = "sk-ant-oat"

#: Console API keys. A different credential with a different rulebook — see
#: `credential_kind` and `save_token`.
API_KEY_PREFIX = "sk-ant-api"

KIND_OAUTH = "oauth"
KIND_API_KEY = "api_key"


def credential_kind(secret: str) -> str | None:
    """Which of the two credentials this is, or None if it is neither.

    THE DIFFERENCE IS NOT COSMETIC. Anthropic's terms treat them as opposites:

      * An OAuth token is one person's Claude subscription. "Each end user must
        authenticate with their own... credentials" and a developer "may not
        pay for, resell, or intermediate Claude usage on their end users'
        behalf" — so it belongs to exactly one person and cannot be shared.

      * An API key is the customer's own key, and the terms say so explicitly:
        "configuring an API key in a development environment, secrets manager,
        or machine image for use by the customer's own authorized users",
        provided the usage bills to the key owner. A team may share one.

    That is why the workspace slot accepts one and refuses the other.
    """
    value = (secret or "").strip()
    if value.startswith(TOKEN_PREFIX):
        return KIND_OAUTH
    if value.startswith(API_KEY_PREFIX):
        return KIND_API_KEY
    return None

#: Where the probe goes and what it carries — copied from the CLI, see above.
VERIFY_URL = "https://api.anthropic.com/v1/messages"
VERIFY_HOST = "api.anthropic.com"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"
#: The CLI's own first system line. The binary matches the opening system
#: block against a fixed set of three; sending one of them keeps the probe
#: indistinguishable from the session it stands in for.
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
#: Cheapest current model. If Anthropic retires it the probe starts answering
#: 404 — which is exactly why anything that is not 200/401/403 is treated as
#: "could not verify" below. Our probe outliving its model must never look
#: like the operator's credential going bad.
PROBE_MODEL = "claude-haiku-4-5"
PROBE_TIMEOUT_SECONDS = 15.0

#: A stored token is re-probed at most this often. `status()` makes NO
#: provider call at all, so this bounds the explicit Test button: holding it
#: down is one request per five minutes per slot rather than one per click.
VERIFY_CACHE_SECONDS = 300
#: How long a good answer is presented as current. Past this the state is
#: "stale" rather than "verified" — a subscription token can be revoked at any
#: moment, and "it worked yesterday" is a different claim from "it works".
STALE_AFTER = timedelta(hours=24)

# Verification bookkeeping on the credential row's metadata dict, beside
# `saved_by`. Four keys because the row has to answer three different
# questions: did it ever pass, when did we last ask, and what came back.
_META_VERIFIED_AT = "verified_at"        # last SUCCESSFUL check (ISO-8601 UTC)
_META_CHECKED_AT = "verify_checked_at"   # last check of any outcome
_META_REASON = "verify_reason"           # provider's words, or ours; "" when ok
_META_CONCLUSIVE = "verify_conclusive"   # did Anthropic actually answer

# What the UI renders. "failed" and "unreachable" are deliberately separate:
# one means Anthropic looked at the token and said no, the other means we
# never got to ask. Showing the second as the first is a lie that sends an
# operator off to re-mint a token that was fine.
STATE_ABSENT = "absent"
STATE_NEVER_CHECKED = "never_checked"
STATE_VERIFIED = "verified"
STATE_STALE = "stale"
STATE_FAILED = "failed"
STATE_UNREACHABLE = "unreachable"

#: Anything shaped like an Anthropic secret, so no error string can carry one
#: into a stored row, an API response or a log line.
_SECRET_SHAPED = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")
_REDACTED = "sk-ant-…"


@dataclass(frozen=True)
class ClaudeConnection:
    token: str
    source: str          # "personal" | "workspace"
    saved_by: str | None
    kind: str = KIND_OAUTH

    @property
    def env(self) -> dict[str, str]:
        """The environment variable this credential is presented in.

        One place, because three call sites each wrote
        ``{"CLAUDE_CODE_OAUTH_TOKEN": conn.token}`` by hand and a fourth would
        have too — and the CLI does NOT treat the two variables as
        interchangeable. Measured against the binary: with both set, a broken
        ANTHROPIC_API_KEY beside a working OAuth token fails the session
        outright, while a working key beside a broken token succeeds. The key
        wins, so handing one over in the other's variable is not a near miss.
        """
        if self.kind == KIND_API_KEY:
            return {"ANTHROPIC_API_KEY": self.token}
        return {"CLAUDE_CODE_OAUTH_TOKEN": self.token}


@dataclass(frozen=True)
class VerificationResult:
    """One answer about one token.

    `ok=False` alone means nothing. Read it with `conclusive`: together they
    are the three real outcomes — accepted, refused, and never asked.
    """

    ok: bool
    #: Anthropic's own words when it refused; ours when we could not ask.
    #: Empty when `ok`.
    reason: str
    #: True when the provider gave a verdict. False means we never got an
    #: answer — DNS, a proxy, a timeout, a 5xx — and `ok=False` must NOT be
    #: read as "this token is bad".
    conclusive: bool
    checked_at: str
    latency_ms: int | None = None
    #: Answered from the row instead of the network — see VERIFY_CACHE_SECONDS.
    cached: bool = False


class TokenRejected(Exception):
    """A paste that will not be stored, and who decided that.

    `by_provider` is the difference between "Anthropic looked at this and
    refused it" and "we did, before asking". The endpoint used to prefix every
    one of these with "Anthropic rejected that token:", which turned a rule of
    ours — a workspace slot may not hold a subscription — into a claim about
    the operator's Anthropic account, and would send them to check something
    that is not wrong.
    """

    def __init__(self, reason: str, *, by_provider: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.by_provider = by_provider


def _ws_slot(workspace_id: str) -> str:
    from src.llm.keys import workspace_slot
    return workspace_slot(workspace_id)


def _slot_for(scope: str, user_id: str, workspace_id: str) -> str:
    return user_id if scope == "personal" else _ws_slot(workspace_id)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _scrub(text: str, token: str = "") -> str:
    """Strip anything credential-shaped out of a string on its way outward.

    Anthropic does not echo the token back and neither does httpx — but this
    string ends up on the credential row, in the API response and in a log
    line, and "it does not happen today" is not a property.
    """
    out = text.replace(token, _REDACTED) if token else text
    return _SECRET_SHAPED.sub(_REDACTED, out)


def token_looks_valid(token: str) -> bool:
    """The cheap first gate: refuse an obviously wrong paste without a call.

    Still the FIRST check and no longer the only one — `save_token` asks
    Anthropic after this passes.
    """
    return credential_kind(token) is not None and len(token.strip()) > 20


def _probe_client():
    """Guarded client for one Anthropic probe.

    `VERIFY_HOST` extends the allowlist the same way the LLM key ping does:
    api.anthropic.com is not on the shipped public list, and without the
    exception the transport that guards this request would refuse it.
    """
    from src.http import build_client

    return build_client(
        timeout=PROBE_TIMEOUT_SECONDS, extra_allowed_hosts=(VERIFY_HOST,),
    )


def _provider_reason(resp, token: str) -> str:
    """Anthropic's own explanation, or the bare status when it did not give one."""
    body = None
    try:
        body = resp.json()
    except ValueError:
        body = None
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            kind = str(err.get("type") or "").strip()
            detail = str(err.get("message") or "").strip()
            message = f"{kind}: {detail}" if kind and detail else (detail or kind)
    if not message:
        message = f"HTTP {resp.status_code}"
    return _scrub(message, token)[:400]


#: A key is checked against the model list: it costs nothing, returns 200 for a
#: live key and 401 for a dead one, and — usefully — 401 for an OAuth token, so
#: it also tells the two credentials apart.
MODELS_URL = "https://api.anthropic.com/v1/models"


def _verify_api_key(key: str) -> VerificationResult:
    """Ask Anthropic whether it accepts this API key.

    The OAuth probe above cannot answer this question: it sends a bearer token
    with the CLI's beta header, and `/v1/messages` refuses a Console key
    presented that way. Same three-line classifier as the OAuth probe, for the
    same reason — only a 200 may write "verified", and only a 401/403 may
    write "refused"; everything else is "could not tell".
    """
    import httpx

    from src.security.egress import EgressBlockedError

    checked_at = _now_iso()
    started = time.monotonic()
    try:
        with _probe_client() as client:
            resp = client.get(
                MODELS_URL,
                headers={
                    "x-api-key": key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
            )
    except (httpx.HTTPError, EgressBlockedError) as exc:
        return VerificationResult(
            ok=False, conclusive=False, checked_at=checked_at,
            reason=f"could not reach Anthropic: {_scrub(str(exc), key)}"[:400],
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code == 200:
        return VerificationResult(
            ok=True, reason="", conclusive=True,
            checked_at=checked_at, latency_ms=latency_ms,
        )
    reason = _provider_reason(resp, key)
    if resp.status_code in (401, 403):
        return VerificationResult(
            ok=False, reason=reason, conclusive=True,
            checked_at=checked_at, latency_ms=latency_ms,
        )
    return VerificationResult(
        ok=False, reason=f"could not verify: {reason}"[:400], conclusive=False,
        checked_at=checked_at, latency_ms=latency_ms,
    )


def verify_token(token: str) -> VerificationResult:
    """Ask Anthropic whether it accepts this token, the way the CLI does.

    The classifier is deliberately three lines and no cleverness:

        200         → verified.
        401 / 403   → refused, with the provider's own reason.
        anything else, including no answer at all → could not verify.

    A 429, a 5xx, a retired probe model's 404, a 400 from a request shape we
    got wrong — none of those is evidence about the operator's credential, and
    treating any of them as a rejection would throw away a working token.
    Symmetrically, nothing but a 200 is allowed to write "verified".
    """
    import httpx

    from src.security.egress import EgressBlockedError

    token = token.strip()
    if credential_kind(token) == KIND_API_KEY:
        return _verify_api_key(token)
    checked_at = _now_iso()
    started = time.monotonic()
    try:
        with _probe_client() as client:
            resp = client.post(
                VERIFY_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": PROBE_MODEL,
                    "max_tokens": 1,
                    "system": CLAUDE_CODE_SYSTEM,
                    "messages": [{"role": "user", "content": "ok"}],
                },
            )
    except (httpx.HTTPError, EgressBlockedError) as exc:
        # Only transport failures are swallowed. A TypeError from our own
        # request shape must keep travelling and show up as a 500 — burying
        # our bugs under "could not reach Anthropic" is how a broken probe
        # would quietly turn every save into an unverified one.
        return VerificationResult(
            ok=False, conclusive=False, checked_at=checked_at,
            reason=f"could not reach Anthropic: {_scrub(str(exc), token)}"[:400],
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    if resp.status_code == 200:
        return VerificationResult(
            ok=True, reason="", conclusive=True,
            checked_at=checked_at, latency_ms=latency_ms,
        )
    reason = _provider_reason(resp, token)
    if resp.status_code in (401, 403):
        return VerificationResult(
            ok=False, reason=reason, conclusive=True,
            checked_at=checked_at, latency_ms=latency_ms,
        )
    return VerificationResult(
        ok=False, reason=f"could not verify: {reason}"[:400], conclusive=False,
        checked_at=checked_at, latency_ms=latency_ms,
    )


def resolve_connection(user_id: str, workspace_id: str) -> ClaudeConnection | None:
    """Personal slot first, then the workspace-shared slot. None → not connected."""
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    for slot, source in ((user_id, "personal"), (_ws_slot(workspace_id), "workspace")):
        try:
            row = store.load(provider=PROVIDER, user_id=slot, account_label="default")
        except CredentialStoreError as exc:
            logger.warning("claude_token_unreadable slot=%s err=%s", slot, exc)
            continue
        if row is not None and row.secret:
            kind = credential_kind(row.secret)
            if source == "workspace" and kind != KIND_API_KEY:
                # Refused at READ time as well as at write time, because a slot
                # filled before the write-time rule existed is still filled.
                # Skipping it falls through to "not connected", which asks the
                # member for their own credential — the outcome the terms
                # require — instead of quietly spending somebody else's plan.
                logger.warning(
                    "claude_workspace_slot_refused ws=%s kind=%s — a shared "
                    "slot may hold an API key only", workspace_id, kind,
                )
                continue
            saved_by = (row.metadata or {}).get("saved_by") if isinstance(row.metadata, dict) else None
            return ClaudeConnection(token=row.secret, source=source, saved_by=saved_by,
                                    kind=kind or KIND_OAUTH)
    return None


def _verification_metadata(result: VerificationResult, base: dict) -> dict:
    """Fold one answer into a row's metadata, keeping what was already there."""
    meta = dict(base)
    meta[_META_CHECKED_AT] = result.checked_at
    meta[_META_REASON] = "" if result.ok else result.reason
    meta[_META_CONCLUSIVE] = result.conclusive
    if result.ok:
        meta[_META_VERIFIED_AT] = result.checked_at
    return meta


def save_token(
    *, token: str, user_id: str, workspace_id: str, scope: str, saved_by: str,
) -> VerificationResult:
    """Verify first, store second. scope: "personal" → the user, else ws:{id}.

    Raises :class:`TokenRejected` — and stores nothing — when the paste fails
    the cheap gate or when Anthropic refuses it.

    The two failure modes pull in opposite directions, and the split between
    them is the whole design:

      * Anthropic answered and said no — expired, revoked, half a paste. The
        token is NOT stored. Storing it would recreate the exact bug this
        function exists to kill: a row that reads "connected" and fails at the
        first review, hours later, in front of a customer.

      * We never got an answer — DNS, a proxy, a 5xx, wifi. The token IS
        stored, marked unverified, with the reason on the row. Dropping it
        would punish the operator for our network and lose a paste they can
        only get back by re-running `claude setup-token` on their own machine;
        and because presence and validity are separate questions in `status()`,
        nothing downstream can mistake an unverified row for a checked one.
        The UI shows "saved, not verified — press Test", which is true.
    """
    from src.credentials import get_credential_store

    token = token.strip()
    kind = credential_kind(token)
    if kind is None or not token_looks_valid(token):
        # The router says this in friendlier words before we get here; the
        # check is repeated so no future caller can reach the store around it.
        raise TokenRejected(
            "That is neither a `claude setup-token` value (sk-ant-oat…) nor an "
            "Anthropic API key (sk-ant-api…).",
        )

    # THE WORKSPACE SLOT TAKES AN API KEY AND NOTHING ELSE.
    #
    # A subscription token in a shared slot means one person's Claude plan runs
    # everybody else's sessions, which is the thing Anthropic's terms name
    # outright: "Customers may not pay for, resell, or intermediate Claude
    # usage on their end users' behalf. Each end user must authenticate with
    # their own Anthropic API key, Claude subscription plan credentials, or 3P
    # inference provider credential."
    #
    # An API key in the same slot is explicitly fine — "configuring an API key
    # in a development environment, secrets manager, or machine image for use
    # by the customer's own authorized users" — because the bill lands on the
    # key's owner under their own agreement. So the slot survives; only the
    # credential that made it a violation is turned away.
    #
    # The UI used to carry a warning here instead. A warning is not a control:
    # it told the operator they might be breaking the terms and then saved it.
    if scope != "personal" and kind == KIND_OAUTH:
        raise TokenRejected(
            "A subscription token belongs to one person and cannot be shared "
            "with a workspace — each member signs in with their own. Paste an "
            "Anthropic API key (sk-ant-api…) here instead: its usage bills to "
            "the key's owner, which the terms allow.",
        )

    result = verify_token(token)
    if result.conclusive and not result.ok:
        logger.info(
            "claude_token_refused scope=%s ws=%s by=%s reason=%s",
            scope, workspace_id, saved_by, result.reason,
        )
        raise TokenRejected(result.reason, by_provider=True)

    get_credential_store().save(
        provider=PROVIDER,
        secret=token,
        metadata=_verification_metadata(result, {"saved_by": saved_by, "scope": scope}),
        user_id=_slot_for(scope, user_id, workspace_id),
        account_label="default",
    )
    logger.info(
        "claude_token_saved scope=%s ws=%s by=%s verified=%s reason=%s",
        scope, workspace_id, saved_by, result.ok, result.reason,
    )
    return result


def _cached_result(meta: dict) -> VerificationResult | None:
    """The row's own last answer, when it is younger than the cache window."""
    checked_at = str(meta.get(_META_CHECKED_AT) or "")
    when = _parse_iso(checked_at) if checked_at else None
    if when is None:
        return None
    if (datetime.now(UTC) - when).total_seconds() >= VERIFY_CACHE_SECONDS:
        return None
    reason = str(meta.get(_META_REASON) or "")
    return VerificationResult(
        ok=(meta.get(_META_VERIFIED_AT) == checked_at and not reason),
        reason=reason,
        conclusive=bool(meta.get(_META_CONCLUSIVE)),
        checked_at=checked_at,
        cached=True,
    )


def recheck_token(
    *, user_id: str, workspace_id: str, scope: str,
) -> VerificationResult | None:
    """Re-probe a stored token and write the answer back onto its row.

    None → that slot holds nothing to check.

    A previously-good token that has since been revoked ends up here: the row
    stays (throwing away the operator's paste on a re-check would be the same
    mistake as throwing it away on a network blip), and its state flips to
    "failed" with Anthropic's reason, which is what the UI needs to say.
    """
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    slot = _slot_for(scope, user_id, workspace_id)
    try:
        row = store.load(provider=PROVIDER, user_id=slot, account_label="default")
    except CredentialStoreError as exc:
        logger.warning("claude_token_unreadable slot=%s err=%s", slot, exc)
        return None
    if row is None or not row.secret:
        return None

    meta = dict(row.metadata) if isinstance(row.metadata, dict) else {}
    cached = _cached_result(meta)
    if cached is not None:
        return cached

    result = verify_token(row.secret)
    # The store has no metadata-only update, so the secret goes back in
    # unchanged alongside the new bookkeeping. It came out of this same row a
    # few lines up; nothing else has touched it.
    store.save(
        provider=PROVIDER,
        secret=row.secret,
        metadata=_verification_metadata(result, meta),
        user_id=slot,
        account_label="default",
    )
    logger.info(
        "claude_token_rechecked scope=%s ws=%s ok=%s conclusive=%s reason=%s",
        scope, workspace_id, result.ok, result.conclusive, result.reason,
    )
    return result


def delete_token(*, user_id: str, workspace_id: str, scope: str) -> None:
    from src.credentials import get_credential_store

    get_credential_store().delete(
        provider=PROVIDER,
        user_id=_slot_for(scope, user_id, workspace_id),
        account_label="default",
    )


def _state_of(meta: dict) -> tuple[str, str | None, str | None]:
    """(state, verified_at, reason) for one present row, from its metadata."""
    verified_at = meta.get(_META_VERIFIED_AT)
    verified_at = str(verified_at) if verified_at else None
    checked_at = str(meta.get(_META_CHECKED_AT) or "")
    reason = str(meta.get(_META_REASON) or "") or None

    if not checked_at:
        # Rows written before verification existed, and only those.
        return STATE_NEVER_CHECKED, verified_at, reason
    if verified_at == checked_at and reason is None:
        when = _parse_iso(verified_at) if verified_at else None
        if when is not None and datetime.now(UTC) - when > STALE_AFTER:
            return STATE_STALE, verified_at, None
        return STATE_VERIFIED, verified_at, None
    if meta.get(_META_CONCLUSIVE):
        return STATE_FAILED, verified_at, reason
    return STATE_UNREACHABLE, verified_at, reason


def status(user_id: str, workspace_id: str) -> dict:
    """Connection status for the UI — never the token, and never a network call.

    Presence and validity are different questions and the UI has to ask both.
    `personal`/`workspace` answer the first: a row exists. `*_state`,
    `*_verified_at` and `*_reason` answer the second, from what the last
    verification wrote on that row.

    This is rendered on every page load, so it reads and never probes. The
    probes live on save and on the explicit test endpoint, and the row is
    their shared memory.
    """
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    out: dict = {
        "personal": False, "workspace": False, "workspace_saved_by": None,
        "personal_state": STATE_ABSENT, "personal_verified_at": None,
        "personal_reason": None,
        "workspace_state": STATE_ABSENT, "workspace_verified_at": None,
        "workspace_reason": None,
    }
    for key, slot in (("personal", user_id), ("workspace", _ws_slot(workspace_id))):
        try:
            row = store.load(provider=PROVIDER, user_id=slot, account_label="default")
        except CredentialStoreError:
            continue
        if not (row and row.secret):
            continue
        meta = row.metadata if isinstance(row.metadata, dict) else {}
        out[key] = True
        state, verified_at, reason = _state_of(meta)
        out[f"{key}_state"] = state
        out[f"{key}_verified_at"] = verified_at
        out[f"{key}_reason"] = reason
        if key == "workspace":
            out["workspace_saved_by"] = meta.get("saved_by")
    return out


__all__ = [
    "PROVIDER",
    "STALE_AFTER",
    "STATE_ABSENT",
    "STATE_FAILED",
    "STATE_NEVER_CHECKED",
    "STATE_STALE",
    "STATE_UNREACHABLE",
    "STATE_VERIFIED",
    "TOKEN_PREFIX",
    "VERIFY_CACHE_SECONDS",
    "ClaudeConnection",
    "TokenRejected",
    "VerificationResult",
    "delete_token",
    "recheck_token",
    "resolve_connection",
    "credential_kind",
    "KIND_API_KEY",
    "KIND_OAUTH",
    "save_token",
    "status",
    "token_looks_valid",
    "verify_token",
]
