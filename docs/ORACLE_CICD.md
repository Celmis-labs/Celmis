# Running Celmis on an Oracle Always Free box

**This page described a pipeline that no longer exists.** It said a push to
`main` made GitHub Actions rsync the repository to the box and run
`docker compose up -d --build` **there**. That is what
`.github/workflows/release.yml` was written to replace: building on the
production box took 485 seconds and 4.2GB of a disk with 4.2GB free, reused no
cache, and the rented runner was billed for the minutes it spent watching over
SSH — which is what exhausted the account's free Actions minutes. There is no
rsync and no `--build` in any workflow now, and no "Deploy to Oracle" workflow
to run.

What happens instead: images are built **once, on a tag**, published to GHCR,
and pulled by the box. Deploying is one command on the box.

```
git tag v0.1.x ──▶ Release images (GitHub-hosted, amd64+arm64) ──▶ ghcr.io
                                                                     │
                        on the box:  scripts/deploy-on-server.sh v0.1.x
                                     └─ docker compose pull + up -d
                                     └─ Caddy → HTTP
```

A user installs by pulling three images rather than building them, which is
the point of the change; the Oracle box is just one more place that pulls.

Files in the repo: [scripts/deploy-on-server.sh](../scripts/deploy-on-server.sh),
[deploy/oracle/caddy-http.yml](../deploy/oracle/caddy-http.yml),
[deploy/oracle/Caddyfile.http](../deploy/oracle/Caddyfile.http).

> **These are the files the pipeline actually uses.** This page used to name
> `caddy.yml` / `Caddyfile` — a TLS overlay the workflow has never referenced.
> The deploy runs
> `docker compose -f docker-compose.yml -f deploy/oracle/caddy-http.yml`
> (in the deploy workflow this document predates), and three superseded
> overlays that sat beside it —
> `caddy.yml`, `caddy-ip.yml`, `caddy-sslip.yml` with their Caddyfiles — have
> been deleted. One of them hardcoded the production IP.
>
> So the box serves **HTTP**, not HTTPS. `tests/security/
> test_no_service_faces_the_internet.py` reads the same overlay this line
> names, which is what keeps the two from drifting apart again.

