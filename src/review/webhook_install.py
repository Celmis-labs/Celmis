"""Install the review webhook on the provider, instead of asking a human to.

A repository registered with auto-review switched on reviewed NOTHING until
somebody opened the provider's settings, found the webhook page, and pasted a
URL, a secret and the right event list into it — a step the product documented
and never performed. On a real Bitbucket install that step was simply never
done: the repository sat registered, `/pull-requests` and `/issues` stayed
empty, and nothing anywhere said why.

So this module does the provider half itself, with the workspace's own git
token (the same one review already uses):

    GitHub     POST /repos/{owner}/{repo}/hooks           events pull_request, push
    GitLab     POST /projects/:id/hooks                    merge_requests_events + token
    Bitbucket  POST /2.0/repositories/{ws}/{slug}/hooks    pullrequest:* + secret

Rules every provider follows:

  * **Idempotent.** List the hooks first; a hook whose URL is ours is UPDATED
    (events, secret, active) rather than duplicated. Pressing "Repair" twice
    leaves one hook.
  * **The secret is the workspace's existing one** (src/review/webhook_secrets),
    generated and stored only if there is none yet — so the hook verifies
    against exactly what the receiver resolves, and a manual setup that already
    works is not rotated out from under itself.
  * **Never returns or logs a secret.** Provider error text is scrubbed of it
    before it goes anywhere.
  * **A failure is a status, not an exception**, from :func:`install` /
    :func:`uninstall` / :func:`status`: registration must not fail because the
    token cannot manage webhooks, and the caller needs a reason and a hint it
    can show.

What each provider sends and what the receiver (src/review/webhook.py) checks:

    GitHub     X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(secret, body)
    GitLab     X-Gitlab-Token      = the token, compared in plaintext
    Bitbucket  X-Hub-Signature     = "sha256=" + HMAC-SHA256(secret, body)
"""

from __future__ import annotations

import json
import logging
import secrets as _secrets
import sqlite3
import urllib.parse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"
BITBUCKET_API = "https://api.bitbucket.org/2.0"

#: Where the API sits behind the bundled Caddy, which strips it before
#: forwarding. Same constant the manual-setup page uses
#: (src/api/routers/webhooks.py), so the URL we install is the URL it shows.
PROXY_PREFIX = "/backend"

#: The events each receiver acts on — and nothing else, so the provider does
#: not spend deliveries on events we would answer "ignored".
#:   GitHub:    pull_request (review + lifecycle), push (index refresh), the
#:              two comment events (`@celmis` commands, feedback replies) and
#:              review-thread resolved / reopened (feedback).
#:   GitLab:    Merge Request Hook, and Comments (a "Note Hook" — commands).
#:   Bitbucket: created/updated (review) + fulfilled/rejected (lifecycle) +
#:              comment created/updated (commands) + approved/unapproved
#:              (productivity: who reviewed, and when) + repo:push (index refresh).
EVENTS: dict[str, list[str]] = {
    "github": ["pull_request", "push", "issue_comment", "pull_request_review_comment",
               "pull_request_review_thread"],
    "gitlab": ["merge_requests_events", "note_events"],
    "bitbucket": [
        "pullrequest:created",
        "pullrequest:updated",
        "pullrequest:fulfilled",
        "pullrequest:rejected",
        "pullrequest:comment_created",
        "pullrequest:comment_updated",
        "pullrequest:approved",
        "pullrequest:unapproved",
        "repo:push",
    ],
}

#: The events that exist for the comment commands only. A hook without them
#: still reviews; it is "outdated" (see `missing_events`) until it is repaired.
#: The same goes for a Bitbucket hook without the approval events the
#: productivity history reads.
COMMAND_EVENTS: dict[str, list[str]] = {
    "github": ["issue_comment", "pull_request_review_comment", "pull_request_review_thread"],
    "gitlab": ["note_events"],
    "bitbucket": ["pullrequest:comment_created", "pullrequest:comment_updated"],
}

