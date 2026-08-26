# LiteLLM gateway — operator runbook

The gateway is a LiteLLM proxy container that becomes the single exit door to
every LLM provider. With it on, the app process no longer holds tenants' real
provider keys on the call path: each workspace gets a *virtual* key that can
only reach that workspace's own deployments.

It is **off by default and stays off** until you deliberately turn it on. This
document is the whole procedure: what to set, in what order, how to prove it
works, how to go back, and the one thing you can never undo.

---

## Verifying without SSH

`scripts/verify_gateway.sh` needs a shell on the box. When you do not have
one, the same checks run as an API call, from the same vantage point — the
API container is inside the compose network, which is the only place
`http://litellm:4000` resolves:

```bash
# Quick yes/no: is the proxy configured, up, and actually routing?
curl -s -H "X-Ops-Token: $CELMIS_OPS_TOKEN" \
  http://<host>/backend/api/ops/gateway | python3 -m json.tool

# The full end-to-end. Needs a global-admin session, not the ops token:
# it creates a team, deployments and a scoped key (and deletes them again).
TOKEN=$(curl -s -X POST http://<host>/backend/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<CELMIS_MASTER_EMAIL>","password":"<CELMIS_MASTER_KEY>"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "http://<host>/backend/api/ops/gateway/verify?workspace_id=<ws>" \
  | python3 -m json.tool
```

The response lists every check rather than stopping at the first failure —
"the admin plane works and only the completion failed" points at the provider
key, while "team creation failed" points at the master key. `cleaned: false`
means the teardown did not finish and something was left on the proxy.

The provider key comes from that workspace's own stored credentials, not from
an environment variable, because that is where keys live: an env-level key
would be a cross-tenant fallback that every workspace without its own key
would silently spend on.

## 1. The two switches

They are independent on purpose, and both must be on:

| Switch | Where | What it controls |
|---|---|---|
| `COMPOSE_PROFILES=gateway` | server `.env` | whether the **container exists** |
| `LITELLM_PROXY_URL=http://litellm:4000` | server `.env` | whether the **API routes through it** |

The container lives behind `profiles: ["gateway"]` in `docker-compose.yml`, so
`docker compose up -d` never creates it unless the profile is active. Compose
reads `COMPOSE_*` variables from the project `.env`, which is exactly the file
the deploy workflow writes from the `DEPLOY_ENV` secret — so **turning the
gateway on never requires a change to `.github/workflows/deploy.yml`**.

Setting only `COMPOSE_PROFILES` runs the proxy but leaves the app on direct
provider calls: a safe way to bring the proxy up and verify it before any real
traffic depends on it. That is the recommended order.

---

## 2. Variables to add to the `DEPLOY_ENV` secret

Only the repository owner can edit `Settings → Secrets and variables → Actions →
DEPLOY_ENV`. Append this block to that secret; it is the full contents of the
server's `.env`, so keep everything already in it.

```dotenv
# ── LiteLLM gateway ──────────────────────────────────────────────────
# Creates the litellm container. Comma-separated; keep any profile already set.
COMPOSE_PROFILES=gateway

# Proxy admin key. MUST start with "sk-".   echo "sk-$(openssl rand -hex 24)"
LITELLM_MASTER_KEY=sk-...

# Encrypts every provider key the proxy stores.   openssl rand -hex 32
# SET ONCE — see section 6. This one can never be changed.
LITELLM_SALT_KEY=...

# Database for the proxy, on the same Postgres. Created automatically by the
# litellm-init service; you do NOT run createdb by hand.
LITELLM_DB=litellm

# Leave this one OUT on the first deploy — add it in step 2 below.
# LITELLM_PROXY_URL=http://litellm:4000
```

Notes:

* `LITELLM_PROXY_API_BASE` is set from `LITELLM_PROXY_URL` inside
  `docker-compose.yml`. Do not add it separately.
* `LITELLM_PROXY_TIMEOUT` (default 20s) is optional.
* Do not add a `ports:` mapping for the proxy. It stores every tenant's real
  provider key; it must stay reachable only from inside the compose network.
* The `.env` must be LF, not CRLF. A trailing `\r` turns `COMPOSE_PROFILES` into
  a profile named `gateway\r`, which matches nothing — the deploy's gateway
  check catches this, but it is a confusing hour if you skip the check.

---

## 3. Turning it on — order matters

**Step 1 — bring the proxy up, with the app still on direct keys.**

Add everything from section 2 *except* `LITELLM_PROXY_URL`, then push to `main`
(or run the Deploy workflow). On the server this happens:

1. `litellm-init` runs to completion first. It refuses to continue unless
   `LITELLM_MASTER_KEY` starts with `sk-` and both keys are at least 16 chars,
   then creates the `litellm` database if it is not already there. It is
   idempotent — it runs on every deploy and does nothing the second time.
2. `litellm` starts, applies its own Prisma migrations to that database, and
   loads `deploy/litellm/config.yaml`.
3. The deploy job waits for the container to report healthy and **fails the
   build if it does not**.

Nothing about the running app has changed yet.

**Step 2 — prove it works.**

```bash
ssh <server>
cd celmis
./scripts/verify_gateway.sh
```

It creates a throwaway team, deployments and a virtual key, makes one real chat
completion and one embeddings call through the proxy, checks the virtual key is
*refused* a deployment outside its allow-list, then deletes everything it made.
It prints every check and exits non-zero on the first thing that is wrong.

**Step 3 — route the app through it.**

