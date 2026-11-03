# syntax=docker/dockerfile:1.7
# FastAPI image для Celmis (Stage 9: containerization).
# Multi-stage build — мінімальний runtime.

# ============================================================================
# STAGE 1 — builder
# ============================================================================
FROM python:3.14-slim-trixie AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv — pinned, downloaded as a release artifact, checksum-verified per
# architecture. It replaces the piped astral.sh installer script, which had
# two independent problems and only one of them was supply chain:
#
#   * unpinned — the layer kept whatever uv was current the day it was built,
#     so the resolver drifted across rebuilds with nothing recording it;
#   * `curl | sh` — the build executed whatever that URL returned, with no
#     digest to check it against. src/deps/ flags npm packages for running
#     install scripts; running an unverified one in our own image is the same
#     finding pointed the other way.
#
# TARGETARCH is supplied by BuildKit per target platform and must be
# re-declared in every stage that reads it. It is EMPTY on the legacy builder
# and for any platform not listed below — the case arm then aborts rather than
# assuming amd64, because "assume amd64" is what produced an image whose
# osv-scanner could not execute on an arm64 host.
#
# Bump all three lines together. Astral publishes a digest next to each
# artifact:
#   https://github.com/astral-sh/uv/releases/download/<VER>/uv-<TRIPLE>.tar.gz.sha256
ARG TARGETARCH
ARG UV_VERSION=0.13.7
ARG UV_SHA256_AMD64=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83
ARG UV_SHA256_ARM64=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc

RUN set -eu; \
    case "${TARGETARCH:-}" in \
      amd64) uv_triple=x86_64-unknown-linux-gnu;  uv_sha="${UV_SHA256_AMD64}" ;; \
      arm64) uv_triple=aarch64-unknown-linux-gnu; uv_sha="${UV_SHA256_ARM64}" ;; \
      *) echo "FATAL: unsupported TARGETARCH='${TARGETARCH:-}'. uv is pinned by" \
              "checksum per architecture; add the digest for this platform" \
              "instead of letting the build guess." >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/uv.tar.gz \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_triple}.tar.gz"; \
    echo "${uv_sha}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp; \
    install -m 0755 "/tmp/uv-${uv_triple}/uv"  /usr/local/bin/uv; \
    install -m 0755 "/tmp/uv-${uv_triple}/uvx" /usr/local/bin/uvx; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${uv_triple}"; \
    uv --version

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Installed FROM THE LOCK, not resolved at build time. `uv pip install -e .`
# took whatever was newest inside pyproject's ranges on the day the layer was
# built, so two builds of one commit could differ and a vulnerable release
# (urllib3 2.7.0, msgpack 1.1.2 were found in a shipped image) arrived without
# any change in the repository. `--frozen` refuses to touch uv.lock, and the
# lock carries a hash for every file, which uv checks on install. CI runs
# `uv lock --check`, so a pyproject change without a new lock fails there.
# Bump with `uv lock --upgrade`.
ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-dev --no-cache

# pip-audit — native Python auditor for the dependency audit (src/deps/native.py).
# Deliberately its OWN venv: it pins packaging/requests/cyclonedx versions of its
# own, and letting those resolve against the application venv is how a security
# tool ends up breaking the app it audits. Only its entrypoint goes on PATH.
# The floors keep a resolver from picking a vulnerable release for it
# (msgpack < 1.2.3, urllib3 < 2.8.0): same numbers as pyproject's [tool.uv].
RUN uv venv /opt/pip-audit \
    && VIRTUAL_ENV=/opt/pip-audit uv pip install --no-cache pip-audit \
        "msgpack>=1.2.4" "urllib3>=2.8.0"

