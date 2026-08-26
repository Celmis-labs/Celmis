#!/usr/bin/env bash
#
# Prove the LiteLLM gateway actually works — run this ON THE SERVER, from the
# celmis/ directory, after a deploy with COMPOSE_PROFILES=gateway.
#
#     ./scripts/verify_gateway.sh
#
# It walks the exact path src/llm/gateway.py takes for a real tenant — team,
# deployments, a model-scoped virtual key, a chat completion, an embeddings
# call — on a throwaway workspace id, then deletes all of it. Nothing it
# creates outlives the run, and it touches no existing team, deployment or key.
#
# Secrets: the master key and the provider key are read from the environment
# the litellm container ALREADY has, inside the container. Nothing secret
# crosses the process boundary, reaches `ps`, or is printed — proxy error
# bodies echo the request back, so every one of them is scrubbed before it is
# shown. Read the driver below before you believe that.
#
# Exit code is the answer: 0 = the gateway works end to end.

set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER=celmis-litellm
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
note() { printf '  \033[33mnote\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

step "1. Configuration (server .env)"

[ -f .env ] || fail ".env not found — run this from the celmis/ directory on the server."

# Only KEYS are matched; no value is ever read into the shell.
grep -Eq '^COMPOSE_PROFILES=.*gateway' .env \
  || fail "COMPOSE_PROFILES in .env does not include 'gateway', so the litellm container is never created. Add COMPOSE_PROFILES=gateway to the DEPLOY_ENV secret and redeploy."
ok "COMPOSE_PROFILES includes 'gateway'"

grep -q '^LITELLM_MASTER_KEY=sk-' .env \
  || fail "LITELLM_MASTER_KEY is missing or not sk- prefixed (the proxy rejects anything else)."
ok "LITELLM_MASTER_KEY present and sk- prefixed"

grep -q '^LITELLM_SALT_KEY=.\{16,\}' .env \
  || fail "LITELLM_SALT_KEY is missing or shorter than 16 chars. It encrypts every provider key the proxy stores and can NEVER be rotated afterwards."
ok "LITELLM_SALT_KEY present"

if grep -q '^LITELLM_PROXY_URL=http' .env; then
  ok "LITELLM_PROXY_URL set — the API routes through the proxy"
else
  note "LITELLM_PROXY_URL is unset: the proxy gets verified, but the API is still calling providers directly."
fi

step "2. Container"

docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || fail "container $CONTAINER does not exist. Did the deploy run with COMPOSE_PROFILES=gateway? Start here: docker logs celmis-litellm-init"

state=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)
[ "$state" = healthy ] \
  || fail "container $CONTAINER health is '$state'. Logs: docker compose logs --tail=50 litellm"
ok "$CONTAINER is healthy"

restart=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")
if [ "$restart" = "unless-stopped" ]; then
  ok "restart policy: unless-stopped"
else
  note "restart policy is '$restart', expected unless-stopped — the proxy will not come back after a reboot"
fi

# The proxy holds every tenant's real provider key. It must be reachable only
# from inside the compose network.
if docker inspect -f '{{json .NetworkSettings.Ports}}' "$CONTAINER" | grep -q HostPort; then
  fail "port 4000 is PUBLISHED to the host. Remove the ports: mapping — the proxy stores every tenant's provider keys."
fi
ok "no host port published (compose network only)"

step "3. End-to-end through the proxy"

# The driver runs INSIDE the container: that is how it reaches localhost:4000
# with no published port, and how it reads both keys without them ever leaving
# the container.
docker exec -i "$CONTAINER" python3 - <<'PYEOF'
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://localhost:4000"
MASTER = (os.environ.get("LITELLM_MASTER_KEY") or "").strip()
PROVIDER_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
# Same models the bootstrap deployments use, so the check exercises what this
# install is actually configured for. Override per-run with CELMIS_VERIFY_*.
CHAT_MODEL = (os.environ.get("CELMIS_VERIFY_CHAT_MODEL")
              or os.environ.get("CELMIS_BOOTSTRAP_CHAT_MODEL") or "").strip()