#: What a token needs, per provider — shown to the user when a call is refused.
PERMISSION_HINTS: dict[str, str] = {
    "github": (
        "The GitHub token must be allowed to manage repository webhooks: a "
        "classic token needs the `admin:repo_hook` scope (or `repo` with admin "
        "rights on the repository); a fine-grained token needs Repository "
        "permissions → Webhooks: Read and write. The account must be an admin "
        "of the repository."
    ),
    "gitlab": (
        "The GitLab token needs the `api` scope, and its account needs the "
        "Maintainer (or Owner) role on the project. A self-hosted GitLab must "
        "also be able to reach this Celmis address (PUBLIC_BASE_URL); if Celmis "
        "is on a private network, a GitLab admin may have to allow it under "
        "Admin → Settings → Network → Outbound requests."
    ),
    "bitbucket": (
        "The Bitbucket token needs webhook permission — Atlassian API token "
        "scopes `read:webhook:bitbucket` + `write:webhook:bitbucket`, or "
        "Webhooks: Read and write on a repository/workspace access token — and "
        "the account must be an admin of the repository."
    ),
}

_HOOK_DESCRIPTION = "Celmis code review"


class WebhookInstallError(Exception):
    """A provider call that did not do what we asked. Message is secret-free."""

    def __init__(self, code: str, message: str, *, hint: str | None = None,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.http_status = http_status


@dataclass
class WebhookStatus:
    """What the API answers about one repository's webhook. Never a secret."""

    provider: str
    #: installed | not_installed | failed | skipped | unknown
    status: str
    url: str | None = None
    events: list[str] = field(default_factory=list)
    hook_id: str | None = None
    #: "created" / "updated" / "removed" on the call that did it.
    action: str | None = None
    #: Short machine code when not installed: permission, auth, not_found,
    #: no_public_url, no_credentials, rejected, network, provider_error, …
    reason: str | None = None
    message: str | None = None
    hint: str | None = None
    #: GitHub's last_response / GitLab's alert_status, when the provider says.
    last_delivery: dict[str, Any] | None = None
    #: The repo's name as the provider spells it (what the payload will carry).
    full_name: str | None = None
    updated_at: str | None = None
    #: Events the receiver acts on that the installed hook does not subscribe
    #: to — a hook from before comment commands existed. `outdated` is "there
    #: are some"; installing again updates the hook in place.
    missing_events: list[str] = field(default_factory=list)
    outdated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Public URL ─────────────────────────────────────────────────


_LOOPBACK = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def public_base_url() -> tuple[str | None, str | None]:
    """(base, problem). `base` is PUBLIC_BASE_URL normalised, or None with why.

    A provider has to reach this from the internet, so an unset value, one
    without a scheme, or a loopback host is refused up front: GitHub would
    reject it anyway, and Bitbucket would accept it and deliver nowhere.
    """
    from src.config import get_settings

    raw = (get_settings().public_base_url or "").strip().rstrip("/")
    if not raw:
        return None, (
            "PUBLIC_BASE_URL is not set. Set it to the address this Celmis is "
            "reachable at from the internet (e.g. https://celmis.example.com) "
            "and restart, or add the webhook manually with the URL and secret "
            "from Settings → Webhooks."
        )
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None, (
            f"PUBLIC_BASE_URL={raw!r} needs an http:// or https:// scheme and a "
            "host."
        )
    if parts.hostname in _LOOPBACK:
        return None, (
            f"PUBLIC_BASE_URL={raw!r} points at this machine; a git provider "
            "cannot deliver to it. Use the public address."
        )
    return raw, None


def webhook_url(base: str, provider: str, workspace_id: str) -> str:
    """The receiver URL for one provider + workspace, behind the proxy prefix."""
    base = base.rstrip("/")
    if not base.endswith(PROXY_PREFIX):
        base = f"{base}{PROXY_PREFIX}"
    ws = urllib.parse.quote(workspace_id, safe="")
    return f"{base}/webhook/{provider}/{ws}"


def _same_url(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


# ─── Secret ──────────────────────────────────────────────────────


def ensure_secret(provider: str, workspace_id: str) -> str:
    """The workspace's secret for `provider`, created once if it has none.

    Reuses what the receiver already resolves — including the environment
    fallback of the `default` workspace — so the installed hook and the
    receiver can never disagree.
    """
    from src.review.settings import get_review_settings
    from src.review.webhook_secrets import resolve_webhook_secret, save_webhook_secret

    existing = resolve_webhook_secret(provider, workspace_id, get_review_settings())
    if existing:
        return existing
    secret = _secrets.token_urlsafe(32)
    save_webhook_secret(provider, workspace_id, secret)
    logger.info("webhook_secret_created provider=%s workspace=%s",
                provider, workspace_id)
    return secret


# ─── HTTP seam ───────────────────────────────────────────────────


def _gitlab_api(credential: Any) -> str:
    """REST base of the GitLab instance the credential row names.

    Raises WebhookInstallError for a stored URL that no longer passes the
    rules — never falls back to gitlab.com with a self-hosted token.
    """
    from src.sync.gitlab_instance import UnsafeGitLabURL, instance_for_credential

    try:
        return instance_for_credential(credential).api_base
    except UnsafeGitLabURL as exc:
        raise WebhookInstallError(
            "gitlab_url", f"The workspace's GitLab URL cannot be used: {exc}",
            hint="Re-save the GitLab connection on the Connections page.",
        ) from None


def _client(provider: str, credential: Any) -> httpx.Client:
    """A guarded client authenticated as the stored credential.

    The one place tests replace to mock the provider. Egress goes through
    src/http.py — all three API hosts are on the shipped allowlist; a
    self-hosted GitLab is this client's one extra host, pinned to its
    validated address (src/sync/gitlab_instance.py).
    """
    from src.http import build_client

    token = credential.secret
    headers = {"Accept": "application/json", "User-Agent": "celmis-webhook-installer"}
    if provider == "github":
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        return build_client(timeout=20.0, headers=headers)
    if provider == "gitlab":
        from src.sync.gitlab_instance import UnsafeGitLabURL, instance_for_credential

        headers["PRIVATE-TOKEN"] = token
        try:
            extra = instance_for_credential(credential).http_kwargs()
        except UnsafeGitLabURL as exc:
            raise WebhookInstallError(
                "gitlab_url", f"The workspace's GitLab URL cannot be used: {exc}",
                hint="Re-save the GitLab connection on the Connections page.",
            ) from None
        return build_client(timeout=20.0, headers=headers, **extra)
    email = (credential.metadata or {}).get("atlassian_email") if isinstance(
        credential.metadata, dict) else None
    if email:
        return build_client(timeout=20.0, headers=headers, auth=(str(email), token))
    headers["Authorization"] = f"Bearer {token}"
    return build_client(timeout=20.0, headers=headers)


def _scrub(text: str, secret: str | None) -> str:
    text = (text or "")[:400]
    if secret:
        text = text.replace(secret, "***")
    return text


def _provider_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        for key in ("message", "error_description", "error"):
            if body.get(key):
                return str(body[key])
    return str(body)[:200]


def _check(resp: httpx.Response, provider: str, what: str, secret: str | None) -> None:
    """Raise a WebhookInstallError that says what to do, for a non-2xx."""
    if resp.status_code < 400:
        return
    msg = _scrub(_provider_message(resp), secret)
    code = resp.status_code
    hint = PERMISSION_HINTS[provider]
    if code == 401:
        raise WebhookInstallError(
            "auth", f"{provider} rejected the token while trying to {what} "
            f"(HTTP 401): {msg}. Reconnect it on the Connections page.",
            hint=hint, http_status=code)
    if code == 403:
        raise WebhookInstallError(
            "permission", f"The {provider} token is not allowed to {what} "
            f"(HTTP 403): {msg}", hint=hint, http_status=code)
    if code == 404:
        # GitHub and Bitbucket answer 404, not 403, for a hooks endpoint the
        # token may not see — so a 404 here is usually a permission problem.
        raise WebhookInstallError(
            "not_found", f"{provider} answered 404 while trying to {what}: the "
            "repository does not exist under that name, or the token cannot "
            f"administer it. {msg}", hint=hint, http_status=code)
    if code in (400, 422):
        raise WebhookInstallError(
            "rejected", f"{provider} refused to {what} (HTTP {code}): {msg}",
            hint=hint, http_status=code)
    raise WebhookInstallError(
        "provider_error", f"{provider} failed to {what} (HTTP {code}): {msg}",
        http_status=code)


# ─── Providers ───────────────────────────────────────────────────


@dataclass
class _Found:
    canonical_full_name: str
    hook: dict[str, Any] | None


def _gh_repo(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _gl_project(full_name: str) -> str:
    return urllib.parse.quote(full_name, safe="")


def _bb_repo(full_name: str) -> str:
    ws, _, slug = full_name.partition("/")
    return f"{urllib.parse.quote(ws, safe='')}/{urllib.parse.quote(slug, safe='')}"


def _canonical(c: httpx.Client, provider: str, full_name: str, secret: str | None,
               *, gl_api: str = GITLAB_API) -> str:
    """The repo's name as the provider spells it — what payloads will carry."""
    if provider == "github":
        r = c.get(f"{GITHUB_API}/repos/{_gh_repo(full_name)}")
        _check(r, provider, "read the repository", secret)
        return str(r.json().get("full_name") or full_name)
    if provider == "gitlab":
        r = c.get(f"{gl_api}/projects/{_gl_project(full_name)}")
        _check(r, provider, "read the project", secret)
        return str(r.json().get("path_with_namespace") or full_name)
    r = c.get(f"{BITBUCKET_API}/repositories/{_bb_repo(full_name)}")
    _check(r, provider, "read the repository", secret)
    return str(r.json().get("full_name") or full_name)


def _hook_url(provider: str, hook: dict[str, Any]) -> str | None:
    if provider == "github":
        return (hook.get("config") or {}).get("url")
    return hook.get("url")


def _hook_id(provider: str, hook: dict[str, Any]) -> str:
    if provider == "bitbucket":
        return str(hook.get("uuid") or "")
    return str(hook.get("id") or "")


def _list_hooks(c: httpx.Client, provider: str, full_name: str,
                secret: str | None, *, gl_api: str = GITLAB_API) -> list[dict[str, Any]]:
    if provider == "github":
        r = c.get(f"{GITHUB_API}/repos/{_gh_repo(full_name)}/hooks",
                  params={"per_page": 100})
        _check(r, provider, "list repository webhooks", secret)
        return list(r.json() or [])
    if provider == "gitlab":
        r = c.get(f"{gl_api}/projects/{_gl_project(full_name)}/hooks",
                  params={"per_page": 100})
        _check(r, provider, "list project hooks", secret)
        return list(r.json() or [])
    out: list[dict[str, Any]] = []
    url: str | None = f"{BITBUCKET_API}/repositories/{_bb_repo(full_name)}/hooks"
    params: dict[str, Any] | None = {"pagelen": 100}
    for _ in range(10):  # bounded: no repository has 1000 webhooks
        if not url:
            break
        r = c.get(url, params=params)
        _check(r, provider, "list repository webhooks", secret)
        body = r.json() or {}
        out.extend(body.get("values") or [])
        url, params = body.get("next"), None
    return out


def _find(c: httpx.Client, provider: str, full_name: str, url: str,
          secret: str | None, *, gl_api: str = GITLAB_API) -> _Found:
    canonical = _canonical(c, provider, full_name, secret, gl_api=gl_api)
    for hook in _list_hooks(c, provider, canonical, secret, gl_api=gl_api):
        if _same_url(_hook_url(provider, hook), url):
            return _Found(canonical, hook)
    return _Found(canonical, None)


def _body(provider: str, url: str, secret: str) -> dict[str, Any]:
    if provider == "github":
        return {
            "name": "web",
            "active": True,
            "events": EVENTS["github"],
            "config": {"url": url, "content_type": "json", "secret": secret,
                       "insecure_ssl": "0"},
        }
    if provider == "gitlab":
        return {
            "url": url,
            "token": secret,
            "merge_requests_events": True,
            "note_events": True,
            "push_events": False,
            "enable_ssl_verification": True,
            "name": _HOOK_DESCRIPTION,
            "description": _HOOK_DESCRIPTION,
        }
    return {
        "description": _HOOK_DESCRIPTION,
        "url": url,
        "active": True,
        "events": EVENTS["bitbucket"],
        "secret": secret,
    }


def _write(c: httpx.Client, provider: str, full_name: str, url: str, secret: str,
           existing: dict[str, Any] | None, *,
           gl_api: str = GITLAB_API) -> tuple[str, dict[str, Any]]:
    """Create, or update in place. Returns (action, hook)."""
    body = _body(provider, url, secret)
    if provider == "github":
        base = f"{GITHUB_API}/repos/{_gh_repo(full_name)}/hooks"
        if existing:
            patch = {k: v for k, v in body.items() if k != "name"}
            r = c.patch(f"{base}/{_hook_id(provider, existing)}", json=patch)
            _check(r, provider, "update the webhook", secret)
            return "updated", r.json()
        r = c.post(base, json=body)
        _check(r, provider, "create the webhook", secret)
        return "created", r.json()
    if provider == "gitlab":
        base = f"{gl_api}/projects/{_gl_project(full_name)}/hooks"
        if existing:
            r = c.put(f"{base}/{_hook_id(provider, existing)}", json=body)
            _check(r, provider, "update the project hook", secret)
            return "updated", r.json()
        r = c.post(base, json=body)
        _check(r, provider, "create the project hook", secret)
        return "created", r.json()
    base = f"{BITBUCKET_API}/repositories/{_bb_repo(full_name)}/hooks"
    if existing:
        uid = urllib.parse.quote(_hook_id(provider, existing), safe="")
        r = c.put(f"{base}/{uid}", json=body)
        _check(r, provider, "update the webhook", secret)
        return "updated", r.json()
    r = c.post(base, json=body)
    _check(r, provider, "create the webhook", secret)
    return "created", r.json()


def _delete(c: httpx.Client, provider: str, full_name: str, hook: dict[str, Any],
            *, gl_api: str = GITLAB_API) -> None:
    hid = urllib.parse.quote(_hook_id(provider, hook), safe="")
    if provider == "github":
        r = c.delete(f"{GITHUB_API}/repos/{_gh_repo(full_name)}/hooks/{hid}")
    elif provider == "gitlab":
        r = c.delete(f"{gl_api}/projects/{_gl_project(full_name)}/hooks/{hid}")
    else:
        r = c.delete(f"{BITBUCKET_API}/repositories/{_bb_repo(full_name)}/hooks/{hid}")
    if r.status_code != 404:  # already gone is the outcome we wanted
        _check(r, provider, "delete the webhook", None)


def _last_delivery(provider: str, hook: dict[str, Any]) -> dict[str, Any] | None:
    if provider == "github":
        lr = hook.get("last_response") or {}
        if lr.get("code") is None and lr.get("status") in (None, "unused"):
            return {"status": "unused"} if lr else None
        return {"code": lr.get("code"), "status": lr.get("status"),
                "message": lr.get("message")}
    if provider == "gitlab":
        if hook.get("alert_status") or hook.get("disabled_until"):
            return {"status": hook.get("alert_status"),
                    "disabled_until": hook.get("disabled_until")}
        return None
    return None  # Bitbucket's hook resource carries no delivery history


def missing_events(provider: str, installed: list[str]) -> list[str]:
    """What the receiver acts on that a hook subscribed to `installed` would
    never deliver. GitHub's `*` subscribes to everything."""
    if "*" in installed:
        return []
    return [e for e in EVENTS.get(provider, []) if e not in installed]


def _hook_events(provider: str, hook: dict[str, Any]) -> list[str]:
    if provider == "gitlab":
        return [k for k in ("merge_requests_events", "push_events",
                            "note_events", "issues_events") if hook.get(k)]
    return list(hook.get("events") or [])


# ─── Orchestration ───────────────────────────────────────────────


def _credential(provider: str, user_id: str, workspace_id: str) -> Any:
    from src.credentials import resolve_git_credential

    cred = resolve_git_credential(provider, user_id=user_id, workspace_id=workspace_id)
    if cred is None:
        raise WebhookInstallError(
            "no_credentials",
            f"No {provider} token is connected for this workspace — connect "
            "one on the Connections page.",
            hint=PERMISSION_HINTS.get(provider),
        )
    return cred


def _failed(provider: str, url: str | None, exc: WebhookInstallError) -> WebhookStatus:
    return WebhookStatus(
        provider=provider, status="failed", url=url, events=EVENTS.get(provider, []),
        reason=exc.code, message=exc.message, hint=exc.hint,
    )


def install(cfg: Any, *, user_id: str, base: str) -> WebhookStatus:
    """Create or repair this repository's webhook. Never raises.

    `cfg` is the RepoConfig. On success `full_name` carries the provider's own
    spelling of the repository, which the caller should store as the binding.
    """
    provider = cfg.provider
    url = webhook_url(base, provider, cfg.workspace_id)
    if provider not in EVENTS:
        return WebhookStatus(provider=provider, status="failed", url=url,
                             reason="unsupported_provider",
                             message=f"Unsupported provider {provider!r}")
    secret: str | None = None
    try:
        cred = _credential(provider, user_id, cfg.workspace_id)
        gl_api = _gitlab_api(cred) if provider == "gitlab" else GITLAB_API
        secret = ensure_secret(provider, cfg.workspace_id)
        with _client(provider, cred) as c:
            found = _find(c, provider, cfg.full_name, url, secret, gl_api=gl_api)
            action, hook = _write(c, provider, found.canonical_full_name, url,
                                  secret, found.hook, gl_api=gl_api)
    except WebhookInstallError as exc:
        logger.warning("webhook_install_failed provider=%s repo=%s ws=%s code=%s",
                       provider, cfg.full_name, cfg.workspace_id, exc.code)
        return _failed(provider, url, exc)
    except httpx.HTTPError as exc:
        logger.warning("webhook_install_network provider=%s repo=%s err=%s",
                       provider, cfg.full_name, type(exc).__name__)
        return WebhookStatus(
            provider=provider, status="failed", url=url, events=EVENTS[provider],
            reason="network", message=_scrub(f"Could not reach {provider}: {exc}", secret),
        )
    except Exception as exc:  # noqa: BLE001 — a status, never a 500 for the caller
        logger.warning("webhook_install_error provider=%s repo=%s err=%s",
                       provider, cfg.full_name, type(exc).__name__)
        return WebhookStatus(
            provider=provider, status="failed", url=url, events=EVENTS[provider],
            reason="error", message=_scrub(str(exc), secret),
        )
    logger.info("webhook_installed provider=%s repo=%s ws=%s action=%s",
                provider, found.canonical_full_name, cfg.workspace_id, action)
    return WebhookStatus(
        provider=provider, status="installed", url=url, events=EVENTS[provider],
        hook_id=_hook_id(provider, hook) or None, action=action,
        full_name=found.canonical_full_name,
    )


def uninstall(cfg: Any, *, user_id: str, base: str) -> WebhookStatus:
    """Remove our webhook (matched by URL) if it exists. Never raises."""
    provider = cfg.provider
    url = webhook_url(base, provider, cfg.workspace_id)
    try:
        cred = _credential(provider, user_id, cfg.workspace_id)
        gl_api = _gitlab_api(cred) if provider == "gitlab" else GITLAB_API
        with _client(provider, cred) as c:
            found = _find(c, provider, cfg.full_name, url, None, gl_api=gl_api)
            if found.hook is not None:
                _delete(c, provider, found.canonical_full_name, found.hook,
                        gl_api=gl_api)
    except WebhookInstallError as exc:
        return _failed(provider, url, exc)
    except httpx.HTTPError as exc:
        return WebhookStatus(provider=provider, status="failed", url=url,
                             reason="network", message=f"Could not reach {provider}: "
                             f"{type(exc).__name__}")
    logger.info("webhook_uninstalled provider=%s repo=%s ws=%s existed=%s",
                provider, cfg.full_name, cfg.workspace_id, found.hook is not None)
    return WebhookStatus(
        provider=provider, status="not_installed", url=url, events=EVENTS[provider],
        action="removed" if found.hook is not None else None,
        full_name=found.canonical_full_name,
    )


def status(cfg: Any, *, user_id: str, base: str) -> WebhookStatus:
    """Ask the provider whether our hook is there. Never raises."""
    provider = cfg.provider
    url = webhook_url(base, provider, cfg.workspace_id)
    try:
        cred = _credential(provider, user_id, cfg.workspace_id)
        gl_api = _gitlab_api(cred) if provider == "gitlab" else GITLAB_API
        with _client(provider, cred) as c:
            found = _find(c, provider, cfg.full_name, url, None, gl_api=gl_api)
    except WebhookInstallError as exc:
        st = _failed(provider, url, exc)
        st.status = "unknown"
        return st
    except httpx.HTTPError as exc:
        return WebhookStatus(provider=provider, status="unknown", url=url,
                             events=EVENTS.get(provider, []), reason="network",
                             message=f"Could not reach {provider}: {type(exc).__name__}")
    if found.hook is None:
        return WebhookStatus(provider=provider, status="not_installed", url=url,
                             events=EVENTS[provider],
                             full_name=found.canonical_full_name)
    hook = found.hook
    active = hook.get("active", True)
    subscribed = _hook_events(provider, hook)
    # An empty list is a hook whose events the provider did not report, not a
    # hook that hears nothing: claim nothing is missing rather than a repair.
    missing = missing_events(provider, subscribed) if subscribed else []
    return WebhookStatus(
        provider=provider, status="installed" if active else "failed",
        url=url, events=subscribed or EVENTS[provider],
        hook_id=_hook_id(provider, hook) or None,
        reason=None if active else "inactive",
        message=None if active else "The webhook exists but is disabled on the provider.",
        last_delivery=_last_delivery(provider, hook),
        full_name=found.canonical_full_name,
        missing_events=missing, outdated=bool(missing),
    )


# ─── Last known state (no live call) ─────────────────────────────
#
# The repository list shows a badge per row; asking every provider on every
# page load would be N API calls and N chances at a rate limit. So the last
# outcome of install/repair/remove/status is kept beside the auto-review
# config, in the same SQLite file — its own table, so no migration and no
# change to the config rows the webhook router reads.

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_webhook_state (
    workspace_id TEXT NOT NULL,
    repo_slug    TEXT NOT NULL,
    provider     TEXT NOT NULL,
    status       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (workspace_id, repo_slug)
);
"""


class WebhookStateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_STATE_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def save(self, workspace_id: str, repo_slug: str, st: WebhookStatus) -> None:
        st.updated_at = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repo_webhook_state "
                "(workspace_id, repo_slug, provider, status, payload, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, repo_slug) DO UPDATE SET "
                "provider=excluded.provider, status=excluded.status, "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (workspace_id, repo_slug, st.provider, st.status,
                 json.dumps(st.as_dict()), st.updated_at),
            )

    def for_workspace(self, workspace_id: str) -> dict[str, WebhookStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT repo_slug, payload FROM repo_webhook_state WHERE workspace_id=?",
                (workspace_id,),
            ).fetchall()
        out: dict[str, WebhookStatus] = {}
        for r in rows:
            try:
                out[r["repo_slug"]] = WebhookStatus(**json.loads(r["payload"]))
            except (TypeError, ValueError):
                continue
        return out

    def get(self, workspace_id: str, repo_slug: str) -> WebhookStatus | None:
        return self.for_workspace(workspace_id).get(repo_slug)

    def delete(self, workspace_id: str, repo_slug: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM repo_webhook_state WHERE workspace_id=? AND repo_slug=?",
                (workspace_id, repo_slug),
            )


_state_stores: dict[Path, WebhookStateStore] = {}


def get_webhook_state_store() -> WebhookStateStore:
    """The store beside auto_review.db for the CURRENT settings.

    Keyed by path rather than a single module global, so a process whose
    workspace_dir changes (tests, mostly) never writes into a stale one.
    """
    from src.config import get_settings

    path = get_settings().workspace_dir / "secrets" / "auto_review.db"
    store = _state_stores.get(path)
    if store is None:
        store = _state_stores[path] = WebhookStateStore(path)
    return store


__all__ = [
    "COMMAND_EVENTS",
    "EVENTS",
    "PERMISSION_HINTS",
    "WebhookInstallError",
    "WebhookStateStore",
    "WebhookStatus",
    "ensure_secret",
    "get_webhook_state_store",
    "install",
    "missing_events",
    "public_base_url",
    "status",
    "uninstall",
    "webhook_url",
]
