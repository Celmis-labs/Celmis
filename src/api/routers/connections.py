"""Connection routes — manage GitHub / GitLab / Bitbucket / Jira tokens.

Each workspace can save tokens for any of the git providers. Tokens are stored
encrypted via CredentialStore. The verify step calls the provider's /user
endpoint to confirm the token works.

GitLab may be self-hosted: the body's ``base_url`` names the instance. It is
validated (src/sync/gitlab_instance.py — https, public address unless the
operator allowlisted the host) BEFORE the token is sent to it, verified with
GET /api/v4/user, and stored in the credential row's metadata next to the
token. The two only ever change together: the endpoint requires the token on
every save, so nobody can re-point a stored token at a host they control.

Jira (the task the business-logic agent reads) is the same shape: the body's
``base_url`` is the site (https://<site>.atlassian.net, or an operator-listed
host — src/sync/jira_instance.py), ``email`` + ``token`` are the Atlassian
account and API token, verified with GET /rest/api/3/myself before anything is
stored. ``reuse_bitbucket`` copies the saved Bitbucket email + token on the
server (an unscoped Atlassian API token works for both) so the token never
travels to the browser and back. The token is never written to a log or to an
audit row.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    is_workspace_admin,
    require_workspace_admin,
)
from src.api.schemas import ConnectionStatus, ConnectionUpsert, ConnectionVerifyResult
from src.credentials import (
    GIT_PROVIDERS,
    get_credential_store,
    git_workspace_slot,
    resolve_git_credential,
)
from src.security.audit import record_action
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])


# Git providers — tokens live in the workspace's own slot (see git_keys).
_GIT_PROVIDERS = GIT_PROVIDERS

# LLM providers (Stage 11). Same encrypted store; verify path differs.
_LLM_PROVIDERS = ("openai", "anthropic", "google", "openrouter", "groq")

#: Read-only task trackers. Not git providers: no repositories hang off them,
#: and the resolver for git tokens must never return one.
_TRACKER_PROVIDERS = ("jira",)

_PROVIDERS = _GIT_PROVIDERS + _TRACKER_PROVIDERS + _LLM_PROVIDERS


def _slot_for(provider: str, workspace_id: str) -> str:
    """Which credentials-store `user_id` this provider's token belongs under.

    Every provider (git AND LLM) now lands in the workspace's own `ws:{id}`
    slot, so a token saved in one workspace can never be resolved by another.
    """
    return git_workspace_slot(workspace_id)


@router.get("", response_model=list[ConnectionStatus])
def list_connections(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[ConnectionStatus]:
    """List connection status across all git + LLM providers for this workspace.

    Git rows are read through the same resolver the workers use, so the page
    shows what a review run would actually pick up — including a legacy
    personally-owned token that still works but is nobody's responsibility.

    Anybody in the workspace may ask WHETHER a provider is connected (the
    dashboard and the setup checklist need it); only an owner or admin is told
    which account it is (an Atlassian e-mail, a Jira host, the credential slot).
    No caller is ever sent a token.
    """
    store = get_credential_store()
    saved = {row["provider"]: row for row in store.list(user_id=_slot_for("", workspace_id))}
    for p in _GIT_PROVIDERS:
        stored = resolve_git_credential(
            p, user_id=user.id, workspace_id=workspace_id, store=store,
        )
        if stored is None:
            saved.pop(p, None)
            continue
        saved[p] = {
            "provider": p,
            "account_label": stored.account_label,
            "metadata": {**stored.metadata, "slot": stored.user_id},
            "updated_at": stored.updated_at,
            "last_used_at": stored.last_used_at,
        }
    out: list[ConnectionStatus] = []
    may_see_account = is_workspace_admin(user, workspace_id)
    for p in _PROVIDERS:
        row = saved.get(p)
        if row is None:
            out.append(ConnectionStatus(provider=p, connected=False))
        elif not may_see_account:
            out.append(ConnectionStatus(
                provider=p, connected=True, updated_at=row.get("updated_at")))
        else:
            out.append(ConnectionStatus(
                provider=p,
                connected=True,
                account_label=str(row.get("account_label", "default")),
                metadata=dict(row.get("metadata", {}) or {}),
                updated_at=row.get("updated_at"),
                last_used_at=row.get("last_used_at"),
            ))
    return out


@router.put("/{provider}", response_model=ConnectionVerifyResult)
def upsert_connection(
    provider: str,
    request: Request,
    req: ConnectionUpsert,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectionVerifyResult:
    """Save token for a provider AFTER verifying it works.

    GitHub/GitLab — verify by calling /user endpoint.
    Bitbucket — needs email + token (Atlassian Basic auth).
    """
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
    if req.provider != provider:
        raise HTTPException(status_code=400, detail="Body/path provider mismatch")
    if provider not in ("gitlab", "jira") and (req.base_url or "").strip():
        raise HTTPException(status_code=400,
                            detail="A custom URL is only supported for GitLab and Jira")
    if provider != "jira" and req.reuse_bitbucket:
        raise HTTPException(status_code=400,
                            detail="reuse_bitbucket is only supported for Jira")

    if provider == "jira":
        req, refusal = _jira_request(req, user_id=user.id, workspace_id=workspace_id)
        if refusal is not None:
            return refusal
    verify = _verify_token(provider, req)
    if not verify.ok:
        return verify  # don't save if verification failed

    metadata: dict[str, object] = {"username": verify.username or ""}
    if provider == "gitlab":
        from src.sync.gitlab_instance import DEFAULT_BASE_URL, METADATA_KEY

        # Only a self-hosted instance is written: a row without the key IS a
        # gitlab.com row, exactly as every row saved before this existed.
        if verify.base_url and verify.base_url != DEFAULT_BASE_URL:
            metadata[METADATA_KEY] = verify.base_url
    if provider == "jira":
        from src.sync.jira_instance import METADATA_KEY as JIRA_URL_KEY

        metadata[JIRA_URL_KEY] = verify.base_url or ""
        metadata["atlassian_email"] = req.email or ""
    if provider == "bitbucket" and req.email:
        metadata["atlassian_email"] = req.email
    if provider == "bitbucket" and req.workspace:
        metadata["bitbucket_workspace"] = req.workspace

    # Git tokens are workspace property, not the admin's personal property:
    # background workers must keep polling and commenting after whoever saved
    # the token leaves. Who *saved* it stays in metadata for the audit trail.
    metadata["saved_by"] = user.id
    store = get_credential_store()
    slot = _slot_for(provider, workspace_id)
    store.save(
        provider=provider,
        secret=req.token,
        metadata=metadata,
        user_id=slot,
        account_label=req.account_label or "default",
    )
    if provider == "jira":
        _forget_jira_reads(workspace_id)
    logger.info(
        "connection_saved user=%s workspace=%s provider=%s username=%s slot=%s",
        user.id, workspace_id, provider, verify.username, slot,
    )
    # The action, not the value. `detail` carries the SHAPE of what changed —
    # a provider name and the account it authenticates as — and never the
    # token: an audit row is written on the successful path, so a secret in it
    # would be worse than the 422 that echoed one.
    record_action(
        action="connection.saved", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=f"{provider}:{req.account_label or 'default'}",
        ip=client_ip(request),
        detail={"provider": provider, "username": verify.username, "slot": slot,
                **({"gitlab_url": verify.base_url} if provider == "gitlab" else {}),
                **({"jira_url": verify.base_url,
                    "reused_bitbucket": bool(
                        getattr(req, "reuse_bitbucket", False))}
                   if provider == "jira" else {})},
    )
    return verify


def _forget_jira_reads(workspace_id: str) -> None:
    """A new or removed Jira credential: what the old one read is not served to
    the new one (the cache key carries no credential identity)."""
    from src.review.task_context import cache as task_cache
    from src.review.task_context.service import forget_projects

    task_cache.purge_workspace(workspace_id)
    forget_projects()


@router.delete("/{provider}", status_code=204)
def delete_connection(
    provider: str,
    request: Request, user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
    store = get_credential_store()
    store.delete(provider=provider, user_id=_slot_for(provider, workspace_id))
    # Only the default/transition tenant can still resolve legacy rows, so only
    # there does "Disconnect" need to purge them — otherwise it would appear to
    # do nothing as the resolver falls through to an older slot.
    if provider in _GIT_PROVIDERS and workspace_id == "default":
        for slot in dict.fromkeys((user.id, "default")):
            store.delete(provider=provider, user_id=slot)
    if provider == "jira":
        _forget_jira_reads(workspace_id)
    logger.info("connection_deleted user=%s workspace=%s provider=%s", user.id, workspace_id, provider)
    record_action(
        action="connection.deleted", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=provider, ip=client_ip(request),
        detail={"provider": provider},
    )


@router.post("/{provider}/verify", response_model=ConnectionVerifyResult)
def verify_existing(
    provider: str, user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> ConnectionVerifyResult:
    """Re-verify the stored token for this provider — no body needed."""
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider!r}")
    store = get_credential_store()
    stored = (
        resolve_git_credential(provider, user_id=user.id, workspace_id=workspace_id, store=store)
        if provider in _GIT_PROVIDERS
        else store.load(provider=provider, user_id=_slot_for(provider, workspace_id))
    )
    if stored is None:
        raise HTTPException(status_code=404, detail="No saved token for this provider")
    email = stored.metadata.get("atlassian_email") if isinstance(stored.metadata, dict) else None
    workspace = (
        stored.metadata.get("bitbucket_workspace")
        if isinstance(stored.metadata, dict) else None
    )
    base_url = None
    if provider == "gitlab" and isinstance(stored.metadata, dict):
        from src.sync.gitlab_instance import METADATA_KEY

        base_url = stored.metadata.get(METADATA_KEY) or None
    if provider == "jira" and isinstance(stored.metadata, dict):
        from src.sync.jira_instance import METADATA_KEY as JIRA_URL_KEY

        base_url = stored.metadata.get(JIRA_URL_KEY) or None
    return _verify_token(
        provider,
        ConnectionUpsert(
            provider=provider,
            token=stored.secret,
            email=str(email) if email else None,
            workspace=str(workspace) if workspace else None,
            base_url=str(base_url) if base_url else None,
        ),
    )


def _verify_token(provider: str, req: ConnectionUpsert) -> ConnectionVerifyResult:
    """Call provider's /user endpoint."""
    import httpx

    from src.http import build_client

    try:
        if provider == "github":
            with build_client(timeout=10.0) as http:
                resp = http.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {req.token}"},
                )
            if resp.status_code == 200:
                login = resp.json().get("login")
                # Classic PATs report granted scopes here; fine-grained PATs
                # send an empty header (their permissions aren't enumerable).
                raw = resp.headers.get("X-OAuth-Scopes", "")
                scopes = [s.strip() for s in raw.split(",") if s.strip()]
                return ConnectionVerifyResult(
                    ok=True, provider=provider, username=login, scopes=scopes,
                )
            return ConnectionVerifyResult(
                ok=False, provider=provider,
                error=f"GitHub /user returned {resp.status_code}",
            )

        if provider == "gitlab":
            return _verify_gitlab(req)

        if provider == "jira":
            return _verify_jira(req)

        if provider == "bitbucket":
            if not req.email:
                return ConnectionVerifyResult(
                    ok=False, provider=provider,
                    error="Bitbucket requires Atlassian email",
                )
            # Prefer /workspaces/{slug} — works for both Atlassian API tokens
            # and Workspace Access Tokens (WATs are scoped to workspace, so
            # /user returns 403 even when token is valid).
            if req.workspace:
                with build_client(timeout=10.0) as http:
                    resp = http.get(
                        f"https://api.bitbucket.org/2.0/workspaces/{req.workspace}",
                        auth=(req.email, req.token),
                    )
                if resp.status_code == 200:
                    body = resp.json()
                    username = body.get("slug") or body.get("name") or req.workspace
                    return ConnectionVerifyResult(
                        ok=True, provider=provider, username=str(username),
                    )
                if resp.status_code == 401:
                    return ConnectionVerifyResult(
                        ok=False, provider=provider,
                        error="Bitbucket: invalid email or token (401). "
                              "Use Atlassian API token + your login email.",
                    )
                if resp.status_code == 403:
                    return ConnectionVerifyResult(
                        ok=False, provider=provider,
                        error=f"Bitbucket: token authenticates but cannot access "
                              f"workspace '{req.workspace}' (403). Either the token "
                              f"is scoped to a different workspace, or it lacks "
                              f"`workspace:read` permission.",
                    )
                if resp.status_code == 404:
                    return ConnectionVerifyResult(
                        ok=False, provider=provider,
                        error=f"Workspace '{req.workspace}' not found (404). "
                              f"Check the slug at bitbucket.org/<slug>/.",
                    )
                return ConnectionVerifyResult(
                    ok=False, provider=provider,
                    error=f"Bitbucket /workspaces/{req.workspace} returned "
                          f"{resp.status_code}: {resp.text[:200]}",
                )

            # Fallback: no workspace provided — try /user
            with build_client(timeout=10.0) as http:
                resp = http.get(
                    "https://api.bitbucket.org/2.0/user",
                    auth=(req.email, req.token),
                )
            if resp.status_code == 200:
                username = resp.json().get("username") or resp.json().get("nickname")
                return ConnectionVerifyResult(ok=True, provider=provider, username=username)
            return ConnectionVerifyResult(
                ok=False, provider=provider,
                error=f"Bitbucket /user returned {resp.status_code}. "
                      f"Tip: provide workspace slug — Workspace Access Tokens "
                      f"can't read /user but can read /workspaces/<slug>.",
            )

        # ── Stage 11: LLM providers ──
        if provider in _LLM_PROVIDERS:
            return _verify_llm_token(provider, req.token)

    except httpx.HTTPError as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=str(exc))

    return ConnectionVerifyResult(ok=False, provider=provider, error="Unknown provider")