EMBED_MODEL = (os.environ.get("CELMIS_VERIFY_EMBED_MODEL")
               or os.environ.get("CELMIS_BOOTSTRAP_EMBED_MODEL") or "").strip()

GREEN, RED, YELLOW, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
failures: list[str] = []


def ok(msg):
    print(f"  {GREEN}ok{OFF}    {msg}", flush=True)


def note(msg):
    print(f"  {YELLOW}note{OFF}  {msg}", flush=True)


def bad(msg):
    failures.append(msg)
    print(f"  {RED}FAIL{OFF}  {msg}", file=sys.stderr, flush=True)


def check(cond, good, wrong):
    ok(good) if cond else bad(wrong)
    return cond


def call(method, path, payload=None, key=None):
    """(status, parsed body). status 0 == the proxy could not be reached."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {key or MASTER}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw, status = resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode("utf-8", "replace"), exc.code
    except Exception as exc:                       # noqa: BLE001
        return 0, {"error": type(exc).__name__}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def brief(body, limit=200):
    """LiteLLM error bodies echo the request back, api_key included."""
    text = body if isinstance(body, str) else json.dumps(body)
    for secret in (PROVIDER_KEY, MASTER):
        if secret:
            text = text.replace(secret, "***")
    return text.replace("\n", " ")[:limit]


def deployment_ids(prefix):
    status, body = call("GET", "/model/info")
    if status != 200 or not isinstance(body, dict):
        return None
    found = {}
    for row in body.get("data") or []:
        name = str(row.get("model_name") or "")
        mid = str((row.get("model_info") or {}).get("id") or "")
        if name.startswith(prefix) and mid:
            found.setdefault(name, []).append(mid)
    return found


if not MASTER.startswith("sk-"):
    print(f"  {RED}FAIL{OFF}  the container has no usable LITELLM_MASTER_KEY", file=sys.stderr)
    sys.exit(1)

ws = f"verify-{uuid.uuid4().hex[:8]}"
team = f"ws-{ws}"
chat_deploy, embed_deploy, off_limits = (
    f"celmis-{ws}-chat", f"celmis-{ws}-embed", f"celmis-{ws}-offlimits",
)
virtual_key = None

try:
    # ── health / admin plane ─────────────────────────────────────────
    status, body = call("GET", "/health/liveliness")
    check(status == 200, "/health/liveliness answers",
          f"/health/liveliness -> {status} {brief(body)}")

    status, body = call("GET", "/model/info")
    check(status == 200 and isinstance(body, dict) and isinstance(body.get("data"), list),
          f"/model/info lists {len((body or {}).get('data') or [])} deployment(s)",
          f"/model/info -> {status} {brief(body)}")

    # ── team ─────────────────────────────────────────────────────────
    status, body = call("POST", "/team/new", {
        "team_id": team, "team_alias": f"celmis:{ws}",
        "metadata": {"celmis_workspace_id": ws},
    })
    if not check(status == 200, f"/team/new created {team}",
                 f"/team/new -> {status} {brief(body)}"):
        raise SystemExit(1)

    # The idempotency the gateway depends on: a repeat is 400, and only
    # /team/info tells that apart from a rejected request.
    status, _ = call("POST", "/team/new", {"team_id": team, "team_alias": f"celmis:{ws}"})
    check(status == 400, "a repeated /team/new is refused with 400 (the idempotent path)",
          f"a repeated /team/new returned {status}, expected 400")
    status, _ = call("GET", f"/team/info?team_id={team}")
    check(status == 200, "/team/info confirms the team exists", f"/team/info -> {status}")

    if not PROVIDER_KEY:
        note("GEMINI_API_KEY is empty in the container — the admin plane is verified, but no "
             "real completion can be made. Set it in .env, redeploy, and run this again.")
        raise SystemExit(1 if failures else 0)

    # ── deployments ──────────────────────────────────────────────────
    # `off_limits` exists only to be excluded from the virtual key: proving the
    # key is refused a model that DOES exist is the only way to show the
    # per-tenant allow-list is real.
    for name, model, extra in (
        (chat_deploy, CHAT_MODEL, {}),
        (embed_deploy, EMBED_MODEL, {"mode": "embedding"}),
        (off_limits, CHAT_MODEL, {}),
    ):
        status, body = call("POST", "/model/new", {
            "model_name": name,
            "litellm_params": {"model": model, "api_key": PROVIDER_KEY},
            "model_info": {"celmis_workspace_id": ws, **extra},
        })
        check(status == 200, f"/model/new registered {name} -> {model}",
              f"/model/new {name} -> {status} {brief(body)}")

    registered = deployment_ids(f"celmis-{ws}") or {}
    check(off_limits in registered,
          "the off-limits deployment is registered (so refusing it proves scoping, not absence)",
          "the off-limits deployment never appeared in /model/info")

    # ── virtual key ──────────────────────────────────────────────────
    status, body = call("POST", "/key/generate", {
        "models": [chat_deploy, embed_deploy],
        "team_id": team,
        "key_alias": f"celmis-{ws}",
        "metadata": {"celmis_workspace_id": ws},
    })
    virtual_key = (body or {}).get("key") if status == 200 and isinstance(body, dict) else None
    if not check(bool(virtual_key),
                 "/key/generate minted a virtual key scoped to exactly 2 deployments",
                 f"/key/generate -> {status} {brief(body)}"):
        raise SystemExit(1)

    # ── the two calls the whole thing exists for ─────────────────────
    status, body = call("POST", "/chat/completions", {
        "model": chat_deploy,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 16,
    }, key=virtual_key)
    check(status == 200 and bool((body or {}).get("choices")),
          f"chat completion routed through the proxy on the virtual key ({CHAT_MODEL})",
          f"chat completion via {CHAT_MODEL} -> {status} {brief(body)}")

    status, body = call("POST", "/embeddings", {
        "model": embed_deploy, "input": ["celmis gateway verification"],
    }, key=virtual_key)
    vec = ((body or {}).get("data") or [{}])[0].get("embedding") if status == 200 else None
    check(bool(vec),
          f"embeddings routed through the proxy ({len(vec or [])} dimensions, {EMBED_MODEL})",
          f"embeddings via {EMBED_MODEL} -> {status} {brief(body)}")

    # ── tenant isolation ─────────────────────────────────────────────
    # This allow-list IS the tenant boundary: an empty `models` on LiteLLM
    # means EVERY model, i.e. every other tenant's deployment and key.
    status, body = call("POST", "/chat/completions", {
        "model": off_limits,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4,
    }, key=virtual_key)
    check(status in (400, 401, 403),
          f"the virtual key is refused a deployment outside its list ({status})",
          f"the virtual key reached {off_limits} ({status}) — the allow-list is NOT enforced")

finally:
    print("\n  cleanup", flush=True)
    if virtual_key:
        status, _ = call("POST", "/key/delete", {"keys": [virtual_key]})
        check(status == 200, "virtual key revoked", f"/key/delete -> {status}")
    # Re-read the list rather than trusting ids captured on the way in: a
    # half-failed run must still leave nothing behind.
    leftovers = deployment_ids(f"celmis-{ws}")
    if leftovers is None:
        bad("/model/info unreadable during cleanup — check for leftover celmis-verify-* deployments")
    else:
        for name, ids in leftovers.items():
            for mid in ids:
                status, _ = call("POST", "/model/delete", {"id": mid})
                check(status == 200, f"deployment {name} deleted", f"/model/delete {name} -> {status}")
    status, _ = call("POST", "/team/delete", {"team_ids": [team]})
    check(status == 200, f"team {team} deleted", f"/team/delete -> {status}")
    check(not (deployment_ids(f"celmis-{ws}") or {}), "nothing left behind",
          "leftover celmis-verify-* deployments remain on the proxy")

sys.exit(1 if failures else 0)
PYEOF

step "Result"
ok "gateway verified end to end"
echo
echo "  Reminder: the one irreversible setting is LITELLM_SALT_KEY. Rotating it makes"
echo "  every provider key already stored in the proxy database unreadable."
