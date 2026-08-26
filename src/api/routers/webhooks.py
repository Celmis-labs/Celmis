"""Everything a workspace needs to wire up auto-review by webhook.

Auto-review has one setup step nobody can guess: the provider has to be told
where to POST, and both sides have to agree on a secret. Neither value is
discoverable from the UI today, which is why the feature looked broken rather
than unconfigured — the endpoints answered 500 "secret not configured" to
every delivery, including the correct ones, and the only place that said so
was a server log.

So this router answers exactly two questions, per provider:

    where do I paste the URL, and what secret goes with it?

The URL is built from the request the browser made, not from a setting. That
is deliberate — see `_public_base()`.

The secret is generated here rather than typed by the user. A webhook secret
is machine-to-machine and has no reason to be memorable, and the only thing a
human-chosen one adds is the chance of a weak one.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, get_current_user
from src.review.webhook_secrets import (
    resolve_webhook_secret,
    save_webhook_secret,
)
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

PROVIDERS = ("github", "gitlab", "bitbucket")

#: Where the API sits behind the reverse proxy. Caddy strips `/backend` before
#: forwarding, so a URL the user pastes into GitHub must include it while the
#: route itself does not.
_PROXY_PREFIX = "/backend"


class WebhookSetup(BaseModel):
    provider: str
    url: str
    configured: bool = Field(
        description="whether a secret exists for this workspace and provider",
    )
    events: list[str]
    header: str
    scheme: str


class SecretOut(BaseModel):
    provider: str
    secret: str
    url: str


def _public_base(request: Request) -> str:
    """The origin the browser used, honouring the reverse proxy.

    Built from the request rather than from PUBLIC_BASE_URL because that
    setting is consumed by exactly two places, both of them the mailer, and is
    routinely left at its default on a self-hosted box. A wrong value here
    produces a URL that GitHub accepts, cannot reach, and reports only as a
    red delivery on a settings page nobody opens — the worst kind of failure
    to hand somebody as a copy-paste instruction.

    The forwarded headers are what the proxy sets; falling back to the request
    URL keeps it correct when there is no proxy at all.
    """
    headers = request.headers
    proto = headers.get("x-forwarded-proto") or request.url.scheme
    host = headers.get("x-forwarded-host") or headers.get("host")
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}"


def _webhook_url(request: Request, provider: str, workspace_id: str) -> str:
    return f"{_public_base(request)}{_PROXY_PREFIX}/webhook/{provider}/{workspace_id}"


# Per provider: the header it signs with, how it verifies, and the events to
# subscribe to. Kept here rather than in the UI so the two cannot drift.
_SCHEME = {
    "github": ("X-Hub-Signature-256", "HMAC-SHA256 over the request body",
               ["pull_request"]),
    "gitlab": ("X-Gitlab-Token", "plaintext token comparison (GitLab does not "
                                 "sign the body)",
               ["Merge request events"]),
    "bitbucket": ("X-Hub-Signature", "HMAC-SHA256 over the request body",
                  ["pullrequest:created", "pullrequest:updated"]),
}


@router.get("", response_model=list[WebhookSetup])
async def list_webhook_setup(
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
    _user: User = Depends(get_current_user),
) -> list[WebhookSetup]:
    """What to paste where, and whether this workspace is set up yet."""
    from src.review.settings import get_review_settings

    settings = get_review_settings()
    out = []
    for provider in PROVIDERS:
        header, scheme, events = _SCHEME[provider]
        out.append(WebhookSetup(
            provider=provider,
            url=_webhook_url(request, provider, workspace_id),
            # Resolved, not merely "is there a row" — a `default` workspace
            # backed by the environment is genuinely configured, and telling
            # it otherwise would send somebody to fix a thing that works.
            configured=bool(
                resolve_webhook_secret(provider, workspace_id, settings)),
            events=events,
            header=header,
            scheme=scheme,
        ))
    return out


@router.post("/{provider}/secret", response_model=SecretOut)
async def rotate_secret(
    provider: str,
    request: Request,
    workspace_id: str = Depends(current_workspace_id),
    _user: User = Depends(get_current_user),
) -> SecretOut:
    """Generate and store this workspace's secret for one provider.

    Returned in full exactly once, because the caller has to paste it into the
    provider. It is not readable afterwards: the store keeps it encrypted and
    nothing reads it back out except signature verification. Losing it means
    generating another and updating the provider — which is the correct shape
    for a shared secret, and cheap.
    """
    if provider not in PROVIDERS:
        raise HTTPException(404, f"Unknown provider: {provider}")

    secret = secrets.token_urlsafe(32)
    try:
        save_webhook_secret(provider, workspace_id, secret)
    except Exception as exc:  # noqa: BLE001
        logger.error("webhook_secret_save_failed provider=%s ws=%s err=%s",
                     provider, workspace_id, exc)
        raise HTTPException(
            500,
            "Could not store the secret. The credential store is unreadable — "
            "check CREDENTIAL_MASTER_KEY.",
        ) from exc

    logger.info("webhook_secret_rotated provider=%s workspace=%s",
                provider, workspace_id)
    return SecretOut(
        provider=provider,
        secret=secret,
        url=_webhook_url(request, provider, workspace_id),
    )


__all__ = ["router"]