# ============================================================================
# STAGE 1b — osv-scanner, compiled with a current Go (see the note in runtime)
# ============================================================================
# golang:1.27.3-bookworm, pinned by digest (the multi-arch index).
FROM --platform=$BUILDPLATFORM golang:1.27.3-bookworm@sha256:d9b6a1beb9474adc2a1ce842573639b44b2a0dd49b6669f51d3821baf9ec37c9 AS osv-build
ARG TARGETARCH
ARG OSV_SCANNER_VERSION=2.7.0
ENV CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} GOTOOLCHAIN=local
RUN set -eu; \
    go install -trimpath -ldflags "-s -w" \
        "github.com/google/osv-scanner/v2/cmd/osv-scanner@v${OSV_SCANNER_VERSION}"; \
    install -D -m 0755 "$(find /go/bin -type f -name osv-scanner | head -n1)" /out/osv-scanner; \
    go version -m /out/osv-scanner | head -n 2

# ============================================================================
# STAGE 2 — runtime
# ============================================================================
FROM python:3.14-slim-trixie AS runtime

# Required by the official MCP registry to prove this image is the one a
# server.json package entry claims it is (checked at publish time against
# ghcr.io, not just asserted in the JSON). One label, one namespace: the
# registry rejects a mismatch rather than trusting the manifest.
LABEL io.modelcontextprotocol.server.name="io.github.constantinemakoid/celmis"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Workspace + vault — mount points усередині container
    WORKSPACE_DIR=/workspace/data \
    VAULT_DIR=/workspace/vault \
    # Git: no prompts
    GIT_TERMINAL_PROMPT=0

# Runtime deps:
#   git    — clone/pull operations
#   grep   — cross-repo drift detector (subprocess grep -rnF)
#   tini   — PID 1 (signal handling)
#   curl   — healthcheck
#   nodejs — Claude Code CLI runtime (embedded agent sessions) AND `npm audit`,
#            the native npm-ecosystem auditor used by the dependency audit
#
# Native auditors NOT installed, and why (the audit reports each as
# `not_checked` with the reason, never as a silent zero):
#   govulncheck  — needs the full Go toolchain (~500 MB); Go repos fall back to
#                  go.sum + OSV, which covers the same modules without the
#                  call-graph precision
#   cargo audit  — needs the Rust toolchain; Cargo.lock + OSV is the fallback
#   composer / bundler-audit — need PHP and Ruby runtimes respectively
#   pnpm / yarn  — not installed here; a pnpm/yarn repo falls back to its lock
#                  file + OSV
# What closes those gaps without any of those toolchains is osv-scanner below:
# it reads lock files instead of running the ecosystem's own resolver, so one
# 55 MB binary audits Java (Maven/Gradle), .NET (NuGet), Ruby, PHP/Composer,
# Dart/Pub, Elixir/Hex, Haskell, R/CRAN, Swift, CocoaPods, Conan (C/C++) and
# Alpine/Debian/RPM packages as well as the four ecosystems above.

# No Claude Code CLI is installed here, and that is a correction rather than an
# omission.
#
# `npm install -g @anthropic-ai/claude-code@2.1.228` used to live below. It was
# never executed: claude_agent_sdk ships its own copy and `_find_cli` returns
# the bundled one BEFORE consulting PATH, so every session ran 2.1.233 from the
# wheel while this pinned 2.1.228 sat unused — 310 MB of image on a 9 GB disk,
# and /api/ops/diag reporting the version that does not run.
#
# The SDK version in pyproject.toml is therefore what pins the CLI. src/ops/
# build.py reads it back from the SDK rather than from a binary on PATH.
#
# nodejs itself STAYS. It is not there for the agent — `src/deps/native.py`
# looks up `npm` with shutil.which and runs `npm audit` as the native auditor
# for the npm ecosystem, so removing the runtime along with the package would
# have quietly downgraded every JavaScript dependency audit to the OSV
# fallback.

# osv-scanner — the universal dependency auditor (src/deps/osv_scanner.py),
# version-pinned in stage 1b: an unpinned one ages out of its own advisory
# matching while every deploy reports success.
# npm — the one NodeSource ships is older than the fixes for the advisories in
# its own bundled packages (tar, glob, ...), and `npm audit` is exactly what
# this image uses it for. Pinned, so an image never changes under a deploy; a
# version whose `engines` the installed node does not meet fails the build here
# instead of at the first audit.
ARG NPM_VERSION=12.2.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        grep \
        tini \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --no-fund --no-audit "npm@${NPM_VERSION}" \
    && npm --version \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# osv-scanner is BUILT here rather than downloaded. Every published release