Add `LITELLM_PROXY_URL=http://litellm:4000` to `DEPLOY_ENV` and deploy again.
From then on, the first LLM call for each workspace provisions that workspace on
the proxy (team + one deployment per surface + a scoped virtual key) and every
later call reads the cached route.

**Step 4 — confirm on a real workspace.**

Ask a question in the UI, then:

```bash
docker compose logs --tail=100 api | grep litellm_workspace_provisioned
```

---

## 4. What the app does when the proxy misbehaves

Fail-soft, everywhere. Any refusal from the proxy is logged (never with a
secret in the message) and reported as "no route", so the caller falls back to
the tenant's direct provider key. Specifically:

* proxy unreachable, or any 4xx/5xx during provisioning → no virtual key, calls
  go direct. Retried at most once a minute per workspace.
* provisioning that dies half-way (deployment deleted, recreate failed) → the
  cached route is **erased**, so calls go direct rather than pointing at a
  deployment the proxy no longer has.
* `/model/info` unreadable → provisioning is refused outright rather than
  creating a second deployment under the same name (`/model/new` appends; a
  duplicate would round-robin between the fresh provider key and the stale one).

The one thing that is deliberately loud rather than soft: an embeddings route
whose model or width no longer matches the vectors already in Qdrant raises
`EmbeddingConfigMismatch`. Silently embedding with the wrong model just makes
search quietly worse, which is far more expensive than an error.

---

## 5. Rolling back

**Stop routing through the proxy** (instant, reversible, no data touched):

remove `LITELLM_PROXY_URL` from `DEPLOY_ENV` and deploy. `src/llm/gateway.py`
requires both `LITELLM_PROXY_URL` and `LITELLM_MASTER_KEY`; with either missing
`is_enabled()` is False, every route lookup returns None, and the app is
byte-for-byte on the direct-provider path it used before. The proxy keeps
running, harmlessly.

To also stop the container, remove `COMPOSE_PROFILES=gateway` from `DEPLOY_ENV`,
deploy, then on the server:

```bash
docker compose --profile gateway stop litellm
docker compose --profile gateway rm -f litellm
```

(Compose does not stop a container just because its profile went inactive.)

The proxy's database, and every provider key stored in it, survives all of the
above. Turning the gateway back on picks up where it left off — **as long as
`LITELLM_SALT_KEY` is unchanged**.

---

## 6. The one irreversible thing: `LITELLM_SALT_KEY`

`LITELLM_SALT_KEY` encrypts every provider credential the proxy stores in its
database. It is not a session secret and it is not rotatable:

> Change `LITELLM_SALT_KEY` and every provider key already stored in the proxy
> becomes permanently unreadable. Not "re-enter it and continue" —
> unreadable. Every deployment silently starts failing auth against its
> provider.

Rules:

1. Generate it once (`openssl rand -hex 32`) and never change it.
2. Back it up with the rest of the `DEPLOY_ENV` secret. Losing it is the same
   as changing it.
3. If it ever *is* lost or changed, the recovery is: stop the proxy, drop the
   `litellm` database, set the new salt, let `litellm-init` recreate the
   database, then re-save each workspace's provider keys in LLM Setup so every
   workspace re-provisions from scratch.

`LITELLM_MASTER_KEY` is *not* in this category — it can be rotated. Change it in
`DEPLOY_ENV` and redeploy; already-minted virtual keys keep working, and the
next provisioning cycle uses the new master key.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Deploy fails: `celmis-litellm is "absent"` | profile not active on the server | `grep COMPOSE_PROFILES celmis/.env`; check for CRLF |
| `litellm-init FATAL: LITELLM_MASTER_KEY must be set...` | key missing or not `sk-` prefixed | fix `DEPLOY_ENV`, redeploy |
| `litellm-init FATAL: could not create database` | the Postgres role lacks CREATEDB | `docker compose exec postgres psql -U analyzer -c 'ALTER ROLE analyzer CREATEDB;'` |
| Container unhealthy for a few minutes on first start | first-boot Prisma migrations | expected; `start_period` is 120s |
| App logs `litellm_gateway_unreachable` | proxy down, or `LITELLM_PROXY_URL` points at the wrong host | from the API container: `curl -s http://litellm:4000/health/liveliness` |
| App logs `litellm_master_key_bad_prefix` | `LITELLM_MASTER_KEY` does not start with `sk-` | the gateway disables itself; fix the key |
| `litellm_refused_unrestricted_key` | a workspace has no usable model/provider key | configure the workspace in LLM Setup |
| Everything works but nothing is routed | `LITELLM_PROXY_URL` unset | that is step 3 |

Useful one-liners on the server:

```bash
docker logs celmis-litellm-init                 # bootstrap: env checks + createdb
docker compose logs --tail=50 litellm           # the proxy itself
docker compose exec postgres psql -U analyzer -l # is the litellm database there?
./scripts/verify_gateway.sh                     # full end-to-end proof
```

---

## 8. What the proxy is pinned to

`ghcr.io/berriai/litellm-database:v1.96.0` — pinned, never `:latest`. The admin
calls `src/llm/gateway.py` makes (`/team/new`, `/team/info`, `/model/info`,
`/model/new`, `/model/delete`, `/key/generate`, `/key/delete`) were verified
against that exact version. Before bumping the tag, re-run
`tests/llm/test_gateway_contract.py` against the new version's request models
and then `./scripts/verify_gateway.sh` on the box.
