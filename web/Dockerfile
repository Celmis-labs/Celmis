# syntax=docker/dockerfile:1.7
# Next.js 16 + pnpm — production build (standalone output).
# Stage 9 (containerization).

# ============================================================================
# STAGE 1 — deps (pnpm install)
# ============================================================================
FROM node:24-alpine AS deps

RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

COPY package.json pnpm-lock.yaml* ./
# pnpm 10+ blocks postinstall scripts by default (sharp, unrs-resolver).
# `--config.dangerouslyAllowAllBuilds=true` whitelists them — safe here
# because we control the dependency tree.
RUN pnpm install --frozen-lockfile --config.dangerouslyAllowAllBuilds=true

# ============================================================================
# STAGE 2 — builder
# ============================================================================
FROM node:24-alpine AS builder

RUN corepack enable && corepack prepare pnpm@latest --activate

WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* baked into client bundle at build time.
# Default points at host-published API (localhost:8000) — overridable.
# EMPTY BY DEFAULT, on purpose. Whatever is set here is baked into the client
# bundle and cannot be changed by the person running the image, so a default of
# `http://localhost:8000` made every published image work on one machine and
# fail everywhere else. Left empty, the browser side falls back to the relative
# `/backend`, which resolves against whatever host the app is served from.
#
# Set it only to build an image for a deployment that does NOT proxy the API on
# the same origin.
ARG NEXT_PUBLIC_API_BASE=
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
ENV NEXT_TELEMETRY_DISABLED=1

RUN pnpm build

# ============================================================================
# STAGE 3 — runtime
# ============================================================================
FROM node:24-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN apk add --no-cache curl tini

# Non-root user
RUN addgroup -g 1001 -S nodejs \
    && adduser -S -u 1001 -G nodejs nextjs

WORKDIR /app

# Copy standalone server + static assets (Next.js 16 standalone output mode)
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD curl -fsS http://localhost:3000/login >/dev/null || exit 1

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "server.js"]
