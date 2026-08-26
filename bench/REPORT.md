# Celmis на Martian Code Review Bench — звіт

**Статус:** у процесі
**Дата початку:** 2026-08-20
**Залізо:** MacBook (Apple Silicon), Docker 29.7.2, 7.2 GB RAM для Docker, 191 GB вільно

---

## 0. Конфігурація прогону

| Параметр | Значення |
|---|---|
| Модель рев'ю | `gemini-3.7-flash` |
| Thinking budget | (заповнюється) |
| Модель судді | `anthropic_claude-sonnet-4-5-20250929` |
| Профіль оцінювання | Core (158 золотих коментарів) |
| F-beta | 0.5 (важить precision) |
| Репозиторії | Sentry, Grafana, Keycloak, Cal.com — **4 з 5** |
| Виключено | Discourse — Ruby не підтримується Celmis |
| Організація форків | `celmis-bench` |
| Акаунт-постувач | `celmis-bot` |

---

## 1. Підняття стека — документованим шляхом

Прогін виконано **з нуля** після `docker compose down -v`, на коді HEAD `8b05187`.
Кроки — рівно ті, що в README, без ручних правок.

### 1.1. Одна команда

```bash
./scripts/init-env.sh
```

```
created .env from .env.example
filled 13: CELMIS_JWT_SECRET, CELMIS_MASTER_KEY, CELMIS_OPS_TOKEN,
CREDENTIAL_MASTER_KEY, LITELLM_MASTER_KEY, LITELLM_SALT_KEY, MCP_JWT_SECRET,
NEXTAUTH_SECRET, POSTGRES_PASSWORD, REVIEW_BITBUCKET_SECRET,
REVIEW_GITLAB_TOKEN, REVIEW_WEBHOOK_SECRET, SANDBOX_TOKEN

left empty (not ours to invent):
  GOOGLE_CLIENT_ID … VAPID_PUBLIC_KEY — an EC P-256 keypair, not a random
  string — generating something plausible there is worse than leaving it blank
```

Перевірка згенерованого:

| Змінна | Формат | Результат |
|---|---|---|
| `SANDBOX_TOKEN` | ≥32 симв. | ✓ len=64 |
| `CREDENTIAL_MASTER_KEY` | Fernet, url-safe b64 | ✓ len=44, конструюється |
| `LITELLM_MASTER_KEY` | префікс `sk-` | ✓ len=51 |
| `CELMIS_MASTER_KEY` | ≥12, без пробілів | ✓ len=48 |

40 змінних, **нуль дублікатів**, **нуль значень-інструкцій**, `chmod 600`.

### 1.2. Чого це коштувало

Перший прогін (до виправлень) робився вручну, і кожна з чотирьох проблем
знайдена саме тому, що шлях був ручний:

| # | Проблема | Як виглядала | Статус |
|---|---|---|---|
| 1 | `.env.example` не давав робочого стека | контейнери не піднімались | ✅ `init-env.sh` |
| 2 | `CREDENTIAL_MASTER_KEY` — Fernet, приклад радив hex | UI: `Failed to fetch`, причина не показана | ✅ `cb89a54` |
| 3 | 11 секретів дорівнювали тексту коментаря | майстер-пароль = рядок із публічного репо | ✅ `2b33bde` |
| 4 | дублікати змінних | dotenv бере останній, ранній мовчки зникає | ✅ `6119bcb` |
| 5 | `SANDBOX_TOKEN` невидимий для скрипта | «filled 12», sandbox не стартує | ✅ `8b05187` |

Спільний патерн усіх п'яти: **запобіжник існував і спрацьовував не там**.
Майстер-логін відхиляє ключі коротші за 12 символів — рядок мав 22.
Таблиця плейсхолдерів шукала `change-me` — там його не було.
`${VAR:?}` спрацьовує лише на порожньому — значення порожнім не було.
Sandbox відмовлявся стартувати — але після того, як оператор вважав
налаштування завершеним.

## 2. Модель і thinking

### 2.1. gemini-3.7-flash

`Settings → LLM Setup → PR Review → Model`. Каталог моделей підтягується
**з живого API провайдера**, не з захардкодженого списку — у випадайці
37 позицій, включно з `gemini-3.7-flash`.

Перевірка, що вибір доїхав до бекенда:

```
ws:774d8a12-…  review → gemini-3.7-flash
```

> 📸 `12-model-3.7-flash.png`

