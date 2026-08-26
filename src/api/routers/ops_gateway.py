"""Prove the LiteLLM gateway works — without a shell on the server.

    GET  /api/ops/gateway         — status. Read-only, ops token or admin.
    POST /api/ops/gateway/verify  — the end-to-end check. Global admin only.

`scripts/verify_gateway.sh` does the same thing, but it needs SSH. This runs
the identical checks from the same vantage point — inside the compose network,
where `http://litellm:4000` resolves and no port is published — so the answer
is one HTTP call from anywhere, including a phone.

The verify route is deliberately NOT on the ops token. That token unlocks
read-only diagnostics; this route creates a team, registers deployments and
mints a key. It cleans up after itself, but "cleans up after itself" is not
the same as read-only, and the token's whole value is that its blast radius
is obvious.

The provider key comes from the workspace's own credential store rather than
an env var, because that is where keys actually live — production has no
GEMINI_API_KEY in its environment on purpose, since an env-level key would be
a cross-tenant fallback.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query

from src.api.deps import require_admin, require_ops_access
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops/gateway", tags=["ops"])


#: Where the proxy lives inside the compose network. Probed directly so that
#: step 1 of the rollout — proxy running, app NOT yet routed through it — is
#: observable at all; at that point LITELLM_PROXY_URL is deliberately unset.
DEFAULT_PROXY = "http://litellm:4000"


@router.get("")
def gateway_status(_access: str = Depends(require_ops_access)) -> dict:
    """Is the proxy up, and is the app routed through it? Never mutates.

    Those are two different questions and the rollout deliberately separates
    them, so answering only the second would report "off" for a perfectly
    healthy proxy.

    Reachability is measured, not read out of the environment. COMPOSE_PROFILES
    and LITELLM_SALT_KEY are compose-level and are never passed into this
    container, so checking os.environ for them reports a confident False for a
    correctly configured install. A proxy that answers is better evidence than
    either: litellm-init refuses to start without both keys, so a live
    container proves they were present.
    """
    import os

    from src.llm import gateway

    proxy = gateway.proxy_url()
    out: dict[str, Any] = {
        "routed_through_proxy": gateway.is_enabled(),
        "proxy_url": proxy or None,
        "master_key_set": bool(gateway.master_key()),
        "master_key_prefix_ok": gateway.master_key().startswith("sk-"),
        # Passed through by compose for exactly this reason — see the note in
        # docker-compose.yml. Tells "the .env never asked for the gateway"
        # apart from "it asked and the container failed to come up", which
        # otherwise look identical from here.
        "profile_requested": "gateway" in (os.environ.get("COMPOSE_PROFILES") or ""),
        "salt_key_set": bool((os.environ.get("LITELLM_SALT_KEY_SET") or "").strip()),
    }

    probe = proxy or DEFAULT_PROXY
    out["probed"] = probe
    out["proxy_up"] = _liveliness(probe)

    if out["proxy_up"] and gateway.master_key():
        resp = _admin_get(probe, gateway.master_key(), "/model/info")
        out["deployments"] = len((resp or {}).get("data") or []) if resp else None

    if not proxy:
        out["note"] = (
            "The app is calling providers DIRECTLY — LITELLM_PROXY_URL is unset. "
            + ("The proxy itself is up, so this is step 1 finished: run "
               "POST /api/ops/gateway/verify, and set LITELLM_PROXY_URL only if "
               "it passes."
               if out["proxy_up"] else
               ("The proxy is not answering, and COMPOSE_PROFILES does not ask "
                "for it — add COMPOSE_PROFILES=gateway to the DEPLOY_ENV secret "
                "and deploy again."
                if not out["profile_requested"] else
                "COMPOSE_PROFILES asks for the gateway but the proxy is not "
                "answering — the container failed to start. Check that "
                "LITELLM_SALT_KEY is set: litellm-init refuses without it."))
        )
    return out


def _probe_client(base: str, *, timeout: float):
    """Guarded client for one probe against `base`.

    `base` is LITELLM_PROXY_URL or DEFAULT_PROXY — operator configuration and
    a module constant, never request data — and a compose-internal name like
    `litellm` is exactly what the shipped public allowlist will never carry,
    so the probe names its one host the same way src/llm/gateway.py names its
    configured proxy (_proxy_host).
    """
    from urllib.parse import urlsplit

    from src.http import build_client

    host = urlsplit(base).hostname or ""
    return build_client(timeout=timeout, extra_allowed_hosts=(host,) if host else ())


def _liveliness(base: str) -> bool:
    try:
        with _probe_client(base, timeout=5.0) as c:
            return c.get(f"{base.rstrip('/')}/health/liveliness").status_code == 200
    except Exception:  # noqa: BLE001 — unreachable is an answer, not an error
        return False


def _admin_get(base: str, key: str, path: str) -> dict | None:
    status, body = _admin(base, key, "GET", path)
    return body if status == 200 else None


def _admin(base: str, key: str, method: str, path: str,
           payload: dict | None = None) -> tuple[int, dict]:
    """One admin call against `base`.

    Not `gateway._call`: that reads LITELLM_PROXY_URL, which is deliberately
    unset while the gateway is being verified.
    """
    try:
        with _probe_client(base, timeout=30.0) as c:
            r = c.request(method, f"{base.rstrip('/')}{path}", json=payload,
                          headers={"Authorization": f"Bearer {key}"})
        try:
            return r.status_code, (r.json() if r.content else {})
        except Exception:  # noqa: BLE001
            return r.status_code, {}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)[:200]}


@router.post("/verify")
def gateway_verify(
    workspace_id: str = Query(default="default",
                              description="whose stored provider key to use"),
    _user: User = Depends(require_admin),
) -> dict:
    """Create a throwaway team + deployments + scoped key, use them, delete them.

    Returns ``{ok, checks: [{name, ok, detail}], cleaned}``. Every check is
    reported rather than short-circuited: knowing that the admin plane works
    and only the completion failed is the difference between a bad master key
    and a bad provider key.
    """
    from src.llm import gateway

    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail[:300]})
        return bool(ok)

    # The whole point of this route is to run BEFORE LITELLM_PROXY_URL is set,
    # so it must not require it: fall back to the in-network address.
    base = gateway.proxy_url() or DEFAULT_PROXY
    if not gateway.master_key().startswith("sk-"):
        record("master key", False, "LITELLM_MASTER_KEY missing or not sk- prefixed.")
        return {"ok": False, "checks": checks, "cleaned": True}

    key = gateway.master_key()
    if not record("proxy reachable", _liveliness(base), base):
        return {"ok": False, "checks": checks, "cleaned": True}

    provider, api_key, models = _provider_material(workspace_id)
    if not api_key:
        record("provider key", False,
               f"workspace {workspace_id!r} has no stored provider key — save one "
               "under LLM Setup first. The admin plane below is still checked.")

    probe = f"verify-{uuid.uuid4().hex[:8]}"
    team = gateway.team_id(probe)
    chat_dep = gateway.deployment_name(probe, "chat")
    embed_dep = gateway.deployment_name(probe, "embeddings")
    # Registered but left OUT of the virtual key's list. Being refused a model
    # that does not exist proves nothing; being refused one that does proves
    # the per-tenant allow-list is real.
    off_limits = gateway.deployment_name(probe, "off-limits")
    virtual_key = ""

    try:
        status, body = _admin(base, key, "POST", "/team/new",
                              {"team_id": team, "team_alias": team})
        record("team created", status == 200, _brief(status, body))

        if api_key:
            for name, model, extra in (
                (chat_dep, models.get("chat", ""), {}),
                (embed_dep, models.get("embeddings", ""), {"mode": "embedding"}),
                (off_limits, models.get("chat", ""), {}),
            ):
                if not model:
                    continue
                status, body = _admin(base, key, "POST", "/model/new", {
                    "model_name": name,
                    "litellm_params": {
                        "model": gateway.qualify_model(provider, model),
                        "api_key": api_key,
                    },
                    "model_info": {"celmis_verify": True, **extra},
                })
                record(f"deployment {name.split('-')[-1]}", status == 200,
                       _brief(status, body))

            status, body = _admin(base, key, "POST", "/key/generate", {
                "models": [chat_dep, embed_dep],
                "team_id": team,
                "key_alias": team,
            })
            virtual_key = str((body or {}).get("key") or "")
            record("scoped virtual key minted", bool(virtual_key),
                   _brief(status, body))

            if virtual_key:
                ok, detail = _chat(base, virtual_key, chat_dep)
                record("chat completion through the proxy", ok, detail)
                ok, detail = _embed(base, virtual_key, embed_dep)
                record("embeddings through the proxy", ok, detail)
                # The point of the whole exercise.
                ok, detail = _refused(base, virtual_key, off_limits)
                record("a model outside the key's list is refused", ok, detail)
    except Exception as exc:  # noqa: BLE001 — a probe must not 500 the ops page
        record("unexpected error", False, str(exc)[:300])
    finally:
        cleaned = _cleanup(base, key, virtual_key, team,
                           [chat_dep, embed_dep, off_limits])

    # The proxy working is not the same as the APP using it. This resolves the
    # real profiles, which is the exact decision the first production request
    # makes — and provisioning is idempotent, so it is also what that request
    # would have done anyway.
    checks.extend(_routing_checks(workspace_id))

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "cleaned": cleaned,
        "workspace_id": workspace_id,
    }


def _routing_checks(workspace_id: str) -> list[dict[str, Any]]:
    """Does each surface actually resolve to the proxy?

    Reported per surface rather than as one verdict: embeddings are
    workspace-SHARED and always resolve against "default", so a tenant can
    legitimately route chat through the gateway while embeddings answer for a
    different slot — one combined boolean would hide that.
    """
    from src.llm import gateway

    out: list[dict[str, Any]] = []
    if not gateway.is_enabled():
        out.append({
            "name": "app routes through the proxy",
            "ok": False,
            "detail": "LITELLM_PROXY_URL is unset — this run only proves the "
                      "proxy itself works.",
        })
        return out

    # `_routed` — not `resolve_profile`. Resolving deliberately never touches
    # the network (so the settings page cannot block on the proxy), which means
    # it reports "direct" for a perfectly good workspace that simply has not
    # made its first routed call yet. `_routed` is the real production entry
    # point: it provisions on first use, then resolves.
    from src.llm.completion import _routed

    for surface in ("chat", "review", "embeddings"):
        try:
            profile = _routed(surface, workspace_id)
        except Exception as exc:  # noqa: BLE001
            out.append({"name": f"{surface} routing", "ok": False,
                        "detail": str(exc)[:200]})
            continue
        out.append({
            "name": f"{surface} routes through the proxy",
            "ok": bool(profile.via_gateway),
            "detail": (f"{profile.gateway_model} → {profile.gateway_underlying}"
                       if profile.via_gateway
                       else f"direct to {profile.provider}/{profile.model}"),
        })
    return out


# ─── helpers ─────────────────────────────────────────────────────────


def _brief(status: int, body: dict | None) -> str:
    if status == 200:
        return "200"
    return f"{status} {str((body or {}).get('error') or body or '')[:160]}"


def _provider_material(workspace_id: str) -> tuple[str, str, dict[str, str]]:
    """(provider, real api key, {surface: model}) as this workspace has them.

    Read through `resolve_profile`, so the probe exercises the models the
    workspace is actually configured for rather than a guess. `raw_api_key` is
    the tenant's REAL provider key — when the gateway is already routing, the
    profile's `api_key` is the virtual key, which the proxy cannot use to call
    the provider on its own behalf.
    """
    try:
        from src.llm.profiles import resolve_profile
    except ImportError:  # pragma: no cover — layout change
        return "google", "", {}

    provider, key = "google", ""
    models: dict[str, str] = {}
    for surface in ("chat", "embeddings"):
        try:
            profile = resolve_profile(surface, workspace_id=workspace_id)
        except Exception as exc:  # noqa: BLE001 — an unconfigured surface is data
            logger.info("gateway_verify_profile_unavailable surface=%s err=%s",
                        surface, str(exc)[:160])
            continue
        models[surface] = profile.model
        if not key:
            provider = profile.provider
            key = (profile.raw_api_key or profile.api_key or "").strip()
    return provider, key, models


def _chat(base: str, key: str, model: str) -> tuple[bool, str]:
    status, body = _admin(base, key, "POST", "/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 8,
    })
    return status == 200 and bool(body.get("choices")), f"{status}"


def _embed(base: str, key: str, model: str) -> tuple[bool, str]:
    status, body = _admin(base, key, "POST", "/embeddings",
                          {"model": model, "input": "celmis gateway probe"})
    vec = ((body.get("data") or [{}])[0] or {}).get("embedding") if body else None
    return status == 200 and bool(vec), f"{status}"


def _refused(base: str, key: str, model: str) -> tuple[bool, str]:
    status, _ = _admin(base, key, "POST", "/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
    })
    # 200 here would mean the allow-list is decorative.
    return status in (400, 401, 403), f"{status}"


def _cleanup(base: str, key: str, virtual_key: str, team: str,
             deployments: list[str]) -> bool:
    """Best-effort teardown. Reported, never raised — a probe that leaves
    debris behind is worse than one that admits it did."""
    ok = True
    try:
        if virtual_key:
            _admin(base, key, "POST", "/key/delete", {"keys": [virtual_key]})
        _, listing = _admin(base, key, "GET", "/model/info")
        by_name: dict[str, list[str]] = {}
        for row in (listing or {}).get("data") or []:
            name = str(row.get("model_name") or "")
            mid = str((row.get("model_info") or {}).get("id") or "")
            if name and mid:
                by_name.setdefault(name, []).append(mid)
        for dep in deployments:
            for mid in by_name.get(dep, []):
                _admin(base, key, "POST", "/model/delete", {"id": mid})
        _admin(base, key, "POST", "/team/delete", {"team_ids": [team]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway_verify_cleanup_failed err=%s", str(exc)[:200])
        ok = False
    return ok


__all__ = ["router"]