def _jira_request(
    req: ConnectionUpsert, *, user_id: str, workspace_id: str,
) -> tuple[ConnectionUpsert, ConnectionVerifyResult | None]:
    """Fill a Jira save from the saved Bitbucket credential when asked to.

    Returns the request to verify, or a refusal sentence. The Bitbucket token
    is read here, on the server, and goes only into the verify call to the
    Jira site the URL rules accepted.
    """
    if not req.reuse_bitbucket:
        return req, None
    stored = resolve_git_credential(
        "bitbucket", user_id=user_id, workspace_id=workspace_id,
        store=get_credential_store())
    meta = stored.metadata if stored is not None and isinstance(stored.metadata, dict) else {}
    email = str(meta.get("atlassian_email") or "").strip()
    if stored is None or not stored.secret or not email:
        return req, ConnectionVerifyResult(
            ok=False, provider="jira",
            error="No Bitbucket token with an Atlassian email is saved for this "
                  "workspace, so there is nothing to reuse — enter the email and "
                  "API token for Jira")
    return req.model_copy(update={"token": stored.secret, "email": email}), None


def _verify_jira(req: ConnectionUpsert) -> ConnectionVerifyResult:
    """URL rules → address rules → GET /rest/api/3/myself with the token.

    Nothing reaches the network before the URL passed both rule sets, and no
    message carries the token or the email: they are built from the host and
    the status code only (src/review/task_context/jira_client.py).
    """
    from src.review.task_context.jira_client import JiraClient, JiraError
    from src.sync.jira_instance import UnsafeJiraURL, validate_base_url

    provider = "jira"
    email = (req.email or "").strip()
    if not email:
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error="Jira needs the Atlassian account email next to the API token")
    try:
        instance = validate_base_url(req.base_url)
    except UnsafeJiraURL as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=str(exc))
    from src.config import get_settings

    try:
        with JiraClient(instance, email, req.token,
                        timeout=float(get_settings().jira_timeout_seconds)) as client:
            who = client.myself()
    except JiraError as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=exc.sentence)
    name = str(who.get("displayName") or who.get("emailAddress") or who.get("name")
               or who.get("accountId") or "")
    return ConnectionVerifyResult(
        ok=True, provider=provider, username=name, base_url=instance.base_url)