**Нюанс, який ледь не став хибною знахідкою.** Перший запит
`resolve_profile('review')` без `workspace_id` віддав старе
`gemini-3.1-pro-preview` — бо резолвив **дефолтний** воркспейс, а UI зберіг
у мій. Інсталяція тримає два воркспейси, і на старті api про це попереджає:

```
deployment_mode_risk workspaces=2 mode=single_tenant — this installation
holds 2 workspaces while running single_tenant, where a repository with no
access rule, an MCP call with no bearer identity and a budget row that
cannot be read all resolve to FULL access across tenants;
set CELMIS_DEPLOYMENT_MODE=multi_tenant to make those paths refuse instead.
```

Це реєстр fall-open з `deployment.py` за роботою: він назвав кількість
воркспейсів, режим, три конкретні шляхи, що деградують у бік дозволу, і
змінну, яка це виправляє.

### 2.2. Thinking budget

**ЗНАХІДКА 9 — власний недогляд, знайдений перевіркою.** Налаштування
`gemini_thinking_budget` було додано в `config.py` і прокинуто в
`gemini_client.py`, але **не передане через docker-compose**. `.env` мав
`GEMINI_THINKING_BUDGET=0`, а контейнер бачив `-1`: compose передає лише
явно перелічені змінні.

Виправлено:

```yaml
GEMINI_THINKING_BUDGET: "${GEMINI_THINKING_BUDGET:--1}"
```

плюс змінна задокументована в `.env.example`, щоб `init-env.sh` її бачив.

Це ілюстрація того самого патерну, що й у знахідках 1–5: **налаштування,
яке мовчки не діє, гірше за налаштування, якого немає** — код виглядав
правильним на всіх трьох рівнях, а ланцюг був розірваний на четвертому.

### 2.3. Живий доказ

Ланцюг `.env → compose → Settings → ThinkingConfig`:

```
budget=0  thinking_config=include_thoughts=None thinking_budget=0 thinking_level=None
```

Два реальні виклики `gemini-3.7-flash`, той самий промпт:

| Режим | thoughts | output | total | відповідь |
|---|---|---|---|---|
| `thinking_budget=0` | 48 | 3 | **68** | `391` |
| dynamic (дефолт) | 57 | 3 | **77** | `391` |

Відповідь однакова, витрата різна: **на 12% менше токенів** на тривіальному
запиті. На діфі, де модель має що обдумувати, розрив буде значно більшим —
і, головне, **передбачуваним**: динамічний бюджет робить два прогони того
самого PR різними за ціною, що для бенчмарка неприйнятно.

> ⚠️ `thinking_budget=0` не означає нуль thoughts-токенів — Gemini 3.x
> витратив 48. Прапорець прибирає *динамічне розширення*, а не мислення
> як таке.

### Інцидент під час прогону — моя помилка, не дефект Celmis

Готуючи цей звіт, я закомітив `bench/results/` цілком, а там лежала копія
робочого `.env`, збережена для порівняння прогонів: 17 секретних змінних,
серед них живий GitHub PAT. Коміт пішов у origin.

- репозиторій **приватний** (анонімний запит до API → 404), тож коло доступу
  вузьке, але не порожнє;
- файл прибрано з індексу, патерн `bench/results/.env-*` додано в `.gitignore`;
- **блоб лишається досяжним у коміті `82861c9`** — прибрати його може лише
  перезапис історії з force-push;
- єдине, що реально закриває інцидент, — **ротація** засвітлених облікових
  даних, і насамперед GitHub-токена.

Причина рівно та сама, що й у знахідках 1–5 цього звіту: `.gitignore` містив
`.env` для кореня, а `git add <тека>` не зупиняється перед крапкою в імені.
Правило, що виглядає покриттям, покривало один шлях із багатьох.


## 3. Ключі через UI

Внесені **через інтерфейс**, не через `.env` — щоб пройти саме той шлях,
яким піде реальний користувач, і перевірити Fernet-сховище.

`Settings → LLM Setup → Provider keys`. Обидва ключі одразу маскуються
в полі. Після Save у `credentials_v2` з'являються три рядки:

| provider | label |
|---|---|
| `google` | default |
| `anthropic` | default |
| `__llm_workspace__` | default |

Усі під слотом воркспейсу, зашифровані Fernet.

**Test пройшов на обох:**

