# syntax=docker/dockerfile:1.7
# FastAPI image для Celmis (Stage 9: containerization).
# Multi-stage build — мінімальний runtime.

# ============================================================================
# STAGE 1 — builder
# ============================================================================
FROM python:3.13-slim-trixie AS builder

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
ARG UV_VERSION=0.12.5
ARG UV_SHA256_AMD64=68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2
ARG UV_SHA256_ARM64=9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31

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
COPY pyproject.toml ./
COPY src/ ./src/

ENV VIRTUAL_ENV=/opt/venv
RUN uv venv /opt/venv \
    && uv pip install --no-cache -e .

# pip-audit — native Python auditor for the dependency audit (src/deps/native.py).
# Deliberately its OWN venv: it pins packaging/requests/cyclonedx versions of its
# own, and letting those resolve against the application venv is how a security
# tool ends up breaking the app it audits. Only its entrypoint goes on PATH.
RUN uv venv /opt/pip-audit \
    && VIRTUAL_ENV=/opt/pip-audit uv pip install --no-cache pip-audit

# ============================================================================
# STAGE 2 — runtime
# ============================================================================
FROM python:3.13-slim-trixie AS runtime

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

# osv-scanner — the universal dependency auditor (src/deps/osv_scanner.py).
# Pinned for the same reason as the CLI above, plus one specific to a security
# tool: an unpinned download in a cached layer keeps whatever was current the
# day the layer was built, so the scanner silently ages out of its own
# advisory-matching fixes while every deploy reports success. The checksum is
# pinned with it — the binary arrives over the network from a release page, and
# "we downloaded something" is not the same as "we downloaded this".
# Bump ALL THREE lines together; one SHA256SUMS file lists every artifact:
#   https://github.com/google/osv-scanner/releases/download/vX.Y.Z/osv-scanner_SHA256SUMS
ARG OSV_SCANNER_VERSION=2.5.0
ARG OSV_SCANNER_SHA256_AMD64=edcfc41d257db36148f065055655fe3fcfc434b0b423ea67468a84c207524e0c
ARG OSV_SCANNER_SHA256_ARM64=fe152e1a546af223e6c557cc3111a8bb3e5dc02fcbf7dbe95d26567c0f0041f2

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        grep \
        tini \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# A single static Go binary — no archive to unpack, no toolchain to install,
# but it IS architecture-specific. This line named the amd64 artifact
# outright, so on the arm64 hosts both deployment guides recommend
# (docs/HETZNER.md picks CAX21, docs/ORACLE_CICD.md builds natively on an
# Ampere A1) the download succeeded, the checksum matched, and `--version`
# then failed with "exec format error" — the documented setup could not build
# at all. The name is now derived from TARGETARCH, whose values (`amd64`,
# `arm64`) happen to be exactly the suffixes this project publishes.
#
# `--version` is a real gate, not decoration: a truncated download or a wrong
# architecture would otherwise only surface as "not installed in this image"
# on the first audit, months later.
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-}" in \
      amd64) osv_sha="${OSV_SCANNER_SHA256_AMD64}" ;; \
      arm64) osv_sha="${OSV_SCANNER_SHA256_ARM64}" ;; \
      *) echo "FATAL: unsupported TARGETARCH='${TARGETARCH:-}'. osv-scanner is" \
              "pinned by checksum per architecture; add the digest for this" \
              "platform instead of letting the build guess." >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/osv-scanner \
        "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_${TARGETARCH}"; \
    echo "${osv_sha}  /tmp/osv-scanner" | sha256sum -c -; \
    install -m 0755 /tmp/osv-scanner /usr/local/bin/osv-scanner; \
    rm -f /tmp/osv-scanner; \
    osv-scanner --version

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
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
