# Деплой Celmis на Hetzner Cloud

На відміну від Railway, Hetzner — це звичайний VPS, тож `docker-compose.yml` працює **напряму**. Треба лише додати reverse-proxy з авто-HTTPS (Caddy) і зробити базовий hardening. У репо вже є turnkey-оверлей: [deploy/hetzner/](../deploy/hetzner/) (Caddy override + Caddyfile + addendum до `.env`).

Актуальні дані — **липень 2026** (після репрайсингу Hetzner 15 червня 2026).

---

## 1. Який сервер брати

Стек: `api` (FastAPI/uvicorn) + `web` (Next.js standalone) + `postgres`. Qdrant — зовнішній (Cloud), не на цій машині. Резидентно стек їсть ~1.5–2.5 GB; **вузьке місце — білд Next.js** (`next build` любить памʼять).

| Роль | vCPU / RAM | Hetzner | €/міс* | Чому |
|---|---|---|---|---|
| **Рекомендовано** | 4 / 8 GB | **CAX21** (ARM) | **10.49** | Білд on-box із запасом, комфортно на десятки юзерів. Найдешевші 8 GB після червня 2026 |
| x86-альтернатива | 4 / 8 GB | CX33 (Intel) | 8.49 | Якщо принципово x86 |
| Бюджет | 2 / 4 GB | CAX11 (ARM) | 5.99 | **Лише** зі swap + `--max-old-space-size`, або білд off-box (CI→registry). Сервінг тягне, білд — ризик OOM |

\* net, без VAT, EU-локації (Falkenstein/Nuremberg/Helsinki), 20 TB трафіку включено. **+€0.50/міс за IPv4** (або IPv6-only безкоштовно). US/Singapore — трохи дорожче й лише ~1 TB трафіку.

**ARM (CAX) — правильний вибір у 2026:** усі нативні залежності мають arm64-колеса (`asyncpg`, `pydantic-core`, `cryptography`, `tree-sitter`, `qdrant-client`), api-образ уже на `python:3.13-slim` (glibc, не alpine — максимум готових коліс), web на `node:24-alpine`. А головне — CAX/CX подорожчали лише на ~30–38%, тоді як **CPX/CCX стрибнули на +144–176%** (CPX32 тепер €35.49 проти €8.49 у CAX21). CPX/CCX зараз брати немає сенсу.

