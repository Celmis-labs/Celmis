"""The curated entries of the review rules library.

Plain dicts so the file reads as data. Every entry is checked by
tests/review/test_the_rules_library_is_well_formed.py: unique ids and titles,
a known severity, language and tag, instructions within the 2000-character
limit a rule may carry, and a glob the matcher accepts.

`path_glob` is matched against the changed file's path with fnmatch (where
`*` crosses directories) plus `{a,b}` alternatives; empty = every file.
"""

from __future__ import annotations

_JS = "*.{js,jsx,mjs,cjs,ts,tsx}"
_TS = "*.{ts,tsx}"

CATALOG: list[dict] = [
    # ─── General ─────────────────────────────────────────────────────
    {
        "id": "general.no-ignored-exceptions",
        "title": "Do not ignore exceptions",
        "instructions": (
            "Flag catch / except / rescue blocks that swallow an error: an empty "
            "body, a bare `pass`, or a body that only returns a default without "
            "logging or re-raising. An ignored exception turns a failure into "
            "wrong data later. Acceptable: logging with context and re-raising, "
            "converting to a domain error, or a comment explaining why the error "
            "is expected and safe to drop."
        ),
        "severity": "error",
        "languages": ("general", "python", "javascript", "typescript", "php"),
        "tags": ("reliability", "correctness"),
        "examples_good": (
            "try:\n    save(order)\nexcept DatabaseError:\n"
            "    logger.exception(\"order_save_failed id=%s\", order.id)\n    raise"
        ),
        "examples_bad": "try:\n    save(order)\nexcept Exception:\n    pass",
    },
    {
        "id": "general.avoid-loop-equality-termination",
        "title": "Avoid equality operators in loop termination conditions",
        "instructions": (
            "Flag loops whose exit condition is `==` / `!=` (or `===` / `!==`) "
            "against a counter or a float that is changed by a step other than "
            "exactly one, or that can be skipped past (`i != n` with `i += 2`, "
            "float accumulation). Use `<`, `<=`, `>` or `>=` so an overshoot "
            "still terminates."
        ),
        "severity": "warning",
        "languages": ("general", "javascript", "typescript", "php", "python"),
        "tags": ("correctness",),
        "examples_good": "for (let i = 0; i < n; i += 2) { … }",
        "examples_bad": "for (let i = 0; i != n; i += 2) { … }",
    },
    {
        "id": "general.no-race-on-shared-state",
        "title": "Prevent race conditions in shared state operations",
        "instructions": (
            "Flag read-modify-write sequences on state shared between requests, "
            "threads, tasks or processes (module globals, caches, class "
            "attributes, rows read then written back) that are not protected by "
            "a lock, an atomic operation, a transaction with the right isolation, "
            "or a conditional update (`UPDATE … WHERE version = :v`). Name the two "
            "interleavings that lose an update."
        ),
        "severity": "error",
        "languages": ("general", "python", "javascript", "typescript", "php", "sql"),
        "tags": ("concurrency", "correctness"),
        "examples_good": (
            "UPDATE accounts SET balance = balance - :amt\n"
            "WHERE id = :id AND balance >= :amt"
        ),
        "examples_bad": (
            "balance = get_balance(id)\nif balance >= amt:\n"
            "    set_balance(id, balance - amt)"
        ),
    },
    {
        "id": "general.no-undeclared-variables",
        "title": "Avoid using undeclared variables",
        "instructions": (
            "Flag reads or writes of names that are never declared, imported or "
            "defined in an enclosing scope — including names that only exist on "
            "another branch of an if/else, a misspelt variable, or an implicit "
            "global created by assignment without `let` / `const` / `var`."
        ),
        "severity": "error",
        "languages": ("general", "javascript", "typescript", "php", "python"),
        "tags": ("correctness",),
        "examples_good": "const total = items.length;\nreturn total;",
        "examples_bad": "total = items.length; // implicit global\nreturn totl;",
    },
    {
        "id": "general.no-magic-numbers",
        "title": "Name magic numbers and strings",
        "instructions": (
            "Flag unexplained literal numbers or strings that encode a business "
            "rule (limits, timeouts, status codes, thresholds) when they appear "
            "in logic. Ask for a named constant or a configuration value. 0, 1, "
            "-1 and obvious literals are fine."
        ),
        "severity": "info",
        "languages": ("general",),
        "tags": ("maintainability",),
        "examples_good": "MAX_RETRIES = 5\nif attempts > MAX_RETRIES: …",
        "examples_bad": "if attempts > 5: …",
    },
    {
        "id": "general.no-secrets-in-code",
        "title": "Never commit secrets or credentials",
        "instructions": (
            "Flag API keys, passwords, tokens, private keys or connection strings "
            "with embedded credentials written into source, tests fixtures that "
            "look real, or config files. They must come from the environment or a "
            "secret store. Quote the line, never the full secret."
        ),
        "severity": "critical",
        "languages": ("general",),
        "tags": ("security",),
        "examples_good": "api_key = os.environ[\"PAYMENTS_API_KEY\"]",
        "examples_bad": "api_key = \"sk_live_51H…\"",
    },
    {
        "id": "general.handle-null-results",
        "title": "Check for null before dereferencing lookup results",
        "instructions": (
            "Flag code that dereferences the result of a lookup that can return "
            "null / None / undefined (find, get, first, a dictionary get, a regex "
            "match, a nullable column) without checking it first. Name the input "
            "that makes it empty."
        ),
        "severity": "error",
        "languages": ("general", "python", "javascript", "typescript", "php"),
        "tags": ("correctness", "reliability"),
        "examples_good": "user = repo.find(id)\nif user is None:\n    raise NotFound(id)\nreturn user.email",
        "examples_bad": "return repo.find(id).email",
    },
    {
        "id": "general.small-functions",
        "title": "Keep functions focused and short",
        "instructions": (
            "Flag new or heavily changed functions that do several unrelated "
            "things or exceed roughly 60 lines of logic, when a clear split "
            "exists. Suggest the split by naming the extracted functions. Do not "
            "flag generated code, data tables or tests."
        ),
        "severity": "info",
        "languages": ("general",),
        "tags": ("maintainability",),
    },
    {
        "id": "general.no-commented-out-code",
        "title": "Do not commit commented-out code",
        "instructions": (
            "Flag blocks of commented-out code added in this change. Version "
            "control keeps the history; dead code in comments rots and misleads. "
            "Explanatory comments are fine."
        ),
        "severity": "info",
        "languages": ("general",),
        "tags": ("maintainability",),
    },
    {
        "id": "general.timeouts-on-network-calls",
        "title": "Set timeouts on outbound network calls",
        "instructions": (
            "Flag HTTP, RPC, database or socket calls to another service made "
            "without an explicit timeout (Python `requests` without `timeout=`, "
            "`fetch` without an AbortSignal, cURL without CURLOPT_TIMEOUT). A call "
            "with no timeout can hang a worker forever."
        ),
        "severity": "warning",
        "languages": ("general", "python", "javascript", "typescript", "php"),
        "tags": ("reliability", "performance"),
        "examples_good": "requests.get(url, timeout=10)",
        "examples_bad": "requests.get(url)",
    },
    # ─── Security ────────────────────────────────────────────────────
    {
        "id": "security.sanitize-user-input",
        "title": "Always sanitize user inputs",
        "instructions": (
            "Flag places where data from a request, form, URL, header, cookie, "
            "file upload or message queue reaches a sensitive sink — HTML output, "
            "SQL, a shell command, a file path, a redirect, a template, a regex, "
            "deserialization — without validation, escaping or an allow-list. "
            "Name the source and the sink."
        ),
        "severity": "critical",
        "languages": ("general", "python", "javascript", "typescript", "php", "vue"),
        "tags": ("security",),
        "examples_good": "name = escape(request.args.get(\"name\", \"\"))",
        "examples_bad": "return f\"<h1>Hello {request.args['name']}</h1>\"",
    },
    {
        "id": "security.parameterized-queries",
        "title": "Use parameterized queries",
        "instructions": (
            "Flag SQL built by concatenating or interpolating values into the "
            "query string (f-strings, `+`, `.format`, sprintf, template literals). "
            "Values must be bound parameters; identifiers that must be dynamic "
            "must come from an allow-list."
        ),
        "severity": "critical",
        "languages": ("general", "python", "javascript", "typescript", "php", "sql"),
        "tags": ("security", "data"),
        "examples_good": "cur.execute(\"SELECT * FROM users WHERE email = %s\", (email,))",
        "examples_bad": "cur.execute(f\"SELECT * FROM users WHERE email = '{email}'\")",
    },
    {
        "id": "security.no-shell-injection",
        "title": "Do not pass user input to a shell",
        "instructions": (
            "Flag subprocess / exec / system / backtick calls that run through a "
            "shell (`shell=True`, `exec()`, `system()`, `child_process.exec`) "
            "with any part of the command coming from outside the program. Use an "
            "argument list without a shell, and validate the arguments."
        ),
        "severity": "critical",
        "languages": ("general", "python", "javascript", "typescript", "php"),
        "tags": ("security",),
        "examples_good": "subprocess.run([\"git\", \"log\", branch], check=True)",
        "examples_bad": "subprocess.run(f\"git log {branch}\", shell=True)",
    },
    {
        "id": "security.authorization-on-every-endpoint",
        "title": "Check authorization on every endpoint",
        "instructions": (
            "Flag new or changed request handlers that read or modify data "
            "without checking that the caller may access THAT object (tenant, "
            "owner, role) — authentication alone is not authorization. Point at "
            "the identifier taken from the request that is trusted without a "
            "check."
        ),
        "severity": "critical",
        "languages": ("general",),
        "tags": ("security",),
    },
    {
        "id": "security.no-path-traversal",
        "title": "Prevent path traversal in file access",
        "instructions": (
            "Flag file reads, writes or deletes whose path includes input from "
            "outside the program without normalizing it and proving it stays "
            "under the intended directory (`..`, absolute paths, symlinks)."
        ),
        "severity": "critical",
        "languages": ("general", "python", "javascript", "typescript", "php"),
        "tags": ("security",),
    },
    {
        "id": "security.no-sensitive-data-in-logs",
        "title": "Do not log sensitive data",
        "instructions": (
            "Flag log statements, error messages or analytics events that include "
            "passwords, tokens, API keys, full card numbers, session ids or "
            "personal data beyond an identifier."
        ),
        "severity": "error",
        "languages": ("general",),
        "tags": ("security",),
        "examples_good": "logger.info(\"login_ok user_id=%s\", user.id)",
        "examples_bad": "logger.info(\"login %s %s\", email, password)",
    },
    {
        "id": "security.safe-deserialization",
        "title": "Never deserialize untrusted data with unsafe loaders",
        "instructions": (
            "Flag `pickle.loads`, `yaml.load` without SafeLoader, PHP "
            "`unserialize`, Java/Node object deserializers or `eval` applied to "
            "data that can come from a user, a file upload, a cache shared with "
            "other tenants or the network."
        ),
        "severity": "critical",
        "languages": ("general", "python", "php", "javascript", "typescript"),
        "tags": ("security",),
        "examples_good": "data = yaml.safe_load(body)",
        "examples_bad": "data = pickle.loads(request.body)",
    },
    # ─── Python ──────────────────────────────────────────────────────
    {
        "id": "python.no-mutable-default-args",
        "title": "Avoid mutable default arguments",
        "instructions": (
            "Flag Python function parameters whose default is a mutable object "
            "(`[]`, `{}`, `set()`, a dataclass instance). The default is shared "
            "between calls. Use `None` and create the object inside the function, "
            "or `field(default_factory=…)` in dataclasses."
        ),
        "severity": "error",
        "languages": ("python",),
        "tags": ("correctness",),
        "path_glob": "*.py",
        "examples_good": "def add(item, bucket=None):\n    bucket = [] if bucket is None else bucket",
        "examples_bad": "def add(item, bucket=[]):\n    bucket.append(item)",
    },
    {
        "id": "python.context-managers-for-resources",
        "title": "Use context managers for files, locks and connections",
        "instructions": (
            "Flag files, sockets, database connections, sessions or locks that are "
            "opened or acquired without `with` (or an explicit try/finally that "
            "releases them). An exception between open and close leaks the "
            "resource."
        ),
        "severity": "warning",
        "languages": ("python",),
        "tags": ("reliability",),
        "path_glob": "*.py",
        "examples_good": "with open(path) as fh:\n    data = fh.read()",
        "examples_bad": "fh = open(path)\ndata = fh.read()",
    },
    {
        "id": "python.no-blocking-in-async",
        "title": "Do not block the event loop in async code",
        "instructions": (
            "Flag synchronous blocking calls inside `async def` functions — "
            "`time.sleep`, `requests`, synchronous database drivers, heavy CPU "
            "work, `subprocess.run` — without `await asyncio.to_thread(...)` or an "
            "async equivalent. One blocked coroutine stalls every request on "
            "that loop."
        ),
        "severity": "error",
        "languages": ("python",),
        "tags": ("performance", "concurrency"),
        "path_glob": "*.py",
        "examples_good": "await asyncio.sleep(1)\nresult = await asyncio.to_thread(parse, blob)",
        "examples_bad": "async def handler():\n    time.sleep(1)\n    return requests.get(url).json()",
    },
    {
        "id": "python.specific-exceptions",
        "title": "Catch specific exceptions, not bare except",
        "instructions": (
            "Flag `except:` and `except BaseException:` and broad `except "
            "Exception:` around code where a specific exception is expected. A "
            "bare except also catches KeyboardInterrupt and SystemExit."
        ),
        "severity": "warning",
        "languages": ("python",),
        "tags": ("reliability",),
        "path_glob": "*.py",
        "examples_good": "except (KeyError, ValueError) as exc:",
        "examples_bad": "except:",
    },
    {
        "id": "python.timezone-aware-datetimes",
        "title": "Use timezone-aware datetimes",
        "instructions": (
            "Flag `datetime.now()` / `datetime.utcnow()` without a tz, and "
            "comparisons or arithmetic mixing naive and aware datetimes. Use "
            "`datetime.now(UTC)` and store UTC."
        ),
        "severity": "warning",
        "languages": ("python",),
        "tags": ("correctness", "data"),
        "path_glob": "*.py",
        "examples_good": "created = datetime.now(UTC)",
        "examples_bad": "created = datetime.utcnow()",
    },
    # ─── JavaScript / TypeScript ─────────────────────────────────────
    {
        "id": "js.strict-equality",
        "title": "Use strict equality in JavaScript",
        "instructions": (
            "Flag `==` and `!=` in JavaScript/TypeScript where coercion can "
            "change the result (comparisons with strings, numbers, booleans, "
            "empty values). Use `===` / `!==`; `x == null` to test for both null "
            "and undefined is acceptable when intended."
        ),
        "severity": "warning",
        "languages": ("javascript", "typescript"),
        "tags": ("correctness",),
        "path_glob": _JS,
        "examples_good": "if (count === 0) { … }",
        "examples_bad": "if (count == \"0\") { … }",
    },
    {
        "id": "js.await-promises",
        "title": "Await or handle every promise",
        "instructions": (
            "Flag promises that are created and neither awaited, returned, nor "
            "given a `.catch` — a floating promise loses its errors and its "
            "ordering. Also flag `forEach` with an async callback when the caller "
            "expects the work to be finished."
        ),
        "severity": "error",
        "languages": ("javascript", "typescript"),
        "tags": ("reliability", "correctness"),
        "path_glob": _JS,
        "examples_good": "await Promise.all(items.map((i) => save(i)));",
        "examples_bad": "items.forEach(async (i) => { await save(i); });\nreturn done();",
    },
    {
        "id": "ts.no-any",
        "title": "Avoid `any` in TypeScript",
        "instructions": (
            "Flag new uses of `any` (explicit annotations, `as any`, untyped "
            "JSON.parse results passed on) where a concrete type, a generic or "
            "`unknown` with narrowing would do. `any` switches the compiler off "
            "for everything that touches the value."
        ),
        "severity": "warning",
        "languages": ("typescript",),
        "tags": ("maintainability", "correctness"),
        "path_glob": _TS,
        "examples_good": "const data: unknown = JSON.parse(raw);\nif (isUser(data)) { … }",
        "examples_bad": "const data: any = JSON.parse(raw);",
    },
    {
        "id": "ts.no-non-null-assertion",
        "title": "Do not silence null checks with the non-null assertion",
        "instructions": (
            "Flag the TypeScript non-null assertion `!` on values that can really "
            "be null or undefined at that point (lookups, optional props, "
            "query results). Narrow with a check instead."
        ),
        "severity": "warning",
        "languages": ("typescript",),
        "tags": ("correctness",),
        "path_glob": _TS,
        "examples_good": "const el = document.getElementById(id);\nif (!el) return;",
        "examples_bad": "document.getElementById(id)!.focus();",
    },
    {
        "id": "js.no-dangerous-html",
        "title": "Do not inject unsanitized HTML",
        "instructions": (
            "Flag `innerHTML`, `outerHTML`, `document.write`, React "
            "`dangerouslySetInnerHTML` and similar sinks fed with data that is not "
            "a constant or the output of a trusted sanitizer."
        ),
        "severity": "critical",
        "languages": ("javascript", "typescript"),
        "tags": ("security",),
        "path_glob": _JS,
        "examples_good": "el.textContent = comment.body;",
        "examples_bad": "el.innerHTML = comment.body;",
    },
    {
        "id": "js.react-hooks-deps",
        "title": "Keep React hook dependency lists complete",
        "instructions": (
            "Flag `useEffect`, `useMemo` and `useCallback` whose dependency array "
            "omits a value the callback reads from props, state or the component "
            "scope, and hooks called conditionally or after an early return."
        ),
        "severity": "warning",
        "languages": ("javascript", "typescript"),
        "tags": ("correctness",),
        "path_glob": "*.{jsx,tsx}",
    },
    {
        "id": "js.no-console-in-production",
        "title": "No debugging output in production code",
        "instructions": (
            "Flag `console.log`, `debugger`, `var_dump`, `print` statements or "
            "similar debugging output added to production code paths. Use the "
            "project's logger."
        ),
        "severity": "info",
        "languages": ("javascript", "typescript", "php", "python"),
        "tags": ("maintainability",),
    },
    # ─── Vue ─────────────────────────────────────────────────────────
    {
        "id": "vue.no-v-html-untrusted",
        "title": "Avoid v-html with untrusted content",
        "instructions": (
            "Flag `v-html` bound to anything that is not a constant or the "
            "output of a trusted sanitizer. It renders raw HTML and opens XSS."
        ),
        "severity": "critical",
        "languages": ("vue",),
        "tags": ("security",),
        "path_glob": "*.vue",
        "examples_good": "<p>{{ comment.body }}</p>",
        "examples_bad": "<p v-html=\"comment.body\"></p>",
    },
    {
        "id": "vue.key-on-v-for",
        "title": "Give every v-for a stable key",
        "instructions": (
            "Flag `v-for` without `:key`, or keyed by the loop index on lists "
            "that can be reordered, filtered or have items inserted. Use a stable "
            "id from the item."
        ),
        "severity": "warning",
        "languages": ("vue",),
        "tags": ("correctness",),
        "path_glob": "*.vue",
        "examples_good": "<li v-for=\"item in items\" :key=\"item.id\">",
        "examples_bad": "<li v-for=\"(item, i) in items\" :key=\"i\">",
    },
    {
        "id": "vue.no-prop-mutation",
        "title": "Do not mutate props in Vue components",
        "instructions": (
            "Flag assignments to a prop (or to a field of an object prop) inside "
            "the child component. Emit an event or copy the prop into local state."
        ),
        "severity": "error",
        "languages": ("vue",),
        "tags": ("correctness",),
        "path_glob": "*.vue",
        "examples_good": "emit(\"update:modelValue\", next)",
        "examples_bad": "props.modelValue = next",
    },
    {
        "id": "vue.no-v-if-with-v-for",
        "title": "Do not combine v-if and v-for on one element",
        "instructions": (
            "Flag elements carrying both `v-if` and `v-for`. Filter in a computed "
            "property or move the condition to a wrapping element."
        ),
        "severity": "warning",
        "languages": ("vue",),
        "tags": ("correctness", "performance"),
        "path_glob": "*.vue",
    },
    # ─── PHP ─────────────────────────────────────────────────────────
    {
        "id": "php.strict-comparison",
        "title": "Use strict comparison in PHP",
        "instructions": (
            "Flag `==` / `!=` in PHP where type juggling can change the result "
            "(strings vs numbers, `\"0\"`, null, arrays), and `in_array` / "
            "`array_search` without the strict flag in the same situations."
        ),
        "severity": "warning",
        "languages": ("php",),
        "tags": ("correctness",),
        "path_glob": "*.php",
        "examples_good": "if ($status === '0') { … }\nin_array($id, $ids, true);",
        "examples_bad": "if ($status == 0) { … }",
    },
    {
        "id": "php.escape-output",
        "title": "Escape output in PHP templates",
        "instructions": (
            "Flag `echo`, `print` or `<?=` of request data or database values "
            "into HTML without `htmlspecialchars(…, ENT_QUOTES)` or the "
            "framework's escaping helper."
        ),
        "severity": "critical",
        "languages": ("php",),
        "tags": ("security",),
        "path_glob": "*.php",
        "examples_good": "<?= htmlspecialchars($name, ENT_QUOTES, 'UTF-8') ?>",
        "examples_bad": "<?= $_GET['name'] ?>",
    },
    {
        "id": "php.prepared-statements",
        "title": "Use prepared statements with PDO / mysqli",
        "instructions": (
            "Flag PHP queries built with string concatenation or interpolation "
            "of variables. Use prepared statements with bound parameters."
        ),
        "severity": "critical",
        "languages": ("php",),
        "tags": ("security", "data"),
        "path_glob": "*.php",
        "examples_good": "$st = $pdo->prepare('SELECT * FROM users WHERE id = ?');\n$st->execute([$id]);",
        "examples_bad": "$pdo->query(\"SELECT * FROM users WHERE id = $id\");",
    },
    {
        "id": "php.declare-strict-types",
        "title": "Declare strict types and type every signature",
        "instructions": (
            "Flag new PHP files without `declare(strict_types=1);` and new "
            "functions or methods without parameter and return types where the "
            "types are known."
        ),
        "severity": "info",
        "languages": ("php",),
        "tags": ("maintainability", "correctness"),
        "path_glob": "*.php",
    },
    # ─── SQL / data ──────────────────────────────────────────────────
    {
        "id": "sql.uniqueness-constraints",
        "title": "Ensure database uniqueness constraints",
        "instructions": (
            "Flag code that enforces uniqueness only in the application (check if "
            "a row exists, then insert) and schema changes that add a naturally "
            "unique column (email, external id, slug) without a UNIQUE constraint "
            "or index. Two concurrent requests pass the check together."
        ),
        "severity": "error",
        "languages": ("sql", "general", "python", "php", "javascript", "typescript"),
        "tags": ("data", "concurrency"),
        "examples_good": "ALTER TABLE users ADD CONSTRAINT uq_users_email UNIQUE (email);",
        "examples_bad": "if not User.exists(email=email):\n    User.create(email=email)",
    },
    {
        "id": "sql.no-select-star",
        "title": "Select only the columns you need",
        "instructions": (
            "Flag `SELECT *` in application queries and views: it reads columns "
            "nobody uses, breaks when columns are added or reordered, and can "
            "expose sensitive fields."
        ),
        "severity": "info",
        "languages": ("sql",),
        "tags": ("performance", "maintainability"),
        "examples_good": "SELECT id, email FROM users WHERE id = $1",
        "examples_bad": "SELECT * FROM users WHERE id = $1",
    },
    {
        "id": "sql.safe-migrations",
        "title": "Write migrations that are safe on a live database",
        "instructions": (
            "Flag schema migrations that lock or rewrite large tables (adding a "
            "NOT NULL column without a default, changing a column type, creating "
            "an index without CONCURRENTLY on Postgres), that drop data without a "
            "backup path, or that have no working downgrade."
        ),
        "severity": "error",
        "languages": ("sql", "python", "php"),
        "tags": ("data", "reliability"),
    },
    {
        "id": "sql.transactions-for-multi-writes",
        "title": "Wrap related writes in a transaction",
        "instructions": (
            "Flag sequences of two or more writes that must succeed or fail "
            "together (order + order lines, debit + credit) executed without a "
            "transaction."
        ),
        "severity": "error",
        "languages": ("sql", "general", "python", "php", "javascript", "typescript"),
        "tags": ("data", "reliability"),
    },
    # ─── Performance ─────────────────────────────────────────────────
    {
        "id": "perf.no-n-plus-one",
        "title": "Avoid N+1 queries",
        "instructions": (
            "Flag loops that run a query or a remote call per item of a "
            "collection (lazy-loaded relations in a loop, `find` inside `map`). "
            "Load in one query (join, IN, eager loading) or batch the calls."
        ),
        "severity": "warning",
        "languages": ("general", "python", "php", "javascript", "typescript", "sql"),
        "tags": ("performance",),
        "examples_good": "users = User.where(id__in=ids)",
        "examples_bad": "for id in ids:\n    users.append(User.get(id))",
    },
    {
        "id": "perf.paginate-unbounded-queries",
        "title": "Paginate or bound queries that can grow",
        "instructions": (
            "Flag queries or API calls that load an entire table or collection "
            "into memory (no LIMIT, no pagination, `.all()` on user data) in "
            "request handlers."
        ),
        "severity": "warning",
        "languages": ("general", "sql", "python", "php", "javascript", "typescript"),
        "tags": ("performance",),
    },
    {
        "id": "perf.no-work-in-hot-loops",
        "title": "Hoist invariant work out of loops",
        "instructions": (
            "Flag expensive work repeated inside a loop although it does not "
            "depend on the loop variable: compiling a regex, opening a "
            "connection, reading a file, building the same object, linear "
            "`in` / `includes` lookups on a list that could be a set."
        ),
        "severity": "info",
        "languages": ("general",),
        "tags": ("performance",),
    },
    {
        "id": "perf.index-filtered-columns",
        "title": "Index the columns new queries filter on",
        "instructions": (
            "Flag new queries that filter, join or sort on columns that have no "
            "index when the table is expected to be large, and suggest the index."
        ),
        "severity": "info",
        "languages": ("sql", "general"),
        "tags": ("performance", "data"),
    },
]