def _verify_gitlab(req: ConnectionUpsert) -> ConnectionVerifyResult:
    """URL rules → address rules → GET /api/v4/user with the token.

    Nothing reaches the network before the URL passed both rule sets, and no
    message carries the token: they are built from the host and the status
    code only.
    """
    import ssl

    import httpx

    from src.security.egress import EgressBlockedError
    from src.sync.gitlab_instance import (
        DEFAULT_BASE_URL,
        UnsafeGitLabURL,
        build_gitlab_client,
        validate_base_url,
    )

    provider = "gitlab"
    try:
        instance = validate_base_url((req.base_url or "").strip() or DEFAULT_BASE_URL)
    except UnsafeGitLabURL as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=str(exc))
    where = "GitLab" if instance.is_default else f"GitLab at {instance.host}"
    try:
        with build_gitlab_client(instance, token=req.token, timeout=10.0) as http:
            resp = http.get(f"{instance.api_base}/user")
    except UnsafeGitLabURL as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=str(exc))
    except EgressBlockedError:
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{where}: outbound connections to this host are blocked by "
                  "the server's egress policy")
    except FileNotFoundError:
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error="GITLAB_CA_BUNDLE points at a file that does not exist on the "
                  "Celmis server")
    except (ssl.SSLError, httpx.ConnectError) as exc:
        cert = "CERTIFICATE" in str(exc).upper()
        hint = (" — the certificate is not trusted; a private CA needs "
                "GITLAB_CA_BUNDLE set by the server operator") if cert else (
                " — the Celmis server cannot reach this host; an internal-only "
                "GitLab needs a network route (VPN/peering) from the server")
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{where}: could not connect{hint}")
    except httpx.HTTPError as exc:
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{where}: request failed ({type(exc).__name__})")
    if 300 <= resp.status_code < 400:
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{where} answered a redirect ({resp.status_code}); redirects "
                  "are not followed — enter the final instance URL")
    if resp.status_code == 200:
        try:
            username = (resp.json() or {}).get("username")
        except ValueError:
            username = None
        if not username:
            return ConnectionVerifyResult(
                ok=False, provider=provider,
                error=f"{where}: /api/v4/user did not answer like GitLab — check the URL")
        return ConnectionVerifyResult(ok=True, provider=provider, username=str(username),
                                      base_url=instance.base_url)
    if resp.status_code in (401, 403):
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{where} rejected the token ({resp.status_code}) — it needs the "
                  "`api` scope (or `read_api`)")
    return ConnectionVerifyResult(
        ok=False, provider=provider,
        error=f"{where}: /api/v4/user returned {resp.status_code}")