> Довідка цін (усі shared-vCPU): CAX11 €5.99 · CAX21 €10.49 · CAX31 (8/16) €20.99 · CX23 €5.49 · CX33 €8.49 · CX43 (8/16) €15.99. Джерела: [CostGoat](https://costgoat.com/pricing/hetzner), [Northflank](https://northflank.com/blog/hetzner-cloud-server-price-increases), [Hetzner Cloud](https://www.hetzner.com/cloud/).

**Образ збирається під arm64 нативно — це перевірено, а не припущення.**
`Dockerfile` тягне два пінованих бінарники (`uv` у builder-стадії,
`osv-scanner` у runtime), і обидва тепер вибираються за `TARGETARCH`, з
окремою SHA-256 на кожну архітектуру. Доти `osv-scanner` був захардкожений як
`_linux_amd64`: на CAX білд качав x86-бінарник, звіряв його суму — і падав уже
на `osv-scanner --version` з `exec format error`. Тобто рекомендація в цій
таблиці суперечила самому Dockerfile. Невідомий `TARGETARCH` тепер зупиняє
білд, а не бере amd64 мовчки — процедура бампу в
[DEPLOY_AND_TESTING.md § 1.8](DEPLOY_AND_TESTING.md#18-під-які-архітектури-збирається-образ).

---

## 2. Створити сервер

Hetzner Cloud Console → Add Server:
- **Image:** Ubuntu 24.04 LTS
- **Type:** CAX21 (ARM) — або з таблиці вище
- **SSH key:** додай свій публічний ключ (root одразу key-only)
- **Firewall:** прикріпи Cloud Firewall (створи, якщо нема) — inbound лише **TCP 22, 80, 443** (+ ICMP за бажанням), джерела `0.0.0.0/0` і `::/0`. Це edge-фаєрвол, тому порти api/web (8000/3000) ззовні недосяжні, навіть якщо Docker їх опублікує.
- **IPv4:** лишити (+€0.50/міс) або IPv6-only для економії (тоді DNS лише AAAA).

---

## 3. Hardening (перші 5 хвилин)

```bash
ssh root@SERVER_IP

# не-root sudo-юзер із тим самим SSH-ключем
adduser deploy && usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy

# у ДРУГОМУ терміналі перевір: ssh deploy@SERVER_IP && sudo whoami  → root

# sshd: лише ключі, без root-логіну
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl restart ssh

# fail2ban + автооновлення безпеки
sudo apt update && sudo apt install -y fail2ban unattended-upgrades
sudo tee /etc/fail2ban/jail.local >/dev/null <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 5
bantime = 1h
EOF
sudo systemctl enable --now fail2ban
sudo dpkg-reconfigure -plow unattended-upgrades   # → Yes
```

Cloud Firewall — головний рубіж. `ufw` на хості — опційно, як другий шар (пропусти 22/80/443).

---

## 4. Docker (офіційний apt-репо, актуально 2026)

```bash
sudo apt install -y ca-certificates curl
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
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy && newgrp docker
docker compose version
```

---

## 5. Swap (обовʼязково на 4 GB, бажано на 8 GB)

`next build` може впасти по OOM на 4 GB. Swap надійно рятує (білд повільніший, але завершиться).

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
printf 'vm.swappiness=10\nvm.vfs_cache_pressure=50\n' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl --system && free -h
```

На 4 GB також додай у `.env`: `NODE_OPTIONS=--max-old-space-size=3072`. Ідеально — білдити образи off-box (CI → registry) і на сервері лише `pull`; тоді памʼять узагалі не проблема.

---

## 6. DNS

До підняття Caddy створи записи на обидва сабдомени (щоб ACME-челендж пройшов):

| Type | Host | Value |
|---|---|---|
| A | `app` | SERVER_IPv4 |
| A | `api` | SERVER_IPv4 |
| AAAA | `app` | SERVER_IPv6 *(якщо IPv6)* |
| AAAA | `api` | SERVER_IPv6 |

Cloudflare — постав записи **DNS-only (сіра хмара)**, поки Caddy не отримає серт.

---

## 7. Деплой

```bash
# GITHUB_OWNER — власник репозиторію (твій GitHub-акаунт або організація).
# SSH-форма потребує ключа на GitHub; без нього бери HTTPS:
#   git clone https://github.com/GITHUB_OWNER/Celmis.git ~/celmis && cd ~/celmis
git clone git@github.com:GITHUB_OWNER/Celmis.git ~/celmis && cd ~/celmis

# .env: секрети + Hetzner-addendum
cp deploy/hetzner/.env.hetzner.example .env
python3 -c "import secrets;print('CELMIS_JWT_SECRET='+secrets.token_urlsafe(48))" >> .env
python3 -c "import secrets;print('MCP_JWT_SECRET='+secrets.token_urlsafe(48))" >> .env
python3 -c "import secrets;print('NEXTAUTH_SECRET='+secrets.token_urlsafe(48))" >> .env
# далі відредагуй .env: APP_DOMAIN/API_DOMAIN/ACME_EMAIL, GEMINI_API_KEY,
# QDRANT_URL, QDRANT_API_KEY, POSTGRES_PASSWORD, і встав реальні домени в
# NEXTAUTH_URL / NEXT_PUBLIC_API_BASE / CELMIS_CORS_ORIGINS
nano .env

# білд + підняття зі стеком Caddy (з кореня репо!)
docker compose -f docker-compose.yml -f deploy/hetzner/docker-compose.hetzner.yml up -d --build

docker compose logs -f caddy   # чекай "certificate obtained successfully"
```

`NEXT_PUBLIC_API_BASE` вшивається у бандл web **на білді** (передається build-arg), тож він має бути в `.env` **до** цієї команди. Змінив пізніше → `--build` web заново.

Перевір і зареєструйся:
```bash
curl -I https://app.example.com        # 200
curl -I https://api.example.com/healthz # 200
```
Відкрий `https://app.example.com/signup` — **перший юзер стає адміном** → Settings → LLM: додай ключі (осідають у credential-store у volume `workspace_data`, переживають редеплої).

---

## 8. Google OAuth — **після** деплою

Так само, як на Railway: спершу URL, потім OAuth (застосунок реєструє Google-провайдера лише за наявності `GOOGLE_CLIENT_ID/SECRET`).

1. Google Console → OAuth client:
   - **Authorized JavaScript origins:** `https://app.example.com`
   - **Authorized redirect URIs:** `https://app.example.com/api/auth/callback/google`
2. У `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (для web) і `GOOGLE_OAUTH_CLIENT_ID` (той самий id, для api — звіряє audience ID-токена).
3. `docker compose -f docker-compose.yml -f deploy/hetzner/docker-compose.hetzner.yml up -d --build web api`

---

## 9. Підсумок вартості

| Позиція | €/міс |
|---|---|
| CAX21 (4/8) | 10.49 |
| IPv4 | 0.50 |
| Qdrant Cloud (free 1 GB) | 0 |
| **Разом** | **~11 / міс** (+VAT) |

Бюджетний варіант CAX11 (2/4) + swap ≈ **~6.5/міс**. Проти Railway ($5/30 днів trial, потім usage-based) Hetzner дешевший для always-on.

## Пастки
- **Порти api/web** тримай на `127.0.0.1` (вже в addendum) — назовні їх фронтить лише Caddy.
- **`caddy_data` volume** зберігає серти — не видаляй (інакше впораєшся в rate-limit Let's Encrypt).
- **`NEXT_PUBLIC_API_BASE` — build-time.** Зміна = ребілд web.
- **Оновлення:** `git pull && docker compose -f docker-compose.yml -f deploy/hetzner/docker-compose.hetzner.yml up -d --build`. Міграції Alembic бігають на старті api автоматично.
- **Бекап:** `docker compose exec postgres pg_dump ...` + volume `workspace_data` (credential-store + `.master.key`). Див. [BACKUP_RESTORE.md](BACKUP_RESTORE.md).