# (2.6.0 and 2.7.0 included) is compiled with Go 1.27.0, whose standard library
# carries advisories fixed in 1.27.2; an image scan reports them against the
# binary however current the scanner itself is. Compiling the same tagged source
# with a current toolchain removes them, and the release's own SHA256SUMS would
# only prove we downloaded the vulnerable build.
#
# What replaces the per-architecture checksum:
#   * the compiler is the pinned image digest in stage 1b;
#   * the source is the tag `v${OSV_SCANNER_VERSION}`, resolved by the Go module
#     proxy and verified against the public checksum database (GONOSUMDB is not
#     set), and every dependency is checked against that module's go.sum;
#   * it is cross-compiled for TARGETARCH, so arm64 needs no emulation.
# Switch back to a downloaded, checksummed binary once a release is built with
# Go >= 1.27.2 (see the lines this stage replaced in git history).
#
# `--version` is a real gate, not decoration: a wrong architecture would
# otherwise only surface as "not installed in this image" on the first audit.
COPY --from=osv-build /out/osv-scanner /usr/local/bin/osv-scanner
RUN osv-scanner --version
# Non-root user
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd -g ${USER_GID} celmis \
    && useradd -u ${USER_UID} -g ${USER_GID} -m -s /bin/bash celmis

WORKDIR /app

COPY --from=builder --chown=celmis:celmis /opt/venv /opt/venv
COPY --from=builder --chown=celmis:celmis /opt/pip-audit /opt/pip-audit
RUN ln -sf /opt/pip-audit/bin/pip-audit /usr/local/bin/pip-audit

ENV PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    PYTHONPATH=/app

COPY --chown=celmis:celmis src/ ./src/
COPY --chown=celmis:celmis alembic/ ./alembic/
COPY --chown=celmis:celmis alembic.ini pyproject.toml ./

# Mount points + permissions
RUN mkdir -p /workspace/data /workspace/vault \
    && chown -R celmis:celmis /workspace /app

USER celmis

VOLUME ["/workspace/data", "/workspace/vault"]

EXPOSE 8000

# Healthcheck — /healthz, the liveness route.
#
# This was /docs, and it stopped being able to pass the day Swagger was put
# behind a session: `_mount_private_docs` requires `get_current_user`, so
# /docs answers 401 to an anonymous caller and `curl -f` exits non-zero on a
# 401. Verified against production: GET /backend/docs → 401.
#
# NOT A LIVE OUTAGE, and the distinction is worth writing down because the
# commit that introduced this said otherwise. `docker-compose.yml` already
# overrides the healthcheck with /healthz — with the same reasoning, written
# out at the same length — and a compose override wins. Production reports
# healthy and always has. What was broken is the IMAGE: anybody running it
# with plain `docker run`, or under a compose file without that override,
# gets a container permanently marked unhealthy while it serves every request
# perfectly.
#
# So this closes a gap between the image and the compose file rather than
# fixing an incident. Two copies of one decision had drifted, and the wrong
# copy is the one a new deployment starts from.
#
# /healthz, not /readyz: this answers "is the process serving HTTP", which is
# what a container healthcheck is for. /readyz checks Postgres, Qdrant and the
# LLM configuration, and a container that restarts because Qdrant is briefly
# unreachable turns one dependency's blip into an outage of everything else.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD curl -fsS http://localhost:8000/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default — uvicorn без --reload (production-like).
# Для dev mode override `command:` у docker-compose.override.yml.
# Forwarded headers are believed only from 127.0.0.1 here (uvicorn's default).
# Behind a proxy set FORWARDED_ALLOW_IPS to the proxy's address or network -
# never `*` - or use the bundled docker-compose.yml, which works it out
# (src/api/proxy_trust.py). Without it, redirects fall back to http.
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