def _verify_llm_token(provider: str, token: str) -> ConnectionVerifyResult:
    """Cheap ping to confirm the LLM key works.

    Every provider has a `/models` list endpoint that requires auth but no
    quota — 200 means the key is valid. This is much cheaper than making a
    real completion call.
    """
    import httpx

    from src.http import build_client

    url, headers = _llm_verify_endpoint(provider, token)
    # The URL is one of the constants in _llm_verify_endpoint — never request
    # data — so its host may extend the allowlist: api.openai.com,
    # api.anthropic.com, openrouter.ai and api.groq.com are not on the shipped
    # public list, and without the exception the key ping would be refused by
    # the very transport that now guards it.
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""

    try:
        with build_client(
            timeout=10.0, extra_allowed_hosts=(host,) if host else (),
        ) as http:
            resp = http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return ConnectionVerifyResult(ok=False, provider=provider, error=str(exc))

    if resp.status_code == 200:
        # For OpenRouter, /models is also our source-of-truth for pricing, but
        # we only ping here — pricing refresh runs in the background.
        return ConnectionVerifyResult(ok=True, provider=provider, username=provider)
    if resp.status_code in (401, 403):
        return ConnectionVerifyResult(
            ok=False, provider=provider,
            error=f"{provider}: invalid key ({resp.status_code}).",
        )
    return ConnectionVerifyResult(
        ok=False, provider=provider,
        error=f"{provider}: /models returned {resp.status_code}",
    )


def _llm_verify_endpoint(provider: str, token: str) -> tuple[str, dict[str, str]]:
    if provider == "openai":
        return "https://api.openai.com/v1/models", {"Authorization": f"Bearer {token}"}
    if provider == "anthropic":
        # Anthropic requires `anthropic-version` header + `x-api-key` (not Bearer).
        return "https://api.anthropic.com/v1/models", {
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        }
    if provider == "google":
        # Google uses `?key=` — no auth header.
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models?key={token}",
            {},
        )
    if provider == "openrouter":
        # /api/v1/key returns the key's remaining credit — no models list needed.
        return "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {token}"}
    if provider == "groq":
        return "https://api.groq.com/openai/v1/models", {
            "Authorization": f"Bearer {token}",
        }
    # Should not reach — caller filters by _LLM_PROVIDERS.
    return "", {}