```
https://generativelanguage.googleapis.com/v1beta/models   HTTP/1.1 200 OK
https://api.anthropic.com/v1/models                       HTTP/1.1 200 OK
```

> 📸 `11-run2-keys-tested.png`

### ЗНАХІДКА 8 — ключ Gemini у відкритому вигляді в логах

Під час Test у лозі api з'явилось:

```
INFO  HTTP Request: GET
      https://generativelanguage.googleapis.com/v1beta/models?key=AQ.Ab8RN6L…
```

Google приймає ключ у **query string**, а логер `httpx` пише повний URL на
рівні INFO. Хвіст ключа знайдено в лозі **4 рази** за дві хвилини — по два
на кожне натискання Test.

Наслідок: `docker compose logs api` віддає робочий ключ будь-кому з доступом
до логів. У проді логи йдуть Promtail → Loki → Grafana, тобто ключ осідає ще
й там, із ретеншном.

**Наявний тест це не ловить.** `tests/security/test_no_secrets_in_query_string.py`
перевіряє HTML-форми фронтенду — «форма з секретом не має сабмітитись через
GET». Це інший бік тієї самої проблеми: тест сканує `web/**/*.tsx`, а витік
стався в бекенді, на вихідному запиті. Назва описує ширший інваріант, ніж
перевірка.

**Виправлення:**
1. `logging.getLogger("httpx").setLevel(WARNING)` — знімає весь клас проблеми
2. пропустити URL через наявний `src/security/redactor.py`
3. другий тест: жоден вихідний URL не логується з непорожнім query string

## 4. Підключення GitHub

`Settings → Git connections`. Токен машинного акаунта `celmis-bot`, введений
через UI тим самим one-shot на loopback, що й ключі моделей — значення не
проходило через текст жодного виклику.

Результат перевірки на боці UI:

```
GitHub  Connected
Saved as celmis-bot.  Updated 20.08.26, 23:35.
```

Форма не просто зберегла рядок — вона сходила в GitHub і **резолвила логін**,
тобто «Connected» тут означає перевірений доступ, а не записаний текст.

> 📸 `14-github-connected.jpeg`, `15-github-scopes.jpeg`

### Чому токен не потрібен у `.env` — відповідь із коду

Питання постало практично: тримати токен у `.env` чи вносити через UI. Код
дає однозначну відповідь, і вона різна для двох шляхів.

**Рев'ю PR не читає оточення взагалі.** `providers/base.py:80` створює
провайдера без токена, а конструктор при `token is None` іде виключно в
`resolve_git_credential(...)` і падає з прямою вказівкою, куди йти:

```
No GitHub credentials saved for user '...'.
Connect GitHub via the Connections page first.
```

**Клон має ланцюг із трьох ланок** (`sync/clone.py:131`), де сховище виграє
завжди:

```
1. CredentialStore (зашифроване, з UI)
2. env vars (GITHUB_TOKEN/…)
3. settings.bitbucket_token (legacy)
```

README це формулює чесно: токени в `.env` **опціональні** й потрібні лише
для CLI всередині контейнера.

**Доказ, що оточення не задіяне:**

```
контейнер api:  GITHUB_TOKEN довжина=0
UI:             Connected, saved as celmis-bot
```

Підключення працює при порожній змінній. Після цього рядок 187 у `.env`
очищено — токен лишився в одному місці замість двох.

### Що лежить на диску

`/workspace/data/secrets/credentials.db`, таблиця `credentials_v2`:

| provider | user_id | secret |
|---|---|---|
| `google` | `ws:774d8a12-…` | `gAAAAABqhyeZ…` 167 симв. |
| `anthropic` | `ws:774d8a12-…` | `gAAAAABqhyeh…` 231 симв. |
| `github` | `ws:774d8a12-…` | `gAAAAABqh2UO…` 143 симв. |
| `__llm_workspace__` | `ws:774d8a12-…` | `gAAAAABqhyg8…` 655 симв. |

Жоден рядок не містить `ghp_` у сирому вигляді — усе Fernet-шифротекст, ключ
із `CREDENTIAL_MASTER_KEY`. Слот `ws:{uuid}` — той самий воркспейс-скоуп, що
й у профілів моделей.

### Дві деталі, які варто знати

**Скоупи для полінгу ширші, ніж здається.** Підказка на сторінці:

> classic: `repo`. Auto-review by polling **additionally needs the classic
> `notifications` scope**; fine-grained tokens cannot poll at all — use a webhook.

