"""Redaction for everything an MCP client can read.

The LLM-bound redactor (``redactor.py``) can be switched off and was tuned to
keep prompts readable; this one guards the *outgoing* side of the code
intelligence API, where a leaked secret is a leak to a person or an agent whose
context we do not control. So:

* it always runs and ignores ``settings.redaction_enabled``;
* it is deterministic and dependency-free (regex rules, one entropy check);
* the placeholder keeps the shape of what was removed, so an agent can still
  read the pattern: ``postgresql+asyncpg://app:[REDACTED:dsn-password]@db/app``;
* references to a secret are *not* redacted, because they are what a developer
  needs (``os.getenv("DB_PASSWORD")``, ``${DB_PASSWORD}``, ``secretKeyRef``,
  ``vault:`` paths, ``changeme``, ``<password>``, empty values);
* git SHAs, UUIDs, import paths and lockfile integrity hashes stay intact, so
  the ``idx:`` header line and the cross-references survive.

Regex cannot see a split, encoded or computed secret. That is why secret files
are never read in the first place (``secret_files.py``) and why this is one
layer of several, not the only one.

Entry points: :func:`redact_for_mcp` (text) and :func:`redact_structure`
(JSON-like values, which also judges a value by the key it sits under).
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Any

from src.security.patterns import SECRET_PATTERNS
from src.security.redactor import RedactionStats
from src.security.secret_literals import mask_literals

PLACEHOLDER = "[REDACTED:{label}]"

# Name keywords (lower-cased substring) that mark a *value-carrying* name.
_NAME_KW = (
    "password", "passwd", "passphrase", "pwd", "secret", "token", "apikey", "api_key",
    "api-key", "private_key", "privatekey", "signing_key", "signingkey", "credential",
    "salt", "access_key", "accesskey", "client_secret", "encryption_key", "master_key",
)
# A name that ends like this points at a secret (or describes it) instead of
# holding it: ``password_env``, ``tokenUrl``, ``secret_name``, ``token_ttl``.
_POINTER_SUFFIX = (
    "env", "var", "name", "file", "path", "url", "uri", "ttl", "expiry", "expires",
    "expiration", "type", "field", "header", "param", "endpoint", "ref", "prefix", "dir",
    "mode", "alg", "algorithm", "provider", "length", "id", "location", "label", "hint",
    "policy", "required", "enabled", "count", "size", "min", "max", "pattern", "regex",
    "tokens", "limit", "timeout", "retries", "interval", "seconds", "ms", "attempts",
)

_CODE_EXT = frozenset({
    "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "go", "java", "kt", "kts", "php", "cs",
    "rb", "rs", "swift", "scala", "c", "cc", "cpp", "h", "hpp", "m", "dart", "lua", "vue",
})


# ─── Helpers ─────────────────────────────────────────────────────────

_REF_START = re.compile(
    r"""^(?:
        \[REDACTED
        |\$\{ | \$[A-Za-z_] | \{\{ | \#\{ | %\( | %[sd]\b | <[^>]*> | \{[^{}]*\}
        |os\.getenv | os\.environ | getenv | environ | process\.env | import\.meta\.env
        |env\( | env\. | config\( | settings\. | self\. | cfg\. | conf\. | props\.
        |vault: | ref\+ | secret:// | kv/ | /run/secrets
        |secretKeyRef | valueFrom | from_env | fromenv | Environment\.
    )""",
    re.VERBOSE | re.IGNORECASE,
)
_PLACEHOLDER_WORD = re.compile(
    r"^(?:|changeme|change[-_ ]?me|replace[-_ ]?me|your[-_ ].*|<.*>|x{3,}|\*{3,}|\.{3}|todo|fixme|"
    r"none|null|nil|undefined|true|false|example|placeholder|dummy|redacted|secret|password|"
    r"token|string|str|value)$",
    re.IGNORECASE,
)
_ENV_NAME = re.compile(r"^[A-Z]{2,}[A-Z0-9]*(?:_[A-Z][A-Z0-9]*)+$")
_ENV_WORD = re.compile(r"PASS|SECRET|TOKEN|KEY|CRED|URL|URI|DSN|AUTH|HOST|USER|SALT")
_ATTR_CHAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|->|::)[A-Za-z_][A-Za-z0-9_]*)+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _looks_random(v: str) -> bool:
    """Long alphanumerics that look generated (digits among letters, or evenly mixed case): a token, not a variable name."""
    if 6 <= len(v) < 16:
        # a short generated value: digits among letters, no snake_case, not ``user1``
        return (
            "_" not in v and any(c.isdigit() for c in v) and any(c.isalpha() for c in v)
            and not re.fullmatch(r"[a-z]{2,}\d{1,2}", v)
        )
    if len(v) < 16:
        return False
    letters = any(c.isalpha() for c in v)
    if any(c.isdigit() for c in v) and letters:
        return True
    return any(c.isupper() for c in v) and any(c.islower() for c in v) and _entropy(v) >= 4.0


def is_reference(value: str, *, code: bool = False, quoted: bool = False) -> bool:
    """True when ``value`` points at a secret rather than being one."""
    v = _unquote(value).strip().rstrip(",;)")
    if _PLACEHOLDER_WORD.match(v) or _REF_START.match(v):
        return True
    if "(" in v and not re.search(r"[:/@]", v.split("(", 1)[0]):
        return True  # a call: ``get_password()``, ``Secret.fetch("x")``
    if _ATTR_CHAIN.match(v):
        return True  # ``settings.db.password``: an expression, not a literal
    if quoted and _ENV_NAME.match(v) and _ENV_WORD.search(v):
        return True  # ``"DB_PASSWORD"``: a name to look up
    # a variable in code
    return bool(code and not quoted and _IDENT.match(v) and not _looks_random(v))


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# Words that name a secret on their own, once a name is split on ``_``, ``-``,
# ``.`` and camelCase: ``db_pass``, ``dbPass``, ``PW``, ``creds``.
_STRONG_TOKENS = frozenset({
    "pass", "pw", "pwd", "passwd", "password", "passphrase", "cred", "creds", "credential",
    "credentials", "secret", "secrets", "token", "pepper", "apikey", "pgpass", "whsec",
    "otp",
})
# ``key`` and its kin only mean a secret next to a qualifier (``hmac_key``,
# ``AccountKey``) or on their own; ``sort_key`` and ``primary_key`` are not.
_KEY_QUALIFIERS = frozenset({
    "hmac", "aes", "enc", "encryption", "session", "cookie", "jwt", "license", "licence",
    "signing", "sign", "private", "access", "api", "auth", "master", "account", "shared",
    "stripe", "crypto", "secret", "app", "webhook", "cipher", "sas", "csrf",
})
_WEAK_ALONE = frozenset({
    "key", "seed", "pin", "master", "signing", "private", "salt", "hmac", "auth",
    "otp", "sas",
})
_NOT_WORD_PASS = frozenset({"bypass", "compass", "overpass", "underpass", "surpass", "trespass", "encompass"})


def _name_tokens(name: str) -> list[str]:
    spaced = _CAMEL.sub("_", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _name_level(name: str) -> int:
    """0: not a secret name, 1: weak (judge the value too), 2: strong."""
    low = name.lower().replace("-", "_")
    if any(k in low for k in _NAME_KW):
        return 2
    toks = _name_tokens(name)
    for t in toks:
        if t in _STRONG_TOKENS:
            return 2
        if t.endswith("pass") and t not in _NOT_WORD_PASS and len(t) > 4:
            return 2
        if t.endswith(("passwd", "pw")) and len(t) > 2:
            return 2
    if "key" in toks and (len(toks) == 1 or any(t in _KEY_QUALIFIERS for t in toks)):
        return 1
    if any(t in _WEAK_ALONE for t in toks):
        return 1
    # glued all-caps forms: ``ACCOUNTKEY``, ``HMACKEY``
    glued = "".join(toks)
    if len(toks) <= 2 and glued.endswith("key") and any(glued.startswith(q) for q in _KEY_QUALIFIERS):
        return 1
    return 0


def _name_has_secret_keyword(name: str) -> bool:
    return _name_level(name) > 0 or name.lower() == "auth"


def _name_is_pointer(name: str) -> bool:
    low = re.sub(r"[^a-z0-9]", "", name.lower())
    return any(low.endswith(s) for s in _POINTER_SUFFIX) and not low.endswith(("tokenid_",))


def name_carries_secret(name: str, value: str | None = None) -> bool:
    """True when ``name`` marks a secret-holding variable. A weak name
    (``key``, ``seed``, ``pin``, ``hmac_key``) counts only with a value that
    looks like a generated secret, or with no value to judge."""
    level = _name_level(name)
    if level == 0 or _name_is_pointer(name):
        return False
    if level == 2 or value is None:
        return True
    return _weak_value_is_secretish(name, value)


def _weak_value_is_secretish(name: str, value: str) -> bool:
    v = _unquote(value).strip()
    if len(v) < 4 or any(c.isspace() for c in v) or _is_url_without_creds(v):
        return False
    if re.fullmatch(r"[\w.\-/]*\.(?:json|ya?ml|pem|txt|key|env|pub|crt)", v):
        return False
    toks = _name_tokens(name)
    if len(v) <= 12 and v.isdigit():
        return bool({"pin", "otp"} & set(toks)) and 4 <= len(v) <= 8
    if len(v) < 8:
        return False
    if _HEX.match(v):
        return len(v) >= 16
    if re.search(r"[+=]", v) and len(v) >= 16:
        return True
    if "/" in v:
        return False  # a path, or an identifier with a namespace
    has_d, has_l = any(c.isdigit() for c in v), any(c.isalpha() for c in v)
    if has_d and has_l:
        return True
    if any(c.isupper() for c in v) and any(c.islower() for c in v) and "_" not in v and len(v) >= 10:
        return True
    return len(v) >= 12 and _entropy(v) >= 3.4 and "_" not in v


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / n * math.log2(c / n) for c in counts.values())


_HEX = re.compile(r"^[0-9a-fA-F]+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NOT_A_SECRET_CTX = re.compile(
    r"(?:sha\d*|hash|checksum|integrity|digest|commit|etag|uuid|guid|revision|rev|"
    r"fingerprint|nonce|csrf|build|image|tag|version|salt|head|base|parent|tree|blob|oid|ref|"
    r"checkout|ancestor|object|since|until|from)\W{0,12}$",
    re.IGNORECASE,
)


def _classes(s: str) -> int:
    return sum((bool(re.search(r"[a-z]", s)), bool(re.search(r"[A-Z]", s)), bool(re.search(r"\d", s))))


def looks_like_secret_literal(s: str, *, context_before: str = "") -> bool:
    """High-entropy opaque string: base64-ish, 20+ chars, or non-SHA hex 32+."""
    if len(s) < 20 or any(c.isspace() for c in s) or _UUID.match(s):
        return False
    if re.match(r"^(?:sha\d+|md5)-", s, re.IGNORECASE) or s.startswith(("data:", "http://", "https://")):
        return False
    if _NOT_A_SECRET_CTX.search(context_before[-40:]):
        return False
    if _HEX.match(s):
        if len(s) in (40, 64):
            # a SHA or an integrity hash only when the text before it says so
            return bool(context_before) and _entropy(s) >= 3.0
        return len(s) >= 32 and _entropy(s) >= 3.0
    if not re.fullmatch(r"[A-Za-z0-9+/_\-=]+", s):
        return False
    return _classes(s) == 3 and _entropy(s) >= 4.0


# ─── Rules ───────────────────────────────────────────────────────────

_Sub = Callable[[re.Match[str], dict, str], str | None]


class _Ctx:
    """Per-call state: what kind of file the text came from."""

    __slots__ = ("code", "hint")

    def __init__(self, hint: str) -> None:
        self.hint = hint or ""
        ext = self.hint.rsplit(".", 1)[-1].lower() if "." in self.hint.rsplit("/", 1)[-1] else ""
        self.code = ext in _CODE_EXT


def _ph(label: str) -> str:
    return PLACEHOLDER.format(label=label)


_PEM_PRIVATE_KIND = re.compile(r"PRIVATE KEY(?: BLOCK)?$")

_PROVIDER = tuple(
    (name, SECRET_PATTERNS[name])
    for name in (
        "openai-key", "anthropic-key", "google-key", "github-token", "github-pat-fine",
        "slack-token", "stripe-key", "aws-access-key", "jwt", "bearer", "basic-auth",
    )
)
_EXTRA_PROVIDER = (
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("pypi-token", re.compile(r"\bpypi-[A-Za-z0-9_-]{40,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("hf-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("bitbucket-app-password", re.compile(r"\bATBB[A-Za-z0-9_=-]{20,}")),
    ("atlassian-token", re.compile(r"\bATATT[A-Za-z0-9_=+\-]{20,}")),
    ("slack-webhook", re.compile(r"(?<=hooks\.slack\.com/)(?:services|workflows|triggers)/[A-Za-z0-9/_-]{16,}")),
    ("discord-webhook", re.compile(r"(?<=/api/webhooks/)\d{6,25}/[A-Za-z0-9_-]{20,}")),
    ("teams-webhook", re.compile(r"(?<=webhook\.office\.com/)webhook[a-z0-9]*/[A-Za-z0-9@\-/]{30,}")),
    ("telegram-bot-token", re.compile(r"(?<![0-9])\d{8,12}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])")),
    ("celmis-token", re.compile(r"\bcmk_[A-Za-z0-9_-]{16,}|\bcelmis_[A-Za-z0-9_-]{20,}")),
)

# scheme://user:PASSWORD@host (any scheme: +driver, jdbc:, redis://:pw@, amqps, ...)
_DSN = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@'\"<>]*:)(?P<v>[^\s/'\"<>]+)(?=@[^\s@/'\"<>]+)"
)
# scheme://TOKEN@host (token as the only userinfo)
_USERINFO_TOKEN = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://)(?P<v>[A-Za-z0-9_\-.~%]{20,})(?=@[A-Za-z0-9])"
)
_KV_PASSWORD = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9_])(?:password|passwd|pwd|pass)\s{0,64}=\s{0,64})(?P<v>[^;\s'\"&,)]+)",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?P<pre>[?&](?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
    r"client[_-]?secret|sig|signature|auth[_-]?token|private[_-]?token|x-amz-signature|x-amz-security-token|x-amz-credential|x-goog-signature|x-goog-credential|secret[_-]?key|session[_-]?token)=)(?P<v>[^&\s'\"#]+)",
    re.IGNORECASE,
)
_AUTH_HEADER = re.compile(
    r"""(?P<pre>(?<![A-Za-z0-9_])(?:(?:proxy-)?authorization|x-api-key|x-auth-token|x-access-token|
        x-secret[a-z-]*|x-token|api-key|apikey|private-token|x-gitlab-token|x-hub-signature(?:-256)?)
        ["'\]\)]*\s{0,64}[:=,]\s{0,64}[fFbBrRuU]{0,2}["']?\s{0,64}
        (?:(?:bearer|basic|token|apikey|digest|negotiate|bot)\s{1,64})?)
        (?P<v>[^\s"',;})\]]{8,})""",
    re.IGNORECASE | re.VERBOSE,
)
# quoted literal assigned to a name: name = "v", "name": "v", name: 'v', x["name"] = "v"
_QUOTED_ASSIGN = re.compile(
    r"""(?P<pre>(?<![\w.\-])(?P<name>[A-Za-z_][\w.\-]*)["']?\]?(?:[ \t]*:[ \t]*[A-Za-z_][\w\[\], .|?]*?)?\s{0,64}[:=]\s{0,64}[fFbBrRuU]{0,2}(?P<q>["']))
        (?P<v>[^"'\n]{4,}?)(?P=q)""",
    re.VERBOSE,
)
# bare ``name = value`` / ``name: value`` on its own line (properties, ini, yaml, toml, dotenv),
# also behind a list dash, a comment mark or a markdown bullet (``- password: v``, ``# db_password=v``)
_BARE_ASSIGN = re.compile(
    r"""^(?P<pre>[ \t]*(?:[-*+;#>/][ \t]*){0,8}(?:export[ \t]+)?(?P<name>[A-Za-z_][\w.\-]*)[ \t]*[:=][ \t]*)
        (?P<v>[^\s#'"\[{=][^\n#'"]*?)[ \t]*(?:\#.*)?$""",
    re.VERBOSE | re.MULTILINE,
)
_SHELL_ENV = re.compile(
    r"""(?P<pre>(?<![A-Za-z0-9_])(?P<name>[A-Z][A-Z0-9_]*
        (?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIALS?)[A-Z0-9_]*)=)
        (?P<v>"[^"\n]*"|'[^'\n]*'|\S+)""",
    re.VERBOSE,
)
# A literal default for a secret-named setting: os.getenv("X_PASSWORD", "v"),
# process.env.X_TOKEN || "v", ${X_SECRET:-v}.
_ENV_DEFAULT_CALL = re.compile(
    r"""(?P<pre>\b(?:os\.getenv|os\.environ\.get|environ\.get|getenv|env|config|ENV\.fetch|Env\.get|
        System\.getenv|configService\.get(?:OrThrow)?|viper\.SetDefault)\s{0,64}\(\s{0,64}["'](?P<name>[A-Za-z_][\w.\-]*)["']\s{0,64},\s{0,64}
        (?:default\s{0,64}=\s{0,64})?(?P<q>["']))(?P<v>[^"'\n]+)(?P=q)""",
    re.VERBOSE,
)
_ENV_DEFAULT_JS = re.compile(
    r"""(?P<pre>\b(?:process\.env|import\.meta\.env)\.(?P<name>[A-Za-z_]\w*)\s{0,64}(?:\|\||\?\?)\s{0,64}(?P<q>["']))(?P<v>[^"'\n]+)(?P=q)"""
)
_ENV_DEFAULT_SHELL = re.compile(
    r"""(?P<pre>\$\{(?P<name>[A-Za-z_][\w.]*):-?)(?P<v>[^}\s]+)(?=\})"""
)
_BARE_ENTROPY = re.compile(r"(?<![A-Za-z0-9+/=_\-.])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=_\-.])")
_QUOTED_LITERAL = re.compile(r"(?P<q>[\"'])(?P<v>[A-Za-z0-9+/_\-=]{20,})(?P=q)")
_K8S_SECRET_DOC = re.compile(r"^kind:[ \t]*[\"']?Secret[\"']?[ \t]*$", re.MULTILINE)
_K8S_DATA_LINE = re.compile(
    r"^(?P<ind>[ \t]+)(?P<key>[A-Za-z0-9_.\-]+):[ \t]*(?P<v>[^\s#][^\n]*?)[ \t]*$", re.MULTILINE
)
_K8S_DATA_HEAD = re.compile(r"^(?:data|stringData):[ \t]*$", re.MULTILINE)


def _is_url_without_creds(v: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.\-]*://[^@\s]*$", v, re.IGNORECASE))


_PEM_BEGIN = re.compile(r"-----BEGIN (?P<k>[A-Z0-9 ]{1,40})-----")
_PUTTY_HEAD = re.compile(r"PuTTY-User-Key-File-\d+:")
_PUTTY_END = re.compile(r"(?:Private-MAC|Private-Hash):\s{0,64}[0-9a-fA-F]+")


def _rule_pem(text: str, hits: list[str], ctx: _Ctx) -> str:
    """Whole PEM blocks, and a private key cut off before its END line.

    Linear: a block without an END marker is looked up once per label, not once
    per BEGIN, so a page of ``-----BEGIN`` strings cannot make it quadratic.
    """
    if "-----BEGIN " in text:
        out: list[str] = []
        pos = 0
        no_end: set[str] = set()
        for m in _PEM_BEGIN.finditer(text):
            if m.start() < pos:
                continue
            kind = m.group("k")
            private = "PRIVATE" in kind
            end = -1
            if kind not in no_end:
                marker = f"-----END {kind}-----"
                end = text.find(marker, m.end())
                if end < 0:
                    no_end.add(kind)
                else:
                    end += len(marker)
            if end >= 0:
                out.append(text[pos:m.start()])
                out.append(_ph("private-key" if private else "pem-block"))
                hits.append("private-key" if private else "pem-block")
                pos = end
            elif _PEM_PRIVATE_KIND.search(kind):  # truncated block: to the end of the text
                out.append(text[pos:m.start()])
                out.append(_ph("private-key"))
                hits.append("private-key")
                pos = len(text)
                break
        out.append(text[pos:])
        text = "".join(out)
    if "PuTTY-User-Key-File-" in text:
        out = []
        pos = 0
        for m in _PUTTY_HEAD.finditer(text):
            if m.start() < pos:
                continue
            end = _PUTTY_END.search(text, m.end())
            if end is None:
                break
            out.append(text[pos:m.start()])
            out.append(_ph("private-key"))
            hits.append("private-key")
            pos = end.end()
        out.append(text[pos:])
        text = "".join(out)
    return text


def _rule_provider(text: str, hits: list[str], ctx: _Ctx) -> str:
    for label, rx in (*_PROVIDER, *_EXTRA_PROVIDER):
        text, n = rx.subn(_ph(label), text)
        hits.extend([label] * n)
    return text


def _sub_value(label: str, hits: list[str], keep: Callable[[re.Match[str]], bool]):
    def fn(m: re.Match[str]) -> str:
        if keep(m):
            return m.group(0)
        hits.append(label)
        return f"{m.group('pre')}{_ph(label)}"

    return fn


# Connection-string keys of the Azure family: AccountKey=..., SharedAccessKey=...
_CONN_KEYS = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9_])(?:AccountKey|SharedAccessKey|SharedAccessSignature|PrimaryKey|"
    r"SecondaryKey|AccessKeySecret|SecretAccessKey)\s{0,64}=\s{0,64})(?P<v>[^;\s'\"]{8,})",
    re.IGNORECASE,
)
# Credentials on a command line: curl -u user:pw, --user user:pw, --password pw, mysql -ppw.
_CLI_USER = re.compile(
    r"(?P<pre>(?<![\w-])(?:-u|--user|--proxy-user)(?:[ \t]+|=)[\"']?[^:\s'\"]{1,64}:)(?P<v>[^\s'\"]+)"
)
_CLI_PASSWORD = re.compile(r"(?P<pre>(?<![\w-])--(?:password|passwd|pass)[ \t]+)(?P<v>[^\s'\"\-][^\s'\"]*)")
_CLI_MYSQL = re.compile(
    r"(?P<pre>\b(?:mysql|mysqldump|mysqladmin|mysqlimport|mariadb)\b[^\n]{0,200}?[ \t]-p)(?P<v>[^\s'\"]{2,})"
)
_COOKIE_HEADER = re.compile(r"(?P<pre>\b(?:Set-Cookie|Cookie)\s{0,64}:[ \t]*)(?P<v>[^\r\n]+)", re.IGNORECASE)
_COOKIE_PAIR = re.compile(r"(?P<n>[A-Za-z0-9_.\-]+)=(?P<v>[^;\s]+)")
_COOKIE_ATTRS = frozenset({"path", "domain", "expires", "max-age", "samesite", "secure", "httponly", "version", "comment"})
# user:password@host/path with no scheme in front of it.
_SCHEMELESS_DSN = re.compile(
    r"(?P<pre>(?<![\w/:.@\-\[])[A-Za-z0-9_.\-]{1,64}:)(?P<v>(?!\[)[^\s/@:'\"<>]{3,})(?=@[A-Za-z0-9][A-Za-z0-9.\-]{0,100}(?::\d{2,5})?/[A-Za-z0-9_])"
)


def _rule_conn_keys(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _CONN_KEYS.sub(
        _sub_value("conn-key", hits, lambda m: is_reference(m.group("v"))), text
    )


def _rule_cli_creds(text: str, hits: list[str], ctx: _Ctx) -> str:
    def not_secret(m: re.Match[str]) -> bool:
        v = m.group("v")
        return v.isdigit() or is_reference(v)  # ``-u 1000:1000`` is a uid:gid

    text = _CLI_USER.sub(_sub_value("cli-password", hits, not_secret), text)
    text = _CLI_PASSWORD.sub(_sub_value("cli-password", hits, lambda m: is_reference(m.group("v"))), text)
    return _CLI_MYSQL.sub(_sub_value("cli-password", hits, lambda m: is_reference(m.group("v"))), text)


def _rule_cookie(text: str, hits: list[str], ctx: _Ctx) -> str:
    def pair(m: re.Match[str]) -> str:
        if m.group("n").lower() in _COOKIE_ATTRS or is_reference(m.group("v")):
            return m.group(0)
        hits.append("cookie")
        return f"{m.group('n')}={_ph('cookie')}"

    def header(m: re.Match[str]) -> str:
        return m.group("pre") + _COOKIE_PAIR.sub(pair, m.group("v"))

    return _COOKIE_HEADER.sub(header, text)


def _rule_dsn(text: str, hits: list[str], ctx: _Ctx) -> str:
    text = _DSN.sub(_sub_value("dsn-password", hits, lambda m: is_reference(m.group("v"))), text)
    text = _USERINFO_TOKEN.sub(
        _sub_value("url-token", hits, lambda m: is_reference(m.group("v"))), text
    )
    return _SCHEMELESS_DSN.sub(
        _sub_value("dsn-password", hits, lambda m: is_reference(m.group("v"))), text
    )


def _rule_kv_password(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _KV_PASSWORD.sub(
        _sub_value("kv-password", hits, lambda m: is_reference(m.group("v"), code=ctx.code)), text
    )


def _rule_query(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _QUERY_SECRET.sub(
        _sub_value("query-secret", hits, lambda m: is_reference(m.group("v"))), text
    )


_SCHEME_BEFORE = re.compile(r"(?:bearer|basic|token|apikey|digest|negotiate|bot)\s{1,64}$", re.IGNORECASE)


def _auth_value_is_reference(m: re.Match[str], code: bool) -> bool:
    """A header value is a variable only when it stands bare; after a scheme
    word ("Token abc...") or an opening quote it is a literal, whatever it looks like."""
    pre = m.group("pre")
    hyphenated = "-" in (re.match(r"[A-Za-z-]+", pre) or re.match(r"", pre)).group(0)  # not an identifier
    literal_position = hyphenated or bool(_SCHEME_BEFORE.search(pre)) or pre.rstrip().endswith(("'", '"'))
    return is_reference(m.group("v"), code=code and not literal_position)


def _rule_auth_header(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _AUTH_HEADER.sub(
        _sub_value("auth-header", hits, lambda m: _auth_value_is_reference(m, ctx.code)), text
    )


def _rule_quoted_assign(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn(m: re.Match[str]) -> str:
        name, v, q = m.group("name"), m.group("v"), m.group("q")
        if not name_carries_secret(name, v) or is_reference(v, code=ctx.code, quoted=True):
            return m.group(0)
        if _is_url_without_creds(v) or re.fullmatch(r"[\w.\-/]*\.(?:json|ya?ml|pem|txt|key|env)", v):
            return m.group(0)
        hits.append("secret-assign")
        return f"{m.group('pre')}{_ph('literal')}{q}"

    return _QUOTED_ASSIGN.sub(fn, text)


def _rule_bare_assign(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn(m: re.Match[str]) -> str:
        name, v = m.group("name"), m.group("v").rstrip()
        if not name_carries_secret(name, v) or is_reference(v, code=ctx.code, quoted=False):
            return m.group(0)
        if _is_url_without_creds(v) or v.endswith((":", "{", "[", "(", "\\")):
            return m.group(0)
        if re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off", v, re.IGNORECASE):
            return m.group(0)  # a number or a flag is a setting, not a password
        hits.append("secret-assign")
        return f"{m.group('pre')}{_ph('literal')}"

    return _BARE_ASSIGN.sub(fn, text)


def _rule_env_default(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn_q(m: re.Match[str]) -> str:
        if not name_carries_secret(m.group("name"), m.group("v")) or is_reference(m.group("v")):
            return m.group(0)
        hits.append("env-default")
        return f"{m.group('pre')}{_ph('env-default')}{m.group('q')}"

    def fn_s(m: re.Match[str]) -> str:
        if not name_carries_secret(m.group("name"), m.group("v")) or is_reference(m.group("v")):
            return m.group(0)
        hits.append("env-default")
        return f"{m.group('pre')}{_ph('env-default')}"

    text = _ENV_DEFAULT_CALL.sub(fn_q, text)
    text = _ENV_DEFAULT_JS.sub(fn_q, text)
    return _ENV_DEFAULT_SHELL.sub(fn_s, text)


def _rule_shell_env(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn(m: re.Match[str]) -> str:
        v = m.group("v")
        if _name_is_pointer(m.group("name")) or is_reference(v, code=False, quoted=True):
            return m.group(0)
        hits.append("shell-env-secret")
        return f"{m.group('pre')}{_ph('env-secret')}"

    return _SHELL_ENV.sub(fn, text)


def _rule_k8s_secret(text: str, hits: list[str], ctx: _Ctx) -> str:
    """Every value under ``data:`` / ``stringData:`` of a ``kind: Secret`` document."""
    if not _K8S_SECRET_DOC.search(text):
        return text
    out: list[str] = []
    in_data = False
    for line in text.split("\n"):
        stripped = line.strip()
        if _K8S_DATA_HEAD.match(line):
            in_data = True
            out.append(line)
            continue
        if in_data:
            if stripped and not line.startswith((" ", "\t")):
                in_data = False
            else:
                m = _K8S_DATA_LINE.match(line)
                if m and not is_reference(m.group("v")):
                    hits.append("k8s-secret-data")
                    out.append(f"{m.group('ind')}{m.group('key')}: {_ph('k8s-secret-data')}")
                    continue
        out.append(line)
    return "\n".join(out)


def _rule_entropy(text: str, hits: list[str], ctx: _Ctx) -> str:
    def quoted(m: re.Match[str]) -> str:
        v = m.group("v")
        if not looks_like_secret_literal(v, context_before=text[max(0, m.start() - 60): m.start()]):
            return m.group(0)
        hits.append("entropy-literal")
        q = m.group("q")
        return f"{q}{_ph('entropy-literal')}{q}"

    text = _QUOTED_LITERAL.sub(quoted, text)

    def bare(m: re.Match[str]) -> str:
        v = m.group(0)
        if _HEX.match(v.rstrip("=")) or _classes(v) < 3 or _entropy(v) < 4.5:
            return v
        if _NOT_A_SECRET_CTX.search(text[max(0, m.start() - 40): m.start()]):
            return v
        hits.append("entropy-literal")
        return _ph("entropy-literal")

    return _BARE_ENTROPY.sub(bare, text)


# A run of non-space characters this long is a data URI, a minified bundle or a
# blob, not a line of code a person reads; it is withheld whole. Besides the
# privacy argument it bounds every pattern below to linear time on hostile input.
MAX_RUN = 2048
_LONG_RUN = re.compile(rf"\S{{{MAX_RUN},}}")
# Budget for one redaction. Regex matching cannot be interrupted, so rules are
# written to be linear and this is the backstop between them.
MAX_SECONDS = 5.0


_WS_RUN = re.compile(r"\s{65,}")


def bound_whitespace(text: str) -> str:
    """Runs of more than 64 whitespace characters cut to 64.

    No rule looks across a longer gap than that, so the cut changes nothing a
    rule could have matched, and it keeps every pattern's cost linear on a
    hostile line (``Authorization:`` followed by a megabyte of blanks).
    """
    return _WS_RUN.sub(lambda m: m.group(0)[:64], text) if len(text) > 64 else text


class RedactionTimeout(RuntimeError):
    """The redaction ran out of time; the caller withholds the output."""


def _rule_long_runs(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn(m: re.Match[str]) -> str:
        hits.append("long-literal")
        return _ph("long-literal")

    return _LONG_RUN.sub(fn, text) if len(text) >= MAX_RUN else text


# Order matters: multi-line and structured rules first, then the generic ones.
# A sample value attached to a secret-named property: OpenAPI / JSON-schema / Swagger
# decorators ("password: { type: 'string', example: 'v' }"), and the block form
# ("password:" then, indented below it, "example: v" or "default: v").
_SAMPLE_KEYS = r"(?:examples?|x-example|default|const|value|sample|placeholder)"
_SCHEMA_SAMPLE_INLINE = re.compile(
    r"""(?P<head>(?<![\w.\-])["']?(?P<name>[A-Za-z_][\w.\-]*)["']?\s{0,64}:\s{0,64}\{[^{}\n]*?
        (?<![\w.\-])["']?""" + _SAMPLE_KEYS + r"""["']?\s{0,64}:\s{0,64}\[?\s{0,64})(?P<q>["'])(?P<v>[^"'\n]{4,}?)(?P=q)""",
    re.VERBOSE | re.IGNORECASE,
)
_SCHEMA_OPENER = re.compile(
    r"""^(?P<ind>[ \t]*)["']?(?P<name>[A-Za-z_][\w.\-]*)["']?[ \t]*:[ \t]*(?:\{[ \t]*)?$"""
)
_SCHEMA_SAMPLE_LINE = re.compile(
    r"""^(?P<pre>(?P<ind>[ \t]+)["']?""" + _SAMPLE_KEYS + r"""["']?[ \t]*:[ \t]*\[?[ \t]*)
        (?P<q>["']?)(?P<v>[^\s"'#,}\]][^"'\n#,}\]]*?)(?P=q)(?P<tail>[ \t]*[,}\]]*[ \t]*(?:\#.*)?)$""",
    re.VERBOSE | re.IGNORECASE,
)


def _rule_schema_sample(text: str, hits: list[str], ctx: _Ctx) -> str:
    def inline(m: re.Match[str]) -> str:
        name, v = m.group("name"), m.group("v")
        if not name_carries_secret(name, v) or is_reference(v, code=ctx.code, quoted=True):
            return m.group(0)
        if _is_url_without_creds(v):
            return m.group(0)
        hits.append("secret-sample")
        q = m.group("q")
        return f"{m.group('head')}{q}{_ph('literal')}{q}"

    text = _SCHEMA_SAMPLE_INLINE.sub(inline, text)
    if ":" not in text:
        return text
    lines = text.split("\n")
    opener: tuple[int, str] | None = None  # (indent, name) of the last secret-named opener
    for i, line in enumerate(lines):
        o = _SCHEMA_OPENER.match(line)
        if o and name_carries_secret(o.group("name")):
            opener = (len(o.group("ind").expandtabs()), o.group("name"))
            continue
        if opener is None or not line.strip():
            continue
        sample = _SCHEMA_SAMPLE_LINE.match(line)
        if sample and len(sample.group("ind").expandtabs()) > opener[0]:
            v = sample.group("v").strip()
            if (not is_reference(v, code=ctx.code, quoted=bool(sample.group("q")))
                    and not re.fullmatch(r"\d{1,6}|true|false|null|yes|no|on|off", v, re.IGNORECASE)):
                hits.append("secret-sample")
                lines[i] = f"{sample.group('pre')}{sample.group('q')}{_ph('literal')}{sample.group('q')}{sample.group('tail')}"
            continue
        if len(line) - len(line.lstrip()) <= opener[0]:
            opener = None  # left the property's block
    return "\n".join(lines)


# ─── Second line: forms the first rules cannot see ───────────────────
#
# Found by probing the redactor with fake secrets in the shapes people really
# write: a Kubernetes ``name:``/``value:`` pair, a ``<password>`` element, an
# ``.npmrc`` auth line, ``docker login -p``, a setter call, ``Pwd=`` inside a
# connection string in code, a full-width separator, and a sentence that says
# what the password is. Each is a rule of its own so a miss in one cannot hide
# another.

_NAME_VALUE_BLOCK = re.compile(
    r"""(?P<pre>^[ \t]*-?[ \t]*(?:name|key|variable|env)[ \t]*:[ \t]*["']?(?P<name>[A-Za-z_][\w.\-]*)["']?[ \t]*\n
        [ \t]*(?:-[ \t]*)?value[ \t]*:[ \t]*(?P<q>["']?))(?P<v>[^\s"'#][^"'\n#]*?)(?P=q)[ \t]*(?:\#.*)?$""",
    re.VERBOSE | re.MULTILINE,
)
_NAME_VALUE_INLINE = re.compile(
    r"""(?P<pre>["']?(?:name|key|variable|env)["']?\s{0,64}[:=]\s{0,64}["'](?P<name>[A-Za-z_][\w.\-]*)["']\s{0,64},\s{0,64}
        ["']?value["']?\s{0,64}[:=]\s{0,64}(?P<q>["']))(?P<v>[^"'\n]+)(?P=q)""",
    re.VERBOSE,
)
_XML_ELEMENT = re.compile(
    r"""(?P<pre><(?P<tag>[A-Za-z_][\w.\-]*)(?:\s[^<>]*)?>[ \t]*)(?P<v>[^<>\n]{3,}?)(?=[ \t]*</(?P=tag)>)"""
)
_NPMRC_AUTH = re.compile(r"(?P<pre>:_(?:authToken|auth|password)[ \t]*=[ \t]*)(?P<v>\S+)", re.IGNORECASE)
_CLI_REGISTRY_LOGIN = re.compile(
    r"(?P<pre>\b(?:docker|podman|buildah|skopeo|crane|oras|helm|nerdctl)\b[^\n]{0,60}?\blogin\b[^\n]{0,200}?[ \t](?:-p|--password)(?:[ \t]+|=))"
    r"(?P<v>[^\s'\"\-][^\s'\"]*)"
)
_SECRET_SETTER_CALL = re.compile(
    r"""(?P<pre>\b(?P<name>(?:set|with|use|put|add)_?(?:[A-Z_]\w*|\w*(?:password|passwd|secret|token|key))\w*)\s{0,64}\(\s{0,64}(?P<q>["']))
        (?P<v>[^"'\n]{4,}?)(?P=q)""",
    re.VERBOSE | re.IGNORECASE,
)
_PAIR_CALL = re.compile(
    r"""(?P<pre>\b[A-Za-z_][\w.]*\(\s{0,64}["'](?P<name>[A-Za-z_][\w.\-]*)["']\s{0,64},\s{0,64}(?P<q>["']))(?P<v>[^"'\s]{4,}?)(?P=q)"""
)
_FULLWIDTH_ASSIGN = re.compile(
    r"(?P<pre>(?<![\w])(?:password|passwd|pwd|passphrase|secret|token|api[_ -]?key)[ \t]*[：＝][ \t]*)(?P<v>[^\s]+)",
    re.IGNORECASE,
)
_PROSE_SECRET = re.compile(
    r"""(?P<pre>\b(?:pass(?:word|wd|phrase)|secret|token|api[ _-]?key|passcode|pin)\s{1,64}(?:is|was|equals|set\s{1,64}to)\s{1,64}["'`]?)
        (?P<v>[^\s"'`,;)]{6,})""",
    re.IGNORECASE | re.VERBOSE,
)


_DOCKER_ENV_SPACE = re.compile(
    r"""^(?P<pre>[ \t]*(?:ENV|ARG)[ \t]+(?P<name>[A-Za-z_][\w.\-]*)[ \t]+)(?P<v>[^\s=#"'][^\n#]*?)[ \t]*$""",
    re.MULTILINE | re.IGNORECASE,
)
_CRYPT_HASH = re.compile(
    r"(?P<pre>(?<![\w$])(?:[A-Za-z0-9_.@\-]{1,64}:)?)(?P<v>\$(?:apr1|2[abxy]|1|5|6|argon2(?:id|i|d)|pbkdf2[\w-]*)\$[A-Za-z0-9./$+=,\-]{8,})"
)


def _odd_quotes_before(text: str, pos: int) -> bool:
    """Is ``pos`` inside a string literal of its line? (odd number of one kind
    of quote earlier on the line — good enough to tell ``"Pwd=x;"`` in code
    from ``connect(password=x)``)"""
    line = text[text.rfind("\n", 0, pos) + 1: pos]
    return line.count('"') % 2 == 1 or line.count("'") % 2 == 1


def _rule_name_value(text: str, hits: list[str], ctx: _Ctx) -> str:
    def fn(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("secret-pair")
        return f"{m.group('pre')}{_ph('literal')}{m.group('q')}"

    text = _NAME_VALUE_BLOCK.sub(fn, text)
    return _NAME_VALUE_INLINE.sub(fn, text)


def _rule_xml_element(text: str, hits: list[str], ctx: _Ctx) -> str:
    if "</" not in text:
        return text

    def fn(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if (not name_carries_secret(m.group("tag"), v) or is_reference(v, quoted=True)
                or _is_url_without_creds(v) or re.fullmatch(r"\d{1,6}|true|false", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("xml-secret")
        return f"{m.group('pre')}{_ph('literal')}"

    return _XML_ELEMENT.sub(fn, text)


# ─── Third line: markup, SQL and legacy file formats ─────────────────
#
# A credential written into documentation or an old config format looks like
# prose to the assignment rules: a markdown table row, a bold or code-quoted
# name, an XML ``name="..." value="..."`` pair, SQL ``PASSWORD '...'``,
# ``sshpass -p`` and a netrc line.

_MD_TABLE_ROW = re.compile(
    r"""(?P<pre>^[ \t]{0,16}\|[ \t]{0,16}[`*]{0,3}(?P<name>[A-Za-z_][\w.\-]*)[`*]{0,3}[ \t]{0,16}\|[ \t]{0,16}[`*]{0,3})
        (?P<v>[^|\s`*][^|\n`*]*?)(?=[`*]{0,3}[ \t]{0,16}(?:\||$))""",
    re.VERBOSE | re.MULTILINE,
)
_MD_NAMED_ASSIGN = re.compile(
    r"""(?P<pre>(?<![\w`*])[`*]{1,3}(?P<name>[A-Za-z_][\w.\-]*)
        (?:[`*]{1,3}[ \t]{0,8}[:=]|[ \t]{0,8}[:=][`*]{1,3})[ \t]{0,8}[`*]{0,3})(?P<v>[^\s`*|]{3,})""",
    re.VERBOSE,
)
_XML_NAME_VALUE = re.compile(
    r"""(?P<pre>\b(?:name|key|id|variable|setting|property)[ \t]{0,8}=[ \t]{0,8}(?P<nq>["'])(?P<name>[^"'<>\n]{1,128})(?P=nq)
        [^<>\n]{0,120}?\bvalue[ \t]{0,8}=[ \t]{0,8}(?P<q>["']))(?P<v>[^"'<>\n]+)(?P=q)""",
    re.VERBOSE | re.IGNORECASE,
)
_XML_VALUE_NAME = re.compile(
    r"""(?P<pre>\bvalue[ \t]{0,8}=[ \t]{0,8}(?P<q>["']))(?P<v>[^"'<>\n]+)(?P<post>(?P=q)
        [^<>\n]{0,120}?\b(?:name|key|id|variable|setting|property)[ \t]{0,8}=[ \t]{0,8}(?P<nq>["'])(?P<name>[^"'<>\n]{1,128})(?P=nq))""",
    re.VERBOSE | re.IGNORECASE,
)
_SQL_PASSWORD = re.compile(
    r"""(?P<pre>\b(?:password|identified[ \t]{1,8}by|identified[ \t]{1,8}with[ \t]{1,8}\w{1,40}[ \t]{1,8}by)
        (?:[ \t]{1,8}(?:=[ \t]{0,8})?|[ \t]{0,8}=[ \t]{0,8})(?P<q>["']))(?P<v>[^"'\n]+)(?P=q)""",
    re.VERBOSE | re.IGNORECASE,
)
_SSHPASS = re.compile(
    r"""(?P<pre>\bsshpass\b[^\n]{0,60}?[ \t]-p[ \t]{0,8}(?P<q>["']?))(?(q)(?P<v>[^"'\n]+)|(?P<v2>[^\s"']+))"""
)
_NETRC_MACHINE = re.compile(
    r"(?P<pre>\bmachine[ \t]{1,8}\S{1,255}[ \t]{1,8}(?:login[ \t]{1,8}\S{1,255}[ \t]{1,8})?password[ \t]{1,8})(?P<v>\S+)"
)
_NETRC_LINE = re.compile(r"^(?P<pre>[ \t]{0,16}password[ \t]{1,8})(?P<v>\S{4,})[ \t]{0,8}$", re.MULTILINE | re.IGNORECASE)


def _looks_chosen(v: str) -> bool:
    return (any(c.isdigit() for c in v) or any(not c.isalnum() for c in v)
            or (any(c.isupper() for c in v) and any(c.islower() for c in v)))


def _rule_markup_assign(text: str, hits: list[str], ctx: _Ctx) -> str:
    def table(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off|-{2,}|:?-+:?", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("markup-secret")
        return f"{m.group('pre')}{_ph('literal')}"

    def named(m: re.Match[str]) -> str:
        v = m.group("v")
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or _is_url_without_creds(v) or re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("markup-secret")
        return f"{m.group('pre')}{_ph('literal')}"

    def pair(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("xml-secret")
        return f"{m.group('pre')}{_ph('literal')}{m.group('q')}"

    def pair_rev(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or re.fullmatch(r"\d{1,6}|true|false|yes|no|on|off", v, re.IGNORECASE)):
            return m.group(0)
        hits.append("xml-secret")
        return f"{m.group('pre')}{_ph('literal')}{m.group('post')}"

    def sql(m: re.Match[str]) -> str:
        if is_reference(m.group("v"), quoted=True):
            return m.group(0)
        hits.append("sql-password")
        return f"{m.group('pre')}{_ph('literal')}{m.group('q')}"

    def sshpass(m: re.Match[str]) -> str:
        v = m.group("v") or m.group("v2")
        if is_reference(v, quoted=bool(m.group("q"))):
            return m.group(0)
        hits.append("cli-password")
        return f"{m.group('pre')}{_ph('literal')}"

    def netrc(m: re.Match[str]) -> str:
        if is_reference(m.group("v")):
            return m.group(0)
        hits.append("netrc-password")
        return f"{m.group('pre')}{_ph('literal')}"

    def netrc_line(m: re.Match[str]) -> str:
        v = m.group("v")
        if is_reference(v) or not _looks_chosen(v):
            return m.group(0)
        hits.append("netrc-password")
        return f"{m.group('pre')}{_ph('literal')}"

    if "|" in text:
        text = _MD_TABLE_ROW.sub(table, text)
    if "*" in text or "`" in text:
        text = _MD_NAMED_ASSIGN.sub(named, text)
    if "value" in text.lower():
        text = _XML_NAME_VALUE.sub(pair, text)
        text = _XML_VALUE_NAME.sub(pair_rev, text)
    text = _SQL_PASSWORD.sub(sql, text)
    if "sshpass" in text:
        text = _SSHPASS.sub(sshpass, text)
    low = text.lower()
    if "password" in low:
        text = _NETRC_MACHINE.sub(netrc, text)
        text = _NETRC_LINE.sub(netrc_line, text)
    return text


def _rule_npmrc(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _NPMRC_AUTH.sub(_sub_value("npmrc-auth", hits, lambda m: is_reference(m.group("v"))), text)


def _rule_registry_login(text: str, hits: list[str], ctx: _Ctx) -> str:
    return _CLI_REGISTRY_LOGIN.sub(
        _sub_value("cli-password", hits, lambda m: is_reference(m.group("v"))), text)


def _rule_secret_call(text: str, hits: list[str], ctx: _Ctx) -> str:
    def setter(m: re.Match[str]) -> str:
        if not name_carries_secret(m.group("name"), m.group("v")) or is_reference(m.group("v"), quoted=True):
            return m.group(0)
        hits.append("secret-setter")
        return f"{m.group('pre')}{_ph('literal')}{m.group('q')}"

    def pair(m: re.Match[str]) -> str:
        v = m.group("v")
        if (not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True)
                or _is_url_without_creds(v)):
            return m.group(0)
        hits.append("secret-call")
        return f"{m.group('pre')}{_ph('literal')}{m.group('q')}"

    return _PAIR_CALL.sub(pair, _SECRET_SETTER_CALL.sub(setter, text))


def _rule_in_string_kv(text: str, hits: list[str], ctx: _Ctx) -> str:
    """``password=x`` / ``Pwd=x;`` INSIDE a string literal of a source file is a
    connection string, never a variable: the code-ident exemption of the plain
    ``name=value`` rule does not apply there."""
    if not ctx.code:
        return text

    def fn(m: re.Match[str]) -> str:
        v = m.group("v")
        if not _odd_quotes_before(text, m.start()) or is_reference(v, code=False):
            return m.group(0)
        hits.append("kv-password")
        return f"{m.group('pre')}{_ph('kv-password')}"

    return _KV_PASSWORD.sub(fn, text)


def _rule_docker_env(text: str, hits: list[str], ctx: _Ctx) -> str:
    """Legacy ``ENV NAME value`` (no ``=``) in a Dockerfile."""
    def fn(m: re.Match[str]) -> str:
        v = m.group("v").strip()
        if not name_carries_secret(m.group("name"), v) or is_reference(v, quoted=True):
            return m.group(0)
        hits.append("docker-env")
        return f"{m.group('pre')}{_ph('literal')}"

    return _DOCKER_ENV_SPACE.sub(fn, text)


def _rule_crypt_hash(text: str, hits: list[str], ctx: _Ctx) -> str:
    """htpasswd / shadow style password hashes: not secrets in themselves, but
    the thing a cracker is handed."""
    if "$" not in text:
        return text
    return _CRYPT_HASH.sub(_sub_value("password-hash", hits, lambda m: False), text)


def _rule_fullwidth(text: str, hits: list[str], ctx: _Ctx) -> str:
    if not re.search(r"[：＝]", text):
        return text
    return _FULLWIDTH_ASSIGN.sub(
        _sub_value("secret-assign", hits, lambda m: is_reference(m.group("v"))), text)


def _rule_prose(text: str, hits: list[str], ctx: _Ctx) -> str:
    """"the db password is hunter2" — a sentence in a doc, a comment or a
    commit message. Only a value that looks chosen (a digit, a symbol or mixed
    case) counts, so "the token is expired" stays readable."""
    def keep(m: re.Match[str]) -> bool:
        v = m.group("v")
        if is_reference(v):
            return True
        chosen = (any(c.isdigit() for c in v) or any(not c.isalnum() for c in v)
                  or (any(c.isupper() for c in v) and any(c.islower() for c in v)))
        return not chosen

    return _PROSE_SECRET.sub(_sub_value("prose-secret", hits, keep), text)



_RULES: tuple[Callable[[str, list[str], _Ctx], str], ...] = (
    _rule_pem,
    _rule_provider,
    _rule_long_runs,
    _rule_conn_keys,
    _rule_cli_creds,
    _rule_cookie,
    _rule_k8s_secret,
    _rule_dsn,
    _rule_auth_header,
    _rule_env_default,
    _rule_query,
    _rule_in_string_kv,
    _rule_kv_password,
    _rule_npmrc,
    _rule_registry_login,
    _rule_shell_env,
    _rule_schema_sample,
    _rule_name_value,
    _rule_xml_element,
    _rule_markup_assign,
    _rule_secret_call,
    _rule_quoted_assign,
    _rule_bare_assign,
    _rule_docker_env,
    _rule_crypt_hash,
    _rule_fullwidth,
    _rule_prose,
    _rule_entropy,
)


# ─── Public API ──────────────────────────────────────────────────────


def redact_for_mcp(text: str, *, source_hint: str = "") -> tuple[str, RedactionStats]:
    """Redact one string. Always runs; raises on an internal error (callers
    fail closed). ``source_hint`` is the file path when known."""
    stats = RedactionStats(bytes_in=len(text or ""), bytes_out=len(text or ""))
    if not text:
        return text, stats
    ctx = _Ctx(source_hint)
    hits: list[str] = []
    out = bound_whitespace(text)
    deadline = time.monotonic() + MAX_SECONDS
    for rule in _RULES:
        out = rule(out, hits, ctx)
        if time.monotonic() > deadline:
            raise RedactionTimeout("redaction exceeded its time budget")
    stats.secrets_found = len(hits)
    stats.patterns_matched = hits
    stats.bytes_out = len(out)
    return out, stats


def redact_for_mcp_floored(text: str, *, source_hint: str = "") -> tuple[str, RedactionStats]:
    """:func:`redact_for_mcp` followed by the literal-masking floor.

    The floor (`secret_literals.mask_literals`) masks ``name: value`` and
    ``KEY=value`` shapes by the name alone. Text that carries repository
    source to a person (howto, PR chat, the code Q&A agent, drift comments)
    goes through this, so a miss in one layer is caught by the other.
    """
    out, stats = redact_for_mcp(text, source_hint=source_hint)
    masked = mask_literals(out)
    if masked != out:
        stats.secrets_found += 1
        stats.patterns_matched.append("literal-floor")
        stats.bytes_out = len(masked)
    return masked, stats


def scan_lines(text: str, *, source_hint: str = "") -> list[tuple[int, str]]:
    """``[(line_no, label)]`` of lines holding a secret literal (1-based).

    Used by ``howto`` to say *where* a hard-coded credential sits without
    repeating it. Multi-line blocks (PEM) are located by their first line.
    """
    found: list[tuple[int, str]] = []
    lines = text.split("\n")
    ctx = _Ctx(source_hint)
    for m in _PEM_BEGIN.finditer(text):
        found.append((text.count("\n", 0, m.start()) + 1, "private-key"))
    for i, line in enumerate(lines, 1):
        hits: list[str] = []
        redacted = bound_whitespace(line)
        for rule in _RULES:
            redacted = rule(redacted, hits, ctx)
        if hits and redacted != line and hits[0] != "long-literal":
            found.append((i, hits[0]))
    return sorted(set(found))


# Keys whose value is a reference the client needs verbatim.
EXEMPT_KEYS = frozenset({"indexed_sha", "sha", "commit", "head_sha", "base_sha", "id", "token_id", "cursor"})
_SHA_VALUE = re.compile(r"^[0-9a-f]{7,64}$")
_CURSOR_VALUE = re.compile(r"^[A-Za-z0-9_\-=.]{1,1024}$")


def _exempt(key: str, value: str) -> bool:
    if key not in EXEMPT_KEYS:
        return False
    if key == "cursor":
        return bool(_CURSOR_VALUE.match(value))
    if key in ("id", "token_id"):
        return bool(re.fullmatch(r"[A-Za-z0-9_\-]{4,64}", value)) and not looks_like_secret_literal(value)
    return bool(_SHA_VALUE.match(value))


_PAIR_NAME_KEYS = ("name", "key", "variable", "var", "env", "setting", "property", "field")


def _pair_label(d: dict) -> str:
    if "value" not in d or not isinstance(d.get("value"), str):
        return ""
    for k in _PAIR_NAME_KEYS:
        v = d.get(k)
        if isinstance(v, str) and 0 < len(v) <= 128:
            return v
    return ""


def redact_structure(
    value: Any, *, key: str = "", source_hint: str = "", stats: RedactionStats | None = None
) -> Any:
    """Redact a JSON-like structure, returning a new one.

    A string is judged by the key it sits under as well as by its content:
    ``{"password": "hunter2"}`` has nothing a text rule could see.
    """
    stats = stats if stats is not None else RedactionStats()
    if isinstance(value, str):
        if _exempt(key, value):
            return value
        if key and name_carries_secret(key, value) and value and not is_reference(value, quoted=True):
            stats.secrets_found += 1
            stats.patterns_matched.append("secret-field")
            return _ph("secret-field")
        out, s = redact_for_mcp(value, source_hint=source_hint)
        stats.secrets_found += s.secrets_found
        stats.patterns_matched.extend(s.patterns_matched)
        if (
            out == value
            and not any(c.isspace() for c in value)
            and looks_like_secret_literal(value)
        ):
            stats.secrets_found += 1
            stats.patterns_matched.append("entropy-literal")
            return _ph("entropy-literal")
        return out
    if isinstance(value, dict):
        # a ``{"name": "DB_PASSWORD", "value": "..."}`` pair: the value is judged by the name
        label = _pair_label(value)
        return {
            k: redact_structure(
                v, key=label if (label and k == "value") else str(k), source_hint=source_hint, stats=stats
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [redact_structure(v, key=key if key in EXEMPT_KEYS else "", source_hint=source_hint, stats=stats) for v in value]
        return items if isinstance(value, list) else tuple(items)
    return value


__all__ = [
    "EXEMPT_KEYS",
    "PLACEHOLDER",
    "is_reference",
    "looks_like_secret_literal",
    "name_carries_secret",
    "redact_for_mcp",
    "redact_for_mcp_floored",
    "redact_structure",
    "scan_lines",
]
