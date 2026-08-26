"""Connection routes — manage GitHub / GitLab / Bitbucket tokens.

Each user can save tokens for any of the three providers. Tokens are stored
encrypted via CredentialStore. The verify step calls the provider's /user
endpoint to confirm the token works.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
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

_PROVIDERS = _GIT_PROVIDERS + _LLM_PROVIDERS


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
    for p in _PROVIDERS:
        row = saved.get(p)
        if row is None:
            out.append(ConnectionStatus(provider=p, connected=False))
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

    verify = _verify_token(provider, req)
    if not verify.ok:
        return verify  # don't save if verification failed

    metadata: dict[str, object] = {"username": verify.username or ""}
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
        detail={"provider": provider, "username": verify.username, "slot": slot},
    )
    return verify


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
    return _verify_token(
        provider,
        ConnectionUpsert(
            provider=provider,
            token=stored.secret,
            email=str(email) if email else None,
            workspace=str(workspace) if workspace else None,
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
            with build_client(timeout=10.0) as http:
                resp = http.get(
                    "https://gitlab.com/api/v4/user",
                    headers={"PRIVATE-TOKEN": req.token},
                )
            if resp.status_code == 200:
                username = resp.json().get("username")
                return ConnectionVerifyResult(ok=True, provider=provider, username=username)
            return ConnectionVerifyResult(
                ok=False, provider=provider,
                error=f"GitLab /user returned {resp.status_code}",
            )

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