Тобто «`repo` і досить» — правда лише для webhook-режиму. Для полінгу треба
`repo` + `notifications`, а fine-grained токен не підходить принципово. Це
рідкісний випадок, коли UI попереджає про обмеження **чужого** API до того,
як воно вистрелить.

**Резолв через legacy-слот попереджає, а не мовчить:**

```
git_credential_legacy_slot provider=github workspace=default
slot=ws:774d8a12-… — resolved via a legacy slot; re-save it on the
Connections page to isolate this token to the workspace
```

Зворотна сумісність тут не безшумна: вона працює й одразу каже, що саме
зробити, щоб перестати бути legacy.

## 5. Форки — і 57.9 GB, які виявились непотрібними

### 5.1. Як це роблять у бенчмарку

Щоб інструмент оцінили, його коментарі мають лежати в PR, до якого є доступ
на запис. README формулює прямо: *«Fork benchmark PRs into a GitHub org where
the tool under evaluation is installed»* — інструмент ставиться як GitHub App
і рев'ює форки автоматично, за подією створення PR.

Звідси розкладка: **окремий репозиторій на пару (PR × інструмент)**. В
організації `code-review-benchmark` — **2468 публічних репозиторіїв**, тобто
50 PR × ~49 інструментів.

Celmis не GitHub App: він self-hosted, рев'ю тригериться через CLI. Для судді
різниці немає — коментарі опиняються в PR однаково — але це єдина справжня
відмінність умов, і її варто назвати.

### 5.2. Хибний шлях, пройдений до кінця

`step0_fork_prs.py` клонує репозиторій і пушить у новий. Я довів цей шлях до
робочого стану, полагодивши по дорозі три речі: повний клон на кожен PR
замість дзеркального кешу, обірваний пуш, що блокує перезапуск вимогою
`Delete it first`, і фонові процеси, які гинули разом із викликом, лишаючи
порожній лог через буферизацію Python.

А потім заміряв те, з чого треба було починати:

```
вивантаження: 10.1 Мбіт/с (1.3 MB/с) — при семи одночасних git-процесах
```

Канал насичений; паралельність нічого не додає, п'ять потоків просто ділять
ту саму трубу. І порахував обсяг:

| Репозиторій | PR | Розмір | До вивантаження |
|---|---|---|---|
| grafana | 10 | 1880 MB | 18.8 GB |
| discourse | 10 | 1602 MB | 16.0 GB |
| cal.com | 10 | 1120 MB | 11.2 GB |
| keycloak | 9 | 593 MB | 5.3 GB |
| sentry | 6 | 880 MB | 5.3 GB |
| решта | 5 | | 2.6 GB |
| **разом** | **50** | | **57.9 GB → 12.7 год** |

### 5.3. Питання, яке скасувало всю роботу

> «нашо репозиторії викачувати локально»

GitHub форкає **на своєму боці**, і форк ділить сховище об'єктів із батьком.
Усередині мережі форків гілку можна створити прямо на SHA з батьківського
репозиторію — не маючи його локально:

```
POST /repos/{src}/forks              → форк, 0 байт
POST /repos/{fork}/git/refs  sha=…   → гілка на чужому SHA, 0 байт
POST /repos/{fork}/pulls             → PR готовий
```

Перевірено побайтово — sha256 дифів:

```
оригінал: 8f605cf9bbaec612a6ce77d2…
форк:     8f605cf9bbaec612a6ce77d2…
```

| | Клон + пуш | Форк через API |
|---|---|---|
| Вивантаження | 57.9 GB | **~0** |
| Час | 12.7 год | **~7 хв** |
| Історія | повна | **повна** |
| Локальний диск | ~5 GB дзеркал | нічого |

Окремо варто зафіксувати: я вже збирався різати історію до двох синтетичних
комітів, щоб зекономити ті 12 годин. Не знадобилось — форк несе історію
батька цілком, вона просто не йде через ваш канал. Компроміс, який здавався
неминучим, зник разом із задачею.

### 5.4. Що це коштувало натомість

Одне обмеження GitHub: **один форк репозиторію на організацію**. Тож 50 PR
лягли в 7 форків по 10, а не в 50 репозиторіїв:

| Форк | PR |
|---|---|
| `celmis-bench/grafana` | 10 |
| `celmis-bench/discourse-graphite` | 10 |
| `celmis-bench/cal.diy` | 10 |
| `celmis-bench/keycloak` | 9 |
| `celmis-bench/sentry` | 6 |
| `celmis-bench/sentry-greptile` | 4 |
| `celmis-bench/fork-probe-keycloak` | 1 |

Ізоляція потрібна між **інструментами**, а не між PR одного інструмента — і
в нашій організації інструмент один. Кожен PR має власну пару гілок
(`bench-base-{n}` ← `bench-pr-{n}`) і власний номер.

Ламається від цього рівно **пошук** у `step1`, який парсить імена
репозиторіїв. Дані не ламаються: запис має вигляд
`{tool, repo_name, pr_url, review_comments[]}`, де `repo_name` довільний.
Тому написано `collect_celmis.py` — він проходить по `mapping.json` і збирає
коментарі бота в ту саму структуру. **Кроки 2 → 2.5 → 3 і суддя лишаються
недоторканими** — а це і є та частина, де порівнюваність має значення.

### 5.5. Ліміт GitHub

Акаунт `celmis-bot` отримав зрізаний ліміт — **60 запитів на годину** замість
5000:

```
API rate limit exceeded for user ID 318998821
x-ratelimit-limit: 60
```

Причина, найімовірніше, у моєму скрипті: `find_fork` для кожного з 50 PR
перебирав усі репозиторії організації окремими запитами. Плюс новий акаунт
із серією створень репозиторіїв — типовий профіль для антиабузу. Ліміт
рахується **на акаунт**, не на токен, тож перевипуск PAT нічого не змінює.

Блокером це не стало: заміряно, що **одне рев'ю коштує 2–3 виклики API**,
тобто ~20 рев'ю на годину. Раннер перевіряє залишок перед кожним PR і чекає
скидання замість того, щоб зловити 403 посеред прогону.

## 6. Прогін рев'ю

Перш ніж форки були готові, я прогнав рев'ю на живому PR, щоб перевірити
ланцюг. Він не працював — і зламаних місць виявилось три.

### ЗНАХІДКА 10 — перейменований репозиторій вбиває рев'ю трейсбеком

`analyzer review https://github.com/calcom/cal.com/pull/8087` падає:

```
HTTPStatusError: Redirect response '301 Moved Permanently'
Redirect location: 'https://api.github.com/repositories/350360184/pulls/8087'
```

GitHub віддає 301 для перейменованих репозиторіїв, а клієнт має
`follow_redirects=False`. Причина цього дефолту названа в докстрінгу
[http.py](../src/http.py) і вона слушна:

> a client that follows a redirect it did not ask to follow is how an
> allowlisted host becomes a jump to one that is not

Але тут **хост той самий**: `api.github.com` → `api.github.com`. Правило,
написане проти стрибка на чужий хост, зупиняє перенаправлення в межах свого.

З 7 джерельних репо бенчмарка редиректить одне — `calcom/cal.com`, 10 PR із 50.
Форки не редиректять, тож прогін це не блокує.

**Що робити:** дозволити редирект, коли хост цілі проходить ту саму перевірку
егресу. Гарантія зберігається, бо перевірка та сама.

### ЗНАХІДКА 11 — ключ, збережений через UI, не доходив до Gemini

Найдорожча з усіх. Воркспейс, налаштований **повністю через інтерфейс**:

```
agent_error agent=quality   err=no API key is configured for this workspace
agent_error agent=security  err=no API key is configured for this workspace
agent_error agent=architect err=no API key is configured for this workspace
agent_error agent=tests     err=no API key is configured for this workspace
```

При цьому `/settings/llm` показує ключ збереженим, а його кнопка Test
повертає 200.

Причина — одне ім'я, прочитане двома способами. Сторінка Connections пише
провайдера як **`google`**. `_provider_of` виводить провайдера з назви моделі,
і голе `gemini-3.7-flash` читається як **`gemini`**. Ключ шукається під
іменем, під яким його ніхто не зберігав.

