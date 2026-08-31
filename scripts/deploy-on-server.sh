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

# ─── 3b. the sandbox must not be able to knock on the host ───────────
#
# Measured from inside the sandbox, running as the code under review would:
# `172.17.0.1:22` was OPEN. That is the docker bridge gateway — the host
# itself, and with it anything the host has bound.
#
# Outbound internet from the sandbox is DELIBERATE and stays: `pip install`
# and `npm ci` need it, and a sandbox that cannot install dependencies cannot
# run a test suite. That is exactly why this cannot live in compose. Docker
# has no switch for "reach the internet but not the router you reach it
# through", because from the container both are the same next hop. Only the
# host firewall sees the difference: traffic TO the host arrives on INPUT,
# traffic THROUGH it to the internet arrives on FORWARD. Dropping the first
# leaves the second alone.
#
# Idempotent, and a missing iptables is a warning rather than a failure — a
# deploy must not stop over a hardening rule on a host that does not use
# iptables at all (nftables-only, a managed runtime, a rootless daemon).
# `|| true` because grep exits 1 when the setting is simply absent, and under
# `set -euo pipefail` a failing substitution in an assignment ends the deploy —
# three lines after the comment that says a deploy must not stop over a
# hardening rule, and one line before the default written for exactly this
# case. It aborted AFTER `up -d`: new containers running, version unstamped,
# health never checked.
SANDBOX_SUBNET="$(grep -E '^SANDBOX_NET_SUBNET=' .env 2>/dev/null \
                    | cut -d= -f2- | tr -d '[:space:]' || true)"
: "${SANDBOX_SUBNET:=172.28.90.0/24}"
# THIS STEP IS NOT OPTIONAL, and it used to say so while behaving otherwise:
# both failure branches logged a WARNING and carried on, five lines after a
# real `fail` on a missing .env. A deploy that cannot isolate the sandbox and
# proceeds anyway ships a container that runs a tenant's own build commands
# with a route to this host.
#
# Two backends, then a stop. `iptables` first because that is what the rule is
# written in; `nft` because a modern host may have nothing else — measured on
# this box: nftables v1.0.9 present. If neither works the deploy fails, and an
# operator who has thought about it says so out loud with
# CELMIS_ALLOW_UNFIREWALLED_SANDBOX=1 — a typed decision rather than a warning
# nobody reads.
block_sandbox_to_host() {
  local subnet="$1"
  if command -v iptables >/dev/null 2>&1; then
    if iptables -C INPUT -s "$subnet" -j DROP 2>/dev/null; then
      log "sandbox→host already blocked ($subnet, iptables)"
      return 0
    fi
    if iptables -I INPUT 1 -s "$subnet" -j DROP 2>/dev/null; then
      log "blocked sandbox→host ($subnet, iptables); internet egress untouched"
      return 0
    fi
  fi
  if command -v nft >/dev/null 2>&1; then
    nft list table inet celmis >/dev/null 2>&1 || \
      nft add table inet celmis 2>/dev/null || true
    nft list chain inet celmis input >/dev/null 2>&1 || \
      nft "add chain inet celmis input { type filter hook input priority 0; }" \
        2>/dev/null || true
    if nft list chain inet celmis input 2>/dev/null | grep -q "$subnet"; then
      log "sandbox→host already blocked ($subnet, nft)"
      return 0
    fi
    if nft add rule inet celmis input ip saddr "$subnet" drop 2>/dev/null; then
      log "blocked sandbox→host ($subnet, nft); internet egress untouched"
      return 0
    fi
  fi
  return 1
}

if ! block_sandbox_to_host "$SANDBOX_SUBNET"; then
  if [ "${CELMIS_ALLOW_UNFIREWALLED_SANDBOX:-}" = "1" ]; then
    log "WARNING: sandbox→host is NOT blocked and CELMIS_ALLOW_UNFIREWALLED_SANDBOX=1 \
was set — continuing on the operator's explicit decision"
  else
    fail "could not block sandbox→host ($SANDBOX_SUBNET) with iptables or nft. \
The execution sandbox runs a tenant's own build and test commands; without \
this rule it can reach this host. Install one of them, or set \
CELMIS_ALLOW_UNFIREWALLED_SANDBOX=1 if this host isolates it another way."
  fi
fi

# THE RULE DOES NOT SURVIVE A REBOOT. Measured on the production box: the rule
# was present, `iptables-persistent` was not, and crontab was EMPTY — so
# nothing would reinstate it. The window after a restart is not "until the next
# scheduled deploy", it is until somebody deploys by hand.
#
# Before=docker.service so the rule exists before anything can be started into
# that subnet.
if command -v systemctl >/dev/null 2>&1; then
  cat > /etc/systemd/system/celmis-sandbox-firewall.service <<UNIT
[Unit]
Description=Block the Celmis execution sandbox from reaching this host
Before=docker.service
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -C INPUT -s ${SANDBOX_SUBNET} -j DROP 2>/dev/null || iptables -I INPUT 1 -s ${SANDBOX_SUBNET} -j DROP'

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload >/dev/null 2>&1 || true
  if systemctl enable celmis-sandbox-firewall.service >/dev/null 2>&1; then
    log "sandbox firewall will be reinstated at boot (systemd unit enabled)"
  else
    log "WARNING: could not enable celmis-sandbox-firewall.service — the rule \
is in place now but will not survive a reboot"
  fi
else
  log "WARNING: no systemctl — the sandbox firewall rule will not survive a reboot"
fi

# ─── 4. the stamp, and the migration ─────────────────────────────────
#
# The COMMIT the running image was built from, not the tag that was asked for:
# a tag can be moved, and "which build is this" has to survive that. It comes
# from the image's own `revision` label, which `release.yml` sets.
#
# The first version stamped the image DIGEST — equally immutable and useless
# here. `src/ops/build.py` shortens this value to seven characters, so
# `sha256:150805…` displayed as the string "sha256:", and the AGPL §13 footer
# offered source at `…/tree/sha256:`, which 404s. A digest names an image; the
# footer needs something a git host can resolve. Reading the code, the digest
# looked right; the first real run on the server is what showed it was not.
rev=$(docker inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "$($COMPOSE images -q api | head -1)" 2>/dev/null || true)
# An image built before those labels existed carries none — fall back to the
# digest rather than stamping nothing, and accept that its short form is noise.
digest=${rev:-$(docker inspect --format '{{index .RepoDigests 0}}' \
         "$($COMPOSE images -q api | head -1)" 2>/dev/null | sed 's/.*@sha256://' || true)}
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
