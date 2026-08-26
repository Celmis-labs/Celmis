# Деплой Celmis на Railway

Railway деплоїть **окремі сервіси** (не docker-compose). Стек мапиться так:

| Сервіс | Що це | Джерело |
|---|---|---|
| **Postgres** | керована БД | Railway → New → Database → PostgreSQL |
| **api** | FastAPI + MCP + поллер | цей репо, Root Directory `/`, `railway.json` (Dockerfile) |
| **web** | Next.js | цей репо, Root Directory `web`, `web/railway.json` (Dockerfile) |
| Qdrant | вектори | лишається у **Qdrant Cloud** (не на Railway) |

MCP віддає сам `api` (`/mcp`) — окремий сервіс не потрібен. Redis (rate-limit) — опційно, можна пропустити.

`railway.json` уже в репо: для `api` він гонить `alembic upgrade head` і піднімає uvicorn на `$PORT` з `--proxy-headers` (коректний HTTPS за проксі Railway). `web` (Next standalone) сам поважає `$PORT`.

---

## ⚠️ Головне: постійний том для api

Файлова система Railway **ефемерна** — при кожному редеплої вона обнуляється. У api на диску живуть **credential-store** (`/workspace/data/secrets/credentials.db`), **майстер-ключ** (`.master.key`) і **vault**. Без тому всі LLM/git-ключі зникнуть при першому ж редеплої.

**Обов'язково:** api-сервіс → **Settings → Volumes → Add Volume**, mount path **`/workspace`**. Тоді майстер-ключ згенерується один раз у том, а ключі, додані через UI, переживуть редеплої.

---

## Крок за кроком

### 1. Postgres
New → Database → **PostgreSQL**. Railway створить сервіс `Postgres` зі змінною `DATABASE_URL`.

### 2. Сервіс `api`
1. New → **GitHub Repo** → `GITHUB_OWNER/Celmis` (`GITHUB_OWNER` — твій GitHub-акаунт
   або організація, де лежить репо/форк). Назви сервіс `api`.
2. Settings → **Root Directory** = `/` (Railway підхопить `railway.json` + кореневий `Dockerfile`).
3. **Volumes** → Add Volume, mount `/workspace` (див. вище).
4. **Variables** (див. таблицю нижче).
5. Settings → **Networking → Generate Domain** → отримаєш `https://api-…up.railway.app`.

### 3. Сервіс `web`
1. New → **GitHub Repo** → той самий репозиторій `GITHUB_OWNER/Celmis`. Назви `web`.
2. Settings → **Root Directory** = `web` (підхопить `web/railway.json` + `web/Dockerfile`).
3. **Variables** (таблиця нижче) — тут важливо: `NEXT_PUBLIC_API_BASE` **вшивається у бандл на етапі білду**, тож він має бути заданий **до** білду web. Тому: спершу згенеруй домен api (крок 2.5), потім став `NEXT_PUBLIC_API_BASE` = URL api, і аж тоді деплой web.
4. Networking → **Generate Domain** → `https://web-…up.railway.app`.

### 4. Зв'язати домени
- `api` → `CELMIS_CORS_ORIGINS` = `https://<web-domain>`
- `web` → `NEXT_PUBLIC_API_BASE` = `https://<api-domain>`, `NEXTAUTH_URL` = `https://<web-domain>`, `API_BASE_INTERNAL` = `https://<api-domain>`
- Редеплой web (бо `NEXT_PUBLIC_*` — build-time).

### 5. Перший вхід
Відкрий `https://<web-domain>/signup`. **Перший зареєстрований користувач стає адміном.** Далі Settings → LLM: додай ключі (вони підуть у credential-store на томі).

---

## Змінні

### api
| Змінна | Значення |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (код сам конвертує `postgresql://` → `postgresql+asyncpg://`) |
| `GEMINI_API_KEY` | твій ключ Gemini |
| `QDRANT_URL` | URL кластера Qdrant Cloud |
| `QDRANT_API_KEY` | ключ Qdrant Cloud |
| `CELMIS_JWT_SECRET` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `MCP_JWT_SECRET` | ще один такий самий (окремий) |
| `CELMIS_CORS_ORIGINS` | `https://<web-domain>` |
| `CELMIS_TRUST_PROXY` | `1` |
| `WORKSPACE_DIR` | `/workspace/data` |
| `VAULT_DIR` | `/workspace/vault` |
| *(пізніше, для Google)* `GOOGLE_OAUTH_CLIENT_ID` | client_id з Google Console |

### web
| Змінна | Значення |
|---|---|
| `NEXTAUTH_SECRET` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `NEXTAUTH_URL` | `https://<web-domain>` |
| `NEXT_PUBLIC_API_BASE` | `https://<api-domain>` (build-time!) |
| `API_BASE_INTERNAL` | `https://<api-domain>` |
| *(пізніше, для Google)* `GOOGLE_CLIENT_ID` | client_id |
| *(пізніше, для Google)* `GOOGLE_CLIENT_SECRET` | client_secret |

---

## Google OAuth — **після** деплою

Робити **після**, бо Google вимагає точні redirect URI, а їх ти знаєш лише коли є домен. Додатковий плюс: застосунок реєструє Google-провайдера **лише якщо задані** `GOOGLE_CLIENT_ID/SECRET` — тож деплой без них працює на паролях, а OAuth додаєш другим проходом.

1. Задеплой без Google, отримай `https://<web-domain>`.
2. Google Cloud Console → твій OAuth client:
   - **Authorized JavaScript origins:** `https://<web-domain>`
   - **Authorized redirect URIs:** `https://<web-domain>/api/auth/callback/google`
3. Змінні: `web` → `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`; `api` → `GOOGLE_OAUTH_CLIENT_ID` (той самий client_id — сервер звіряє audience ID-токена).
4. Редеплой `web` і `api`.

---

## Вартість

Trial = **$5 / 30 днів**. api + web + postgres + том у режимі always-on з'їдять $5 за кілька днів (Railway білить за RAM/CPU/egress). Для постійного демо — Hobby-план ($5/міс включає $5 usage) або дешевший always-on на Hetzner (див. [DEPLOY_AND_TESTING.md](DEPLOY_AND_TESTING.md) §1.6).

## Дрібні пастки
- **`$PORT`** — Railway призначає порт сам; `railway.json` для api вже слухає `${PORT}`, web (Next) — автоматично.
- **`NEXT_PUBLIC_API_BASE` — build-time.** Змінив → потрібен редеплой web.
- **Приватна мережа** (без egress-плати): замість публічного `API_BASE_INTERNAL` можна `http://<api>.railway.internal:$PORT`, але для старту простіше публічний URL.
- **Індексація репо** через Qdrant Cloud працює однаково — вектори не на Railway.