Найгірше те, що система **вже знала** про цей аліас —
[profiles.py:172](../src/llm/profiles.py#L172):

```python
# google/gemini are the same underlying key — try both aliases.
candidates = ("google", "gemini") if provider in ("google", "gemini") else (provider,)
```

Знання було в одному шляху резолву й відсутнє в іншому — і рев'ю ходить саме
другим.

### ЗНАХІДКА 12 — те саме ім'я, прочитане третім способом

Правка знахідки 11 не прибрала помилку, а пересунула її:

```
google.auth.exceptions.DefaultCredentialsError: Your default credentials
were not found.  (vertex_ai · gemini-3.7-flash · HTTP 500)
```

LiteLLM читає голе `gemini-*` як **Vertex AI** і йде шукати Application
Default Credentials, яких у контейнері нема й не буде. Префікс `gemini/`
обирає діалект з API-ключем.

Тобто один рядок `gemini-3.7-flash` читають троє — Celmis як `gemini`, UI як
`google`, LiteLLM як `vertex_ai` — і всі троє мають рацію у своїй системі
координат.

**Виправлено обидві** в `d040478`: аліас у резолві ключа + префікс там, де
рішення про провайдера вже ухвалено (щоб покрити й тих, хто передає модель
явно). Регресійний тест відтворює точний продакшн-стан і падає на обох
половинах без правки. 621 тест `tests/llm` + `tests/review` проходять.

### ЗНАХІДКА 13 — рев'ю без жодної моделі виглядає як успішне

Це найважливіше зі знайденого, і воно не про Gemini.

Коли всі чотири LLM-агенти мертві, CLI друкує:

```
Verdict: COMMENT
  findings:    0 (critical=0 error=0 warning=0 info=0)
  agents:      structural
  tokens:      0/0
```

і виходить з кодом 0. `status=partial` та `agents_failed=...` є **в логу**, не
у вердикті. На чужому PR це читається як «перевірили, все чисто».

Порівняйте з дисципліною, яку той самий продукт тримає в інших місцях:
`HygieneFinding.line` віддає `None`, коли джерело справді не несе номера
рядка, бо вигадати його «було б гірше, ніж визнати, що його немає». Тут же
відсутність результату подана як результат.

**Що робити:** вердикт має нести стан. `partial` — не `comment`, і ненульовий
код виходу, коли жоден LLM-агент не відпрацював.

### Стан провайдера

Після правок рев'ю доходить до Gemini і впирається в справжнє обмеження:

```
503 UNAVAILABLE — "This model is currently experiencing high demand."
```

Заміряно: `gemini-3.7-flash` віддає **2 успіхи з 3** послідовних запитів.
Решта моделей на цьому ключі — 404 (`gemini-3-flash`, `gemini-3-pro-preview`,
`gemini-2.5-flash` недоступні). Чотири агенти стартують паралельно й
вибивають квоту разом.

### Побічне: температура

LiteLLM попереджає на кожному виклику:

> Setting temperature < 1.0 for Gemini 3 models can cause infinite loops,
> degraded reasoning performance, and failure on complex tasks.

Celmis ставить `temperature=0.1` для агентів рев'ю
([base.py:420](../src/review/agents/base.py#L420)) — розумний вибір для
детермінізму, який для Gemini 3.x працює проти якості. Для прогону, що
міряє точність, це не дрібниця.

### Перше публічне рев'ю

Форк доїхав, і Celmis відпрацював на живому публічному PR:

**https://github.com/celmis-bench/sentry-greptile__celmis__PR1__20260821/pull/1**

```
Verdict: CHANGES
  findings: 5 (error=5)
  agents:   structural, tests, quality, architect  (security впав)
  tokens:   12,430/10,211
  elapsed:  94.9s
  ✓ Posted to PR: comments_posted=5
```

Знахідки, перевірені анонімним запитом до GitHub API — тобто видно будь-кому:

| Файл:рядок | Confidence | Знахідка |
|---|---|---|
| `organization_auditlogs.py:71` | **1.00** | Undefined variable `organization_context` → NameError |
| `paginator.py:880` | **1.00** | Negative queryset slicing unsupported by Django ORM |
| `paginator.py:839` | 0.95 | Missing `math` import + missing BasePaginator methods |
| `paginator.py:819` | 0.70 | `OptimizedCursorPaginator` без тестів |
| `organization_auditlogs.py:70` | 0.70 | Нові permission gates без тестів |

Дві перші — детерміновані за характером: невизначена змінна й непідтримувана
операція ORM перевіряються за секунди. Коментарі лежать інлайн на дифі та
несуть блоки **Suggested change** з готовою правкою.

> 📸 `17-public-pr-comments.jpeg`

### Дві дрібниці, помітні лише на GitHub

**Заголовок губиться у знахідок агента `tests`.** У CLI видно осмислений
рядок, у коментарі на GitHub — саме слово серйозності:

```
**🟠 ERROR**

The new `OptimizedCursorPaginator` class and its negative offset handling
logic do not have any unit tests…
```

Суть у тілі, тож суддя бенчмарка це прочитає. Але людина бачить картку з
заголовком «ERROR» — там, де вона читає знахідки насправді.

**Вердикт не виражається.** Тіло каже `CHANGES REQUESTED`, а стан рев'ю —
`COMMENTED`. GitHub не дозволяє вимагати змін у власному PR, а всі 50 форків
створені тим самим `celmis-bot`, який їх і рев'ювить. На підрахунок не
впливає (суддя читає коментарі), але жоден наш прогін не покаже вердикт
чесно — це властивість розкладки бенчмарка, не продукту.


## 7. Фінальна точність

### 7.1. Що вимірює бенчмарк

Martian Code Review Bench, офлайн-частина: **50 PR** із 5 проєктів, кожен із
людсько-верифікованими *golden comments*. LLM-суддя зіставляє коментар
інструмента із золотим і питає одне: «це про ту саму проблему?» —
формулювання може відрізнятись, важить суть.

| Репозиторій | Мова | PR |
|---|---|---|
| Sentry | Python | 10 |
| Grafana | Go | 10 |
| Cal.com | TypeScript | 10 |
| Keycloak | Java | 10 |
| Discourse | Ruby | 10 |

Три профілі підрахунку — `strict` (лише bug/security/concurrency/data/api),
`core` (+perf/test_gap/doc_defect), `all` (+style/speculative).

### 7.2. Планка

Опубліковані результати, суддя `openai_gpt-5.2`, 22 видимі інструменти:

| Профіль | Інструмент | Prec | Recall | F1 | F2 |
|---|---|---|---|---|---|
| CORE | **kodus-v2** | 52.4% | 34.2% | **41.4%** | **36.7%** |
| CORE | лідер `cubic-v2` | 56.8% | 60.8% | 58.7% | 59.9% |
| STRICT | **kodus-v2** | 50.5% | 36.0% | 42.0% | 38.2% |
| ALL | **kodus-v2** | 53.8% | 32.9% | 40.9% | 35.7% |

Kodus — 18-й із 22.

### 7.3. Метрика працює проти філософії Celmis

Дашборд за замовчуванням ранжує за **F2, де recall важить удвічі більше за
precision**. Celmis побудований на протилежному принципі — «повідомляємо лише
те, що можемо довести».

Ціна цього принципу видна в таблиці прямо:

| Інструмент | Prec | Recall | F2 | Місце |
|---|---|---|---|---|
| `graphite` | **73.3%** | 7.0% | 8.5% | останнє |
| `kg` | 54.5% | 15.2% | 17.8% | передостаннє |

`graphite` має **найвищу точність із усіх 22** і сидить на дні. Метрика карає
мовчання сильніше за помилку — бо припускає, що пропущений баг коштує дорожче
за зайвий коментар.

Це не привід міняти продукт під бенчмарк. Але це причина показувати **три
числа замість одного**: під F0.5 (precision вдвічі важливіший) той самий
kodus-v2 дає 47.4%, а не 36.7%, і картина інша.

### 7.4. Межі цього виміру

- **Ruby не індексується.** У `src/indexing/graph/languages/` є go, java,
  python, typescript, php, cpp, csharp, vue, terraform, k8s — ruby немає.
  Discourse (10 PR із 50) піде без графового контексту.
- **Прогін усе одно на всіх 50.** Результат на 40 не порівнюється з чужими
  числами, а це вбиває сенс вправи. Discourse показано окремим рядком.
- **Тест-сет відкритий.** Golden comments лежать у публічному репозиторії.
  Під них можна написати промпт і отримати красиве число, яке нічого не
  означає. Прогін робиться без жодного погляду в них до підрахунку.

## 8. Застереження

- Модель судді: `anthropic_claude-sonnet-4-5-20250929`. Різні судді дають різні
  цифри — Greptile звітував 82% recall, Augment на тих самих даних 45%.
- **4 з 5 репозиторіїв.** Discourse (Ruby) виключено: Celmis не має парсера Ruby.
- Coverage наведено обов'язково: рушій, що мовчить на частині PR, виглядає
  точнішим, ніж є.
- Результат self-reported на незалежному бенчмарку — не те саме, що незалежно
  перевірений результат.