> **arm64 is a supported build target, not a hope.** The API image pulls two
> pinned binaries — `uv` in the builder stage, `osv-scanner` in the runtime
> stage — and both are now selected from BuildKit's `TARGETARCH` with a
> separate SHA-256 per architecture. `osv-scanner` used to be hardcoded to
> `_linux_amd64`, so the native arm64 build this page describes downloaded an
> x86 binary, matched its checksum, and then died at the `osv-scanner
> --version` gate with `exec format error`. An unknown `TARGETARCH` now stops
> the build instead of assuming amd64 — see
> [DEPLOY_AND_TESTING.md § 1.8](DEPLOY_AND_TESTING.md#18-під-які-архітектури-збирається-образ)
> for the bump procedure.

---

## 1. Prepare the Oracle box (one-time)

Oracle Always Free Ampere A1 (arm64), Ubuntu 24.04. SSH in as the image's
default user (`ubuntu` on the Ubuntu image; `opc` on Oracle Linux).

### Docker + swap
```bash
# Docker Engine + compose (official repo)
sudo apt update && sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker

# Swap — insurance for the Next.js build (12 GB box is fine, but cheap safety)
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Open ports — Oracle has TWO firewalls
1. **VCN Security List / NSG** (cloud console): add ingress rules for TCP **80**, **443** (and **22**) from `0.0.0.0/0` (and `::/0`).
2. **Host iptables** (Oracle's Ubuntu image blocks everything but 22 locally):
```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```
Both must allow 80/443 or Caddy's certificate challenge fails.

### CI deploy key
Give Actions an SSH key to reach the box:
```bash
# on the box (or reuse an existing keypair):
ssh-keygen -t ed25519 -f ~/ci_deploy -N ''
cat ~/ci_deploy.pub >> ~/.ssh/authorized_keys
cat ~/ci_deploy          # <-- PRIVATE key: copy into the GitHub secret below, then delete it
rm ~/ci_deploy ~/ci_deploy.pub
```

### App directory + .env
```bash
mkdir -p ~/celmis
nano ~/celmis/.env        # paste the production .env (template below)
```
`.env` is never synced or committed — it lives only on the box.

---

## 2. Production `.env` (on the box)

```dotenv
# --- secrets ---
GEMINI_API_KEY=...
QDRANT_URL=...
QDRANT_API_KEY=...
POSTGRES_PASSWORD=...
CELMIS_JWT_SECRET=...        # python -c "import secrets;print(secrets.token_urlsafe(48))"
MCP_JWT_SECRET=...           # another one
NEXTAUTH_SECRET=...          # another one

# --- domains / URLs ---
APP_DOMAIN=app.example.com
API_DOMAIN=api.example.com
ACME_EMAIL=you@example.com
NEXTAUTH_URL=https://app.example.com
NEXT_PUBLIC_API_BASE=https://api.example.com   # baked into the web build
API_BASE_INTERNAL=http://api:8000              # server-side, over the compose network
CELMIS_CORS_ORIGINS=https://app.example.com
CELMIS_TRUST_PROXY=1

# --- keep web/api off the public interface; only Caddy is exposed ---
# The PORT only. docker-compose.yml already writes the loopback address in
# front of it — `"127.0.0.1:${API_HOST_PORT:-8000}:8000"` — so an address
# here is a second one, and `docker compose config` refuses the file with
# `invalid IP address: 127.0.0.1:127.0.0.1`. This page used to say
# `API_HOST_PORT=127.0.0.1:8000`, which does not start.
API_HOST_PORT=8000
WEB_HOST_PORT=3000
```

---

## 3. GitHub secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ORACLE_HOST` | box public IP (or a domain pointing at it) |
| `ORACLE_USER` | `ubuntu` (Ubuntu image) or `opc` (Oracle Linux) |
| `ORACLE_SSH_KEY` | the **private** key from step 1 (whole file, incl. header/footer lines) |

---

## 4. DNS

At your DNS provider, point both subdomains at the box:

| Type | Name | Value |
|---|---|---|
| A | `app` | `<oracle-public-ip>` |
| A | `api` | `<oracle-public-ip>` |
| AAAA | `app` / `api` | `<oracle-ipv6>` (optional) |

Set these before the first deploy if you intend to put TLS in front of the
box later; the overlay named above serves HTTP and obtains no certificate.

---

## 5. Go

```bash
# on your machine: cut the release, and let Actions build and publish it
git tag -a v0.1.x -m v0.1.x && git push origin v0.1.x

# on the box: pull that tag and bring the stack up
cd /root/celmis && git fetch --tags && git reset --hard origin/main
bash scripts/deploy-on-server.sh v0.1.x
```

The script prints the version it settled on and waits for the API to report
healthy, so a deploy that did not take says so rather than looking finished.
Then open `http://<your-box>/signup` — **the first user becomes admin** →
Settings → LLM to add keys (they persist in the `/workspace` volume across
redeploys).

**HTTP, not HTTPS.** The overlay this page names serves plain HTTP and the TLS
overlays that used to sit beside it were deleted; `tests/security/
test_no_service_faces_the_internet.py` reads the same overlay, which is what
keeps the two from drifting again. Section 4's DNS records are worth setting
anyway, but nothing here obtains a certificate — that needs a TLS overlay you
supply.

---

## Alternatives

- **No YAML at all:** install **Dokploy** or **Coolify** on the box, connect the
  repo in its UI → it reads `docker-compose.yml` and auto-redeploys on push via
  webhook, and brings its own Traefik + TLS. Same result, no Actions file.
- **Self-hosted runner:** register a GitHub Actions runner ON the box
  (`runs-on: [self-hosted, ARM64]`) and skip SSH entirely. Great for a **private**
  repo; avoid on a public repo (fork PRs could run on your box).
- **GitLab CI:** identical shape — a job that installs an SSH key from CI
  variables and runs `ssh … scripts/deploy-on-server.sh <tag>`. Build once and
  pull; building on the box is the thing this setup moved away from.
- **First-run tip:** on a fresh box the deploy needs `.env` present (step 2) or
  it fails on missing secrets. Create it before the first deploy.
