#!/usr/bin/env bash
# Pull the published images and bring the stack up — run ON the server.
#
# WHY THIS EXISTS RATHER THAN A GITHUB WORKFLOW. The deploy used to run from a
# GitHub runner over ssh, which meant the repository's secrets held a root key
# to the production box. That was the only way to deliver code while the images
# were built on the server; now they come from a registry, so the server can
# fetch its own updates and the direction of trust reverses. Nothing here needs
# a credential that anyone outside this machine holds.
#
#   ./scripts/deploy-on-server.sh v0.1.0
#   ./scripts/deploy-on-server.sh            # re-deploys whatever .env names
#
# From cron, every ten minutes, deploying whatever `latest` points at:
#   */10 * * * * cd /root/celmis && ./scripts/deploy-on-server.sh >> \
#       /var/log/celmis-deploy.log 2>&1
#
# `docker compose pull` is a no-op when the digest has not moved, so a run that
# finds nothing new costs a registry round-trip and exits.
#
# FOUR THINGS THIS CARRIES that `pull && up -d` does not. Each was learned from
# an outage, and each would disappear silently if this were simplified:
#
#   1. reclaiming disk BEFORE anything writes. On a full disk the pull fails
#      too, so a cleanup step placed after it never runs.
#   2. repairing network aliases AFTER `up -d`. Compose recreates a network
#      whose definition changed and RECONNECTS the containers it is not
#      recreating — and a reconnect does not restore the service-name alias.
#      Postgres keeps its container id and answers only to `celmis-postgres`,
#      so everything that connects to `postgres` fails DNS. This has taken
#      this box down.
#   3. checking `alembic current` is at head. A migration that did not run is
#      invisible until it fails somewhere else, as something unrelated.
#   4. stamping the build, because `/api/capabilities` reads `api_version` from
#      it — and the AGPL §13 footer links to the source AT THAT VERSION. An
#      unstamped deploy offers the wrong source, which is the one thing that
#      footer exists to get right.

# NOT YET RUN ON A SERVER. Written after ssh to production became unreachable
# from here, so every claim below is derived from the workflow it replaces and
# from the outages that shaped it — not from watching this file work. The parts
# that could be checked without the box were: `bash -n`, the digest extraction
# against a real image, and the `(head)` match against real alembic output.
#
# Before trusting it in cron, run it once by hand and read what it prints:
#
#     ./scripts/deploy-on-server.sh v0.1.0
#
# The two places most likely to be wrong are the digest lookup through
# `$COMPOSE images -q`, which was verified a different way, and the final
# `curl localhost/backend/...`, which assumes the proxy from the base compose
# file is answering on port 80.
#
# Delete this comment once it has run.

set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.yml"
TAG="${1:-}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

[ -f .env ] || fail ".env is missing — run ./scripts/init-env.sh first"

if [ -n "$TAG" ]; then
  if grep -q '^CELMIS_TAG=' .env; then
    sed -i "s|^CELMIS_TAG=.*|CELMIS_TAG=$TAG|" .env
  else
    printf 'CELMIS_TAG=%s\n' "$TAG" >> .env
  fi
  log "deploying tag $TAG"
else
  log "deploying $(grep -m1 '^CELMIS_TAG=' .env || echo 'CELMIS_TAG=(unset → latest)')"
fi

# ─── 1. disk, before anything writes to it ───────────────────────────
#
# NOT `volume prune`, NOT `system prune`: Postgres, Qdrant and the LiteLLM
# database live in volumes on this box, and a cleanup step must never be able
# to take data with it. Images and layers are reproducible; volumes are not.
#
# NOT `builder prune` either, which is what used to be here: measured on this
# server it reclaims 0B, because with the containerd image store the space it
# reports under "build cache" is image layers a builder prune cannot touch. It
# ran three times per deploy and did nothing.
log "--- disk before ---"; df -h / | tail -1
docker image prune -af >/dev/null 2>&1 || true
docker container prune -f >/dev/null 2>&1 || true
log "--- disk after ---"; df -h / | tail -1

avail=$(df -Pk / | awk 'NR==2{print $4}')
if [ "$avail" -lt 2097152 ]; then
  docker system df || true
  fail "only $((avail / 1024))MB free on / after cleanup — refusing to start a deploy that will die halfway"
fi

# ─── 2. fetch and run ────────────────────────────────────────────────
log "pulling images"
$COMPOSE pull api web sandbox

# `set -e` would stop here on a failed `up -d`, on the SYMPTOM, and the alias
# repair below — the thing that fixes the usual cause — would never run.
UP_FAILED=""
$COMPOSE up -d || UP_FAILED=1

# ─── 3. network aliases ──────────────────────────────────────────────
REPAIRED=""
for svc in postgres qdrant; do
  if ! docker run --rm --network celmis_default alpine:3 \
       getent hosts "$svc" >/dev/null 2>&1; then
    log "WARNING: $svc lost its network alias on reconnect — recreating"
    $COMPOSE up -d --force-recreate "$svc" || true
    REPAIRED=1
  fi
done

# Only now, with DNS actually working, is a failed `up -d` worth retrying: the
# reason it failed has just been removed.
if [ -n "$UP_FAILED" ] || [ -n "$REPAIRED" ]; then
  log "retrying up -d after alias repair"
  $COMPOSE up -d
fi

for svc in postgres qdrant; do
  docker run --rm --network celmis_default alpine:3 \
    getent hosts "$svc" >/dev/null 2>&1 \
    || fail "$svc does not resolve by service name — the stack is up but cannot reach its own database"
done

# ─── 4. the stamp, and the migration ─────────────────────────────────
#
# The digest of the image actually running, not the tag that was asked for: a
# tag can be moved, and "which build is this" has to survive that.
digest=$(docker inspect --format '{{index .RepoDigests 0}}' \
         "$($COMPOSE images -q api | head -1)" 2>/dev/null | sed 's/.*@//' || true)
sed -i '/^CELMIS_GIT_SHA=/d;/^CELMIS_DEPLOYED_AT=/d' .env
printf 'CELMIS_GIT_SHA=%s\nCELMIS_DEPLOYED_AT=%s\n' \
  "${digest:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> .env
$COMPOSE up -d api >/dev/null

log "waiting for the api to report healthy"
for _ in $(seq 1 60); do
  status=$(docker inspect --format '{{.State.Health.Status}}' celmis-api 2>/dev/null || echo starting)
  [ "$status" = "healthy" ] && break
  sleep 5
done
[ "${status:-}" = "healthy" ] || fail "the api never became healthy — check: docker compose logs api"

docker exec celmis-api alembic current 2>&1 | tail -1 | grep -q "(head)" \
  || fail "the database is not at head — a migration did not run"

log "deployed: $(curl -fsS localhost/backend/api/capabilities 2>/dev/null \
  | grep -o '"api_version":"[^"]*"' || echo 'api_version unavailable')"
