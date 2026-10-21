"""Which files in a repository must never be indexed, read or returned.

A clone holds whatever was ever committed, and the occasional ``.env`` or
private key is among it. Code intelligence has no use for the *values* in such
files, and an agent that can read them through MCP is one prompt injection
away from handing them to a stranger. So the decision is made once, here, and
every reader (the indexing walker, the code reader, the dev tools, ``howto``,
the exploration agent, the Q&A retriever behind ``ask`` and the web chat, the
plain-text search) asks the same lists (:func:`classify`, :func:`safe_join` and
:func:`plain_file` for a file that may be shown, :func:`is_secret_path` where it
cannot be shown masked, so ``keys_only`` is hidden too):

* ``deny``      the file does not exist as far as Celmis is concerned;
* ``keys_only`` the file documents configuration (``.env.example``): names
                are useful, values are shown only when empty or an obvious
                placeholder (see :func:`mask_env_values`);
* ``ok``        an ordinary file (still redacted on the way out).

The default list cannot be shortened. ``settings.secret_path_globs_extra``
only adds to it.

A name is judged the way the filesystem will read it, not the way it was typed:
Unicode-compatibility folded (``NFKC``) and case-FOLDED (``casefold``, not
``lower``: a case-insensitive filesystem opens ``ſecrets.json`` for
``secrets.json``), with invisible characters and trailing dots dropped.

Only NAMES are cheap; a data file is also read (:func:`content_verdict`): its
whole content, up to :data:`SNIFF_MAX_BYTES`, is checked for a private key, a
service-account key, a Kubernetes ``Secret`` or a WireGuard key. A data file too
large to read that way is withheld rather than waved through. The content is read
the way a person would read the file: any line ending (CRLF, a lone CR ...), a
byte order mark, UTF-16 and UTF-32, a manifest indented, quoted, in a list or in
YAML flow style. Source code, prose, binary media and lockfiles merely MENTION the
words, so :func:`content_verdict` does not read them from disk at all. The strict shapes
(a private key, a bare ``kind: Secret`` at the start of a line, the JSON
``"kind": "Secret"`` and ``"type": "service_account"``) hold source and prose only where
a caller passes the content to ``classify(rel, data)``: the knowledge ingest and the
``howto`` tracer.

``settings.secret_path_globs_extra`` globs read like ``.gitignore`` lines
(:func:`_extra_matcher`): a name hides what is below it, ``**/`` reaches the root.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import posixpath
import re
import stat
import unicodedata
from pathlib import Path
from typing import Literal

Verdict = Literal["deny", "keys_only", "ok"]

REFUSED = "path withheld (secret file)"

#: The most of a file the content sniff reads. A data file above it cannot be
#: inspected, so it is withheld ("deny if unsure"); source code, prose and
#: binary media are never read for the sniff, and lockfiles are exempt by name.
SNIFF_MAX_BYTES = 2 * 1024 * 1024


class SecretPathRefused(PermissionError):
    """Raised by :func:`safe_join`. ``str(exc)`` never says why."""

    def __init__(self) -> None:
        super().__init__(REFUSED)


# ─── The lists ───────────────────────────────────────────────────────

# `credentials.<data>`: a credentials FILE, not a module called credentials
# (src/auth/credentials.py, Credentials.java, docs/credentials.md are code and
# prose and stay readable). Backup and template spellings are data too.
_CREDENTIALS_DATA_EXT: tuple[str, ...] = (
    "json", "json5", "jsonc", "yml", "yaml", "toml", "ini", "cfg", "conf", "txt", "csv",
    "xml", "properties", "env", "plist", "dat", "db", "sqlite", "enc", "secret", "secrets",
    "token", "bak", "old", "orig", "backup", "save", "tmp", "swp", "dist", "example",
    "sample", "template", "tpl", "cnf", "config", "hcl", "tfrc",
)
# `credentials.prod.yml` and `credentials.yml.enc` / `credentials.json.bak` too, for the
# extensions a credentials file really has; the rare ones only as the bare name.
_CREDENTIALS_MAIN_EXT = ("json", "yml", "yaml", "toml", "ini", "cfg", "conf", "txt", "env", "xml", "properties",
                         "cnf", "config", "hcl")
_CREDENTIALS_GLOBS: tuple[str, ...] = (
    *(f"credentials.{e}" for e in _CREDENTIALS_DATA_EXT),
    *(f"credentials.*.{e}" for e in _CREDENTIALS_MAIN_EXT),
    *(f"credentials.{e}.*" for e in ("json", "yml", "yaml", "toml")),
)

# The floor: names the dev tools hid before this list was central. They are
# part of the deny lists below (and so of ``classify``, ``safe_join``,
# ``plain_file`` and ``GIT_PATHSPEC_EXCLUDES``), not a second list beside them:
# a name that only a floor knew (``credentials.yml``, ``_netrc``,
# ``private_keys/``) used to be hidden by the dev tools and the Q&A retriever
# but read by the indexing walker, the code reader, ``howto`` and the
# exploration agent. Kept as named constants so a test can hold every one of
# them to account.
FLOOR_NAME_GLOBS: tuple[str, ...] = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", "*.tfstate",
    "*.tfstate.backup", "*.tfvars", "credentials", *_CREDENTIALS_GLOBS,
    "*credentials*.json", "service-account*.json", "serviceaccount*.json", "secrets.yml",
    "secrets.yaml", "secrets.json", "secrets.toml", "*.gpg", "*.asc",
)

#: Directory names under which nothing is shown (the floor's own).
FLOOR_DIR_NAMES: frozenset[str] = frozenset({
    "secrets", ".secrets", ".ssh", ".gnupg", ".aws", ".kube",
})

#: Directory names that are ALSO ordinary package or folder names (Go's
#: ``internal/secret``, a ``private_keys/README.md``): only the files in them
#: that are not source code or prose are withheld. The name rules still apply to
#: everything below them.
SOFT_DENY_DIRS: frozenset[str] = frozenset({"secret", "private_keys"})

# Directory names: anything below one of them is denied. ``.git`` is the
# clone's own: its config can carry a remote URL with a token, its objects every
# secret ever committed.
DENY_DIRS: frozenset[str] = frozenset({".git"}) | FLOOR_DIR_NAMES

# File names / extensions, matched on the case-folded base name.
_DENY_NAME_GLOBS_BASE: tuple[str, ...] = (
    # env files and key material
    ".env", ".env.*", "*.env", ".envrc", ".env-*", ".env_*", "*.env.local",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "*.ppk", "*.p8", "*.asc", "*.gpg", "*.pem.*", "*.key.txt",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "*.jceks", "*.bks", "keystore",
    # credential stores
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd",
    ".pgpass", ".my.cnf", ".dockercfg", ".s3cfg", "*.keytab", ".boto",
    "*dockerconfigjson", ".git",
    # package-manager and editor configs that carry registry or server logins
    # (YAML, XML, space-separated: nothing the example-file masker could keep safe)
    ".yarnrc", ".yarnrc.yml", "settings-security.xml",
    "local.settings.json", "sftp-config.json", "sftp-config-alt*.json", ".ftpconfig",
    ".vault-pass*", ".vault_pass*", "vault-pass*", "vault_pass*", "credentials",
    # one more store per tool that keeps a login in its own file: Cloudflare Workers' env file,
    # rclone, Databricks, Kaggle, MySQL's obfuscated login, PostgreSQL's service file, a CIFS
    # credentials file, a WireGuard interface (`wg0.conf`; any other name by its content),
    # Android signing, Terraform's CLI config, an FTP password, and an extensionless `secrets`
    ".dev.vars", ".dev.vars.*", "rclone.conf", ".databrickscfg", "kaggle.json", ".mylogin.cnf",
    ".pg_service.conf", "pg_service.conf", ".smbcredentials", "smbcredentials", "wg[0-9]*.conf",
    "keystore.properties", "signing.properties", ".terraformrc", "terraform.rc", ".ftppass",
    "secrets",
    # files named after credentials
    "credentials*.json", "*credentials*.json", "client_secret*.json", "*service-account*.json", "*service_account*.json",
    "*serviceaccount*.json",
    "secrets.yml", "secrets.yaml", "secrets.json", "secrets.toml", "secrets.ini",
    "secrets.env", "secrets.properties", "secrets.txt", "secrets.conf",
    "secret.yml", "secret.yaml", "secret.json",
    # terraform state and variables
    "*.tfstate", "*.tfstate.*", "*.tfvars", "*.auto.tfvars",
    # cluster and registry auth
    "kubeconfig", "kubeconfig.*", "*.kubeconfig", "*.ovpn",
    # sops
    "*.enc.*",
    # state, vars and key material by another name
    "*.tfvars.json", "*.auto.tfvars.json", "*.pkcs12", "*.pk8", "*.der.key",
    "gcp*key*.json", "*-keyfile.json", "firebase-adminsdk*.json",
    "*adminsdk*.json", "wp-config.php", "local_settings.py", "settings_local.py",
    "vault.yml", "vault.yaml", "vault.json", "*.vault", "*.vault.yml", "*.vault.yaml",
    "values-secret*.y*ml", "*-secret.y*ml", "*-secrets.y*ml", "*_secret.y*ml", "*_secrets.y*ml",
    "htpasswd", "*.htpasswd", "shadow", "master.key", "secret_key_base", "secret.key",
    # a file named after what it holds: vault-password.txt, token.txt, api_token
    "*password*.txt", "*passwd*.txt", "*passphrase*.txt", "*token*.txt", "*secret*.txt",
    "*credential*.txt", "*apikey*.txt", "*api_key*.txt", "*api-key*.txt", "*creds*.txt",
    "*_token", "*-token", "*_password", "*-password", "*_passwd", "*_secret", "*-secret",
    "*_apikey", "*_api_key", "*.token", "*.password", "*.secret", "*.secrets",
)
DENY_NAME_GLOBS: tuple[str, ...] = _DENY_NAME_GLOBS_BASE + tuple(
    g for g in FLOOR_NAME_GLOBS if g not in _DENY_NAME_GLOBS_BASE)

# Whole relative paths (suffix match on the normalised path).
DENY_PATH_SUFFIXES: tuple[str, ...] = (
    ".docker/config.json",
    ".azure/accesstokens.json", ".azure/msal_token_cache.json",
    ".azure/service_principal_entries.json",
    ".config/gh/hosts.yml",
    ".m2/settings.xml",
    ".composer/auth.json",
    ".huggingface/token", "huggingface/token",
    ".gradle/gradle.properties",
    ".config/hcloud/cli.toml", ".config/doctl/config.yaml", ".config/ngrok/ngrok.yml",
    ".config/glab-cli/config.yml", ".config/gcloud/access_tokens.db",
    ".azure/msal_token_cache.bin", ".azure/service_principal_entries.bin",
    ".vscode/sftp.json",
)

#: Paths that are secret only at the repository root (Composer's ``auth.json``;
#: a ``locales/en/auth.json`` is a translation file).
DENY_ROOT_PATHS: tuple[str, ...] = ("auth.json",)

# The .env files that exist to be read.
KEYS_ONLY_NAMES: frozenset[str] = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    ".env.defaults", ".env.tpl", ".env.test.example", "env.example",
    "example.env", "sample.env", "template.env", "dist.env",
    ".dev.vars.example", ".dev.vars.sample", ".dev.vars.template",
    # a Gradle build's `gradle.properties` is mostly settings and versions, and sometimes a
    # signing or registry password: shown with its values withheld (the user-level
    # `~/.gradle/gradle.properties` is denied outright, see DENY_PATH_SUFFIXES)
    "gradle.properties",
})
KEYS_ONLY_GLOBS: tuple[str, ...] = (
    ".env.*.example", ".env.*.sample", ".env.*.template", ".env.*.dist",
    "*.env.example", "*.env.sample", "*.env.template",
)

# What counts as source code or prose, for two decisions: a SOFT_DENY_DIRS
# directory still shows these, and the content sniff does not read them (a
# secret literal inside source code is the redactor's business, not a reason to
# hide the file).
_CODE_EXT = frozenset({
    ".py", ".pyi", ".pyx", ".pxd", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts",
    ".tsx", ".vue", ".svelte", ".astro", ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".sc", ".groovy", ".gradle", ".cs", ".vb", ".fs", ".fsx", ".rb", ".rake", ".php",
    ".swift", ".m", ".mm", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".cu",
    ".cuh", ".dart", ".lua", ".pl", ".pm", ".r", ".jl", ".ex", ".exs", ".erl", ".hrl",
    ".hs", ".ml", ".mli", ".clj", ".cljs", ".cljc", ".elm", ".nim", ".zig", ".sol",
    ".sql", ".proto", ".graphql", ".gql", ".thrift", ".css", ".scss", ".sass", ".less",
    ".html", ".htm", ".ipynb", ".tf", ".bicep",
})
_DOC_EXT = frozenset({".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc"})
_SOURCE_OR_DOC_EXT = _CODE_EXT | _DOC_EXT
# Binary media and archives: nothing a text sniff can say about them.
_BINARY_EXT = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".tif", ".tiff",
    ".heic", ".avif", ".svg", ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3",
    ".mp4", ".m4a", ".mov", ".avi", ".webm", ".ogg", ".wav", ".zip", ".gz", ".tgz", ".bz2",
    ".xz", ".7z", ".rar", ".tar", ".jar", ".war", ".ear", ".class", ".so", ".dll",
    ".dylib", ".exe", ".o", ".a", ".lib", ".pyc", ".pyd", ".wasm", ".psd", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx",
})
# Lockfiles are large, generated and carry no secret; a size cap must not hide them.
_LOCKFILE_NAMES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "go.sum",
    "poetry.lock", "pipfile.lock", "composer.lock", "cargo.lock", "gemfile.lock",
})
_NO_SNIFF_EXT = _SOURCE_OR_DOC_EXT | _BINARY_EXT | {".lock"}

_SNIFF_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d")
# A WireGuard interface or peer secret (`PrivateKey = <44 base64 characters>`): a `wg0.conf`
# is told by what it holds as well as by its name. Only the canonical spelling.
_SNIFF_WIREGUARD = re.compile(
    r"^[ \t]*(?:Private|Preshared)Key[ \t]*=[ \t]*[A-Za-z0-9+/]{43}=[ \t]*$", re.MULTILINE)


def _loose_shape(key: str, word: str) -> re.Pattern[str]:
    """``key: word`` as a YAML or JSON data file writes it, whatever the layout.

    The key quoted (single or double, anywhere on the line: JSON), or bare at the start of a
    line after any indentation and list dashes (``- kind: Secret``, the ``kubectl get -o yaml``
    shape), or bare inside a flow mapping (``{apiVersion: v1, kind: Secret}``); the value
    quoted, or bare and then ending the line, a comment or a flow (``kind: SecretStore`` is
    not a Secret), after YAML tags and anchors (``!!str``) and on the next line too.
    """
    value = (rf"(?:[!&]\S*[ \t]+)*(?:\"{word}\"|'{word}'"
             rf"|{word}(?=[ \t]*(?:#[^\n]*)?$|[ \t]*[,}}\]]))")
    return re.compile(
        rf"(?P<q>[\"']){key}(?P=q)\s*:\s*{value}"
        rf"|^[ \t]*(?:-[ \t]+)*{key}[ \t]*:\s*{value}"
        rf"|[{{,][ \t]*{key}[ \t]*:\s*{value}",
        re.MULTILINE)


#: The shapes refused in EVERY file whose content is sniffed, source and prose included where a
#: caller hands the content to ``classify`` (a bare ``kind: Secret`` at the start of a line, the
#: JSON ``"kind": "Secret"``)...
_SNIFF_K8S_STRICT = re.compile(
    r"^kind[ \t]*:\s*[\"']?Secret[\"']?[ \t]*(?:#[^\n]*)?$|\"kind\"\s*:\s*\"Secret\"", re.MULTILINE)
_SNIFF_SA_STRICT = re.compile(r"\"type\"\s*:\s*\"service_account\"")
#: ... and the looser layouts, only for a data file (code and prose merely mention the words:
#: an indented ``kind: "Secret";`` in a TypeScript interface is not a manifest).
_SNIFF_K8S_LOOSE = _loose_shape("kind", "Secret")
_SNIFF_SA_LOOSE = _loose_shape("type", "service_account")
# Cheap byte-level pre-filter: the regexes above can only match a text that
# contains one of these, so most files cost one `in` scan and no decode.
_SNIFF_MARKERS = (b"PRIVATE KEY", b"PuTTY-User-Key-File", b"service_account", b"Secret",
                  b"PrivateKey", b"PresharedKey")


class _CharClass:
    """``[abc]``, ``[a-c]``, ``[!abc]`` as ``fnmatch`` reads them (a reversed range names nothing)."""

    __slots__ = ("negate", "ranges", "singles")

    def __init__(self, stuff: str) -> None:
        self.negate = stuff[:1] == "!"
        if self.negate:
            stuff = stuff[1:]
        singles: set[str] = set()
        ranges: list[tuple[str, str]] = []
        i, n = 0, len(stuff)
        while i < n:
            if i + 2 < n and stuff[i + 1] == "-":
                ranges.append((stuff[i], stuff[i + 2]))
                i += 3
            else:
                singles.add(stuff[i])
                i += 1
        self.singles = frozenset(singles)
        self.ranges = tuple(ranges)

    def accepts(self, ch: str) -> bool:
        hit = ch in self.singles or any(lo <= ch <= hi for lo, hi in self.ranges)
        return hit != self.negate


#: One step of an operator glob, once compiled: ``("lit", c, None)``, ``("any", "", None)`` for
#: ``?``, ``("cls", "", class)`` for ``[...]`` and ``("star", "", None)`` for ``*`` (which also
#: crosses ``/``). The joint of ``/**/`` is ``("lit", "/", None)``, ``("enter", "", None)`` and
#: ``("dirs", "", None)``.
_Token = tuple[str, str, _CharClass | None]


def _glob_tokens(glob: str) -> list[_Token]:
    """``fnmatch`` steps (``*`` also crosses ``/``); an unclosed ``[`` is a plain character."""
    out: list[_Token] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        i += 1
        if c == "*":
            while i < n and glob[i] == "*":
                i += 1
            out.append(("star", "", None))
        elif c == "?":
            out.append(("any", "", None))
        elif c == "[":
            j = i
            if j < n and glob[j] in "!^":
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:
                out.append(("lit", "[", None))
                continue
            out.append(("cls", "", _CharClass(glob[i:j])))
            i = j + 1
        else:
            out.append(("lit", c, None))
    return out


class _Glob:
    """One operator glob (:func:`_extra_matcher`), run over a path in ONE pass.

    A regular expression backtracks: ``*a*a*a*.yaml`` against a long path of ``a`` costs
    seconds, and the path is whatever a caller sends. So the glob is kept as a list of
    steps (a character, ``?``, ``[...]``, ``*``, and the ``/**/`` joint) and read as a
    small automaton whose live states are the bits of ONE integer. Each character of the
    path moves all of them at once, so a path of ``n`` characters costs ``n`` steps of
    ``len(glob)`` bits, however the glob is written. Nothing is retried.
    """

    __slots__ = ("_accept", "_all", "_anchored", "_ascii", "_classes", "_dir_only", "_eps1",
                 "_eps2", "_lit", "_lits", "_loop", "_slash", "_start")

    def __init__(self, tokens: list[_Token], *, anchored: bool, dir_only: bool) -> None:
        self._anchored = anchored  # starts only at the root, else after any `/`
        self._dir_only = dir_only  # followed by `/`: a directory, not a file of that name
        lits: dict[str, int] = {}
        every = loop = slash = eps1 = eps2 = 0
        classes: list[tuple[int, _CharClass]] = []
        run = best = ""
        for i, (kind, ch, cls) in enumerate(tokens):
            bit = 1 << i
            if kind == "lit":
                lits[ch] = lits.get(ch, 0) | bit
                run += ch
                best = max(best, run, key=len)
                continue
            run = ""
            if kind == "any":
                every |= bit
            elif cls is not None:
                classes.append((bit, cls))
            elif kind == "star":  # may match nothing, so its state leads on at once
                loop |= bit
                eps1 |= bit
            elif kind == "enter":  # `(?:.*/)?` of a `/**/` joint: skipped, or entered ...
                eps1 |= bit
                eps2 |= bit
            else:  # "dirs" ... and then it loops on anything and is left on a `/`
                loop |= bit
                slash |= bit
        self._lits, self._all, self._classes, self._slash = lits, every, tuple(classes), slash
        self._loop, self._eps1, self._eps2 = loop, eps1, eps2
        self._accept = 1 << len(tokens)
        self._start = self._closure(1)
        self._lit = best  # what the glob spells out must be in the path: a cheap first test
        self._ascii: dict[str, int] = {}

    def _closure(self, states: int) -> int:
        """``states`` and every step that can be reached from them without a character."""
        while True:
            more = ((states & self._eps1) << 1) | ((states & self._eps2) << 2)
            if not more & ~states:
                return states
            states |= more

    def _consumers(self, ch: str) -> int:
        """The steps that consume ``ch`` (one bit per step)."""
        mask = self._lits.get(ch, 0) | self._all
        if ch == "/":
            mask |= self._slash
        for bit, cls in self._classes:
            if cls.accepts(ch):
                mask |= bit
        return mask

    def _table(self, low: str) -> dict[str, int]:
        table = self._ascii  # grows to 128 entries at most, so it is kept
        wide: dict[str, int] = {}
        for ch in set(low):
            if ch not in table:
                if ch < "\x80":
                    table[ch] = self._consumers(ch)
                else:
                    wide[ch] = self._consumers(ch)
        return {**table, **wide} if wide else table

    def search(self, low: str) -> bool:
        """True when the glob names ``low`` or something above it (its subtree is hidden)."""
        if self._lit and self._lit not in low:
            return False
        n = len(low)
        moves = self._table(low)
        loop, eps1, eps2, accept, start = self._loop, self._eps1, self._eps2, self._accept, self._start
        anchored, dir_only = self._anchored, self._dir_only
        cur = i = 0
        while True:
            if i == 0 or (not anchored and low[i - 1] == "/"):
                cur |= start
            elif not cur:  # nothing alive: go on to the next name a match may start at
                j = -1 if anchored else low.find("/", i)
                if j < 0:
                    return False
                i, cur = j + 1, start
            if cur & accept and (low[i] == "/" if i < n else not dir_only):
                return True
            if i >= n:
                return False
            nxt = ((cur & moves[low[i]]) << 1) | (cur & loop)
            more = ((nxt & eps1) << 1) | ((nxt & eps2) << 2)
            while more & ~nxt:
                nxt |= more
                more = ((nxt & eps1) << 1) | ((nxt & eps2) << 2)
            cur = nxt
            i += 1


@functools.lru_cache(maxsize=512)
def _extra_matcher(glob: str) -> _Glob | None:
    """One operator glob as a matcher over a normalised, case-folded relative path.

    Read like a ``.gitignore`` line: a leading ``./`` is dropped; a pattern with no ``/``
    in it, or one that starts with ``**/``, applies at any depth, the repository root
    included; any other ``/`` (a leading one too) ties it to the repository root; a trailing
    ``/`` means a directory only; ``/**/`` also matches no directory at all. What a pattern
    names, it names with everything below it: ``config``, ``config/`` and ``/config/*`` all
    hide ``config/app.txt``. ``*`` also matches ``/`` (so ``deploy/*`` reaches
    ``deploy/a/b``), as it always has. The comparison is case-blind.
    """
    g = _fold(glob.strip()).replace("\\", "/")
    while g.startswith("./"):
        g = g[2:]
    anchored = g.startswith("/")
    g = g.lstrip("/")
    dir_only = g.endswith("/")
    g = g.rstrip("/")
    deep = False
    while g.startswith("**/"):
        g, deep = g[3:], True
    if not g:
        return None
    tokens: list[_Token] = []
    for k, part in enumerate(g.split("/**/")):
        if k:  # a slash, then any run of directories or none
            tokens += [("lit", "/", None), ("enter", "", None), ("dirs", "", None)]
        tokens += _glob_tokens(part)
    at_any_depth = deep or (not anchored and "/" not in g)
    return _Glob(tokens, anchored=not at_any_depth, dir_only=dir_only)


#: No filesystem holds a path this long (Linux ``PATH_MAX`` is 4096), so one that is cannot be
#: a file to show: with globs configured it is withheld unjudged, and a caller cannot make the
#: globs read a megabyte of text.
_GLOB_PATH_MAX_CHARS = 8192


@functools.lru_cache(maxsize=1 << 16)
def _extra_hit(low: str, globs: tuple[str, ...]) -> bool:
    for g in globs:
        matcher = _extra_matcher(g)
        if matcher is not None and matcher.search(low):
            return True
    return False


def _extra_denies(low: str) -> bool:
    """True when one of the operator's globs (:func:`_extra_matcher`) hides ``low``."""
    globs = _extra_globs()
    if globs and len(low) > _GLOB_PATH_MAX_CHARS:  # also keeps such a string out of the cache
        return True
    return _extra_hit(low, globs)


def _extra_globs() -> tuple[str, ...]:
    try:
        from src.config import get_settings

        return tuple(getattr(get_settings(), "secret_path_globs_extra", None) or ())
    except Exception:  # noqa: BLE001 - config trouble must not open the gate
        return ()


def extra_globs() -> tuple[str, ...]:
    """The operator's additions (``SECRET_PATH_GLOBS_EXTRA``); they never shorten the lists above."""
    return _extra_globs()


def _compile(globs: tuple[str, ...]) -> re.Pattern[str]:
    # One alternation instead of a fnmatch call per glob: a repository walk
    # asks this question for every file.
    return re.compile("|".join(fnmatch.translate(g) for g in globs)) if globs else re.compile(r"(?!x)x")


_DENY_NAME_RX = _compile(DENY_NAME_GLOBS)
_KEYS_ONLY_RX = _compile(KEYS_ONLY_GLOBS)

#: Format characters a filesystem ignores or a reader strips (zero-width,
#: bidi controls, soft hyphen, BOM, variation selectors, combining grapheme joiner).
_EXTRA_IGNORABLE = frozenset({0x034F, 0x180B, 0x180C, 0x180D, *range(0xFE00, 0xFE10)})


def _ignorable(ch: str) -> bool:
    return unicodedata.category(ch) == "Cf" or ord(ch) in _EXTRA_IGNORABLE


def _segment(seg: str) -> str:
    if not seg.isascii():
        seg = "".join(c for c in unicodedata.normalize("NFKC", seg) if not _ignorable(c))
    return seg.strip().rstrip(". ") or seg.strip()


def _norm(rel_path: str) -> str:
    # A name is judged as it will be READ, not as it was typed: ".env ", ".env."
    # and ".env<zero-width space>" are the env file to every tool that opens it,
    # and so are the fullwidth and compatibility spellings (NFKC).
    rel = "/".join(_segment(seg) for seg in str(rel_path).replace("\\", "/").split("/"))
    rel = posixpath.normpath(rel) if rel else rel
    return rel.lstrip("/") if rel not in ("", ".") else rel


def _fold(text: str) -> str:
    """``casefold`` (not ``lower``): ``ſ`` and ``ß`` fold, ``K`` (Kelvin) is a ``k``."""
    low = text.casefold()
    return low if low.isascii() else unicodedata.normalize("NFKC", low).casefold()


def _ext(name: str) -> str:
    return os.path.splitext(name)[1]


@functools.lru_cache(maxsize=1 << 16)
def _name_verdict(low: str) -> Verdict:
    """The verdict of an already normalised, case-folded relative path (no operator globs)."""
    parts = low.split("/")
    name = parts[-1]
    dirs = parts[:-1]
    if name == ".git" or any(p in DENY_DIRS for p in dirs):
        return "deny"
    if any(p in SOFT_DENY_DIRS for p in dirs) and _ext(name) not in _SOURCE_OR_DOC_EXT:
        return "deny"
    if low in DENY_ROOT_PATHS or any(low == s or low.endswith("/" + s) for s in DENY_PATH_SUFFIXES):
        return "deny"
    if name in KEYS_ONLY_NAMES or _KEYS_ONLY_RX.match(name):
        return "keys_only"
    if _DENY_NAME_RX.match(name):
        return "deny"
    return "ok"


def _sniff_text(data: bytes) -> str | None:
    """``data`` as text the sniff patterns can read, or None when no marker is in it.

    Every line end reads as ``\\n`` (CRLF, a lone CR, form feed, NEL, U+2028 ...), a byte
    order mark is dropped, and a NUL byte is dropped too: UTF-16 and UTF-32 text (with or
    without a BOM, either byte order) then reads as the ASCII keywords it spells.
    """
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
    if not any(m in data for m in _SNIFF_MARKERS):
        return None
    text = data.decode("utf-8", errors="ignore").replace("\ufeff", "")
    return "\n".join(text.splitlines())


def _sniff(head: bytes, *, loose: bool = True) -> bool:
    """True when ``head`` (a whole file or its start) holds a private key, a
    service-account key or a Kubernetes ``Secret``. ``loose`` reads the layouts of a data
    file (indented, quoted, flow); a source or prose file is held to the strict shapes (it
    is only ever sniffed where a caller passes its content to :func:`classify`)."""
    text = _sniff_text(head[:SNIFF_MAX_BYTES])
    if text is None:
        return False
    if _SNIFF_PRIVATE_KEY.search(text) or _SNIFF_K8S_STRICT.search(text) or _SNIFF_SA_STRICT.search(text):
        return True
    return loose and bool(
        _SNIFF_K8S_LOOSE.search(text) or _SNIFF_SA_LOOSE.search(text) or _SNIFF_WIREGUARD.search(text))


def classify(rel_path: str, head: bytes | None = None) -> Verdict:
    """Classify a repo-relative path. ``head`` is the file's content (all of it
    when the caller has it; at most :data:`SNIFF_MAX_BYTES` are looked at), for
    the content sniff."""
    rel = _norm(rel_path)
    if not rel or rel == ".":
        return "ok"
    low = _fold(rel)

    # The operator's globs come first and are deny-only, so a name the built-in
    # lists treat as `keys_only` is still hidden when the operator says so.
    name = low.rsplit("/", 1)[-1]
    if _extra_denies(low):
        return "deny"

    verdict = _name_verdict(low)
    if verdict != "ok":
        return verdict
    if head and _sniff(head, loose=_ext(name) not in _SOURCE_OR_DOC_EXT):
        return "deny"
    return "ok"


def _needs_sniff(name: str) -> bool:
    return name not in _LOCKFILE_NAMES and _ext(name) not in _NO_SNIFF_EXT


@functools.lru_cache(maxsize=1 << 17)
def _sniff_file(path: str, size: int, mtime_ns: int, ctime_ns: int, ino: int) -> Verdict:
    # The stat fields are the cache key: a file that is rewritten, touched or
    # replaced is read again; one that is walked a second time costs a stat.
    try:
        with open(path, "rb") as fh:
            data = fh.read(SNIFF_MAX_BYTES)
    except OSError:
        return "ok"
    return "deny" if _sniff(data) else "ok"


def content_verdict(repo_path: Path | str, rel_path: str) -> Verdict:
    """The part of the question only the file itself can answer: ``deny`` for a
    data file that holds a private key, a service-account key or a ``Secret``
    manifest anywhere in it, for one too large to look through
    (:data:`SNIFF_MAX_BYTES`), and for any regular file that has a second name
    (a hard link: git never makes one in a clone, and its name says nothing about
    the file it is).

    The content is not read for source code, prose, binary media or lockfiles.
    Cached on ``(path, size, mtime, ctime, inode)``, so a second walk of an
    unchanged repository reads nothing.
    """
    rel = _norm(rel_path)
    if not rel or rel == ".":
        return "ok"
    # The path as it is on disk (`rel_path`), not the normalised spelling used to judge the name.
    fp = os.path.join(str(repo_path), str(rel_path))
    try:
        st = os.stat(fp)
    except (OSError, ValueError):
        return "ok"
    if not stat.S_ISREG(st.st_mode):
        return "ok"
    if st.st_nlink > 1:
        return "deny"
    if not _needs_sniff(_fold(rel).rsplit("/", 1)[-1]):
        return "ok"
    if st.st_size > SNIFF_MAX_BYTES:
        return "deny"
    return _sniff_file(fp, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def classify_file(repo_path: Path, rel_path: str) -> Verdict:
    """Like :func:`classify` but also asks the file itself (:func:`content_verdict`)."""
    verdict = classify(rel_path)
    if verdict != "ok":
        return verdict
    return content_verdict(repo_path, rel_path)


def reading_verdict(repo_path: Path | str, rel_path: str, resolved: Path | str) -> Verdict:
    """What a reader that holds ``resolved`` (from :func:`safe_join`) may do with it.

    The strictest of the name as it was asked for, the name it resolved to, and
    its content: a reader must not mask by the REQUESTED name alone, or a link
    called ``notes.txt`` that reaches ``.env.example`` comes back unmasked.
    """
    try:
        inside = Path(resolved).relative_to(Path(repo_path).resolve()).as_posix()
    except (ValueError, OSError):
        return "deny"
    a, b = classify(rel_path), classify(inside)
    if "deny" in (a, b):
        return "deny"
    if "keys_only" in (a, b):
        return "keys_only"
    return content_verdict(repo_path, inside)


def safe_join(repo_path: Path | str, rel: str) -> Path:
    """``repo_path / rel`` or :class:`SecretPathRefused`.

    Refuses a path that leaves the repository (``../``, absolute paths,
    symlinks pointing out), a path that is a secret file, whether named
    directly or reached through a directory link, a file that IS a symlink or
    has another hard link (a link's own name says nothing about its target, and
    git never makes either), and any path the operating system cannot resolve
    (an over-long name, a link loop). All refusals read the same and carry no
    server path, so the answer is not an oracle for what exists.
    """
    try:
        return _safe_join(repo_path, rel)
    except SecretPathRefused:
        raise
    except (OSError, ValueError, RuntimeError):
        # ENAMETOOLONG, ELOOP, an embedded NUL: the message would carry the clone's path.
        raise SecretPathRefused from None


def _safe_join(repo_path: Path | str, rel: str) -> Path:
    root = Path(repo_path).resolve()
    rel_s = str(rel).replace("\\", "/")
    if "\x00" in rel_s:
        raise SecretPathRefused
    if classify(_norm(rel_s)) == "deny":
        raise SecretPathRefused
    raw = root / rel_s
    target = raw.resolve()
    try:
        inside = target.relative_to(root)
    except ValueError:
        raise SecretPathRefused from None
    inside_s = inside.as_posix()
    by_name = classify(inside_s)
    if by_name == "deny":
        raise SecretPathRefused
    try:
        st = os.lstat(raw)
    except (FileNotFoundError, NotADirectoryError):
        return target
    if stat.S_ISLNK(st.st_mode):
        raise SecretPathRefused
    # A `keys_only` file is shown masked whatever it holds; any other file is read
    # for the sniff, and a file with a second name is refused (see content_verdict).
    if stat.S_ISREG(st.st_mode) and by_name == "ok" and content_verdict(root, inside_s) == "deny":
        raise SecretPathRefused
    if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
        raise SecretPathRefused
    return target


def plain_file(repo_path: Path | str, rel: str) -> Path | None:
    """The real path of a regular, non-secret file inside the repository, else None.

    Symlinks are never followed: a committed link can point at ``.env``, at
    another clone or at the server's own files, and the link's own name says
    nothing about its target. A link that stays inside the repository is
    skipped as well; the file it points at is read under its own name.
    """
    try:
        fp = safe_join(repo_path, rel)
    except (SecretPathRefused, OSError, ValueError):
        return None
    try:
        raw = Path(repo_path) / str(rel).replace("\\", "/")
        if raw.is_symlink() or not fp.is_file():
            return None
    except OSError:
        return None
    return fp


# ─── The one question every reader asks ──────────────────────────────


def is_git_internal(rel_path: str) -> bool:
    """True when some segment of ``rel_path`` is a clone's own ``.git`` (however it is
    spelled: ``.GIT``, ``.git `` with a trailing space, a fullwidth dot).

    For a caller that LISTS and drops what it does not keep (an upload): a ``.git/``
    folder is never stored and never read, so it needs no "secret file" refusal.
    """
    return any(
        _fold(_segment(seg)) == ".git" for seg in str(rel_path or "").replace("\\", "/").split("/"))


def is_secret_path(rel_path: str) -> bool:
    """True when ``rel_path`` (repo-relative) must never be shown to anybody.

    The predicate for a reader that cannot show a file masked: the dev tools
    (``find``, ``outline``, ``read_symbol``, ``grep``, ``map``), the Q&A
    retriever (so ``ask`` and the web chat), the plain-text search. It is
    :func:`classify` with ONE difference: ``keys_only`` counts as secret here,
    so ``.env.example`` (which the code reader can show with its values masked)
    is hidden from a caller that cannot mask. Every deny name, directory and
    operator glob is the same list for both, floor included, so nothing is
    hidden by this function and read by another. It applies to an owner too; it
    is independent of the access rules.

    Fails closed: a classifier that raises answers "secret".
    """
    rel = _norm(rel_path or "")
    if not rel or rel == ".":
        return False
    try:
        return classify(rel) != "ok"
    except Exception:  # noqa: BLE001 - fail closed
        return True


def _glob_to_pathspec(glob: str) -> str:
    return f":(exclude,glob,icase)**/{glob}" if "/" not in glob else f":(exclude,glob,icase){glob}"


#: Extensions the pathspecs drop below a SOFT_DENY_DIRS directory. Git cannot say
#: "everything but source", so this is a belt over the braces: `classify` is the
#: rule, and a data file with another extension is still refused by every reader.
_SOFT_DIR_DATA_EXT: tuple[str, ...] = (
    "json", "yml", "yaml", "toml", "ini", "cfg", "conf", "txt", "env", "xml", "properties", "csv",
)


def _build_pathspecs() -> tuple[str, ...]:
    specs: list[str] = []
    for d in sorted(DENY_DIRS):
        specs.append(f":(exclude,glob,icase)**/{d}/**")
    for d in sorted(SOFT_DENY_DIRS):
        for e in _SOFT_DIR_DATA_EXT:
            specs.append(f":(exclude,glob,icase)**/{d}/**/*.{e}")
    for g in DENY_NAME_GLOBS:
        specs.append(_glob_to_pathspec(g))
    for s in DENY_PATH_SUFFIXES:
        specs.append(f":(exclude,glob,icase)**/{s}")
    for s in DENY_ROOT_PATHS:
        specs.append(f":(exclude,glob,icase){s}")
    # keys-only files stay searchable, but only the example names; the
    # concrete variants above (``.env.*``) would swallow them, so re-include
    # nothing here: git pathspec excludes win, and a documented ``.env.example``
    # is served through the code reader, which masks values.
    return tuple(specs)


GIT_PATHSPEC_EXCLUDES: tuple[str, ...] = _build_pathspecs()


# ─── keys-only masking ───────────────────────────────────────────────

_PLACEHOLDER_VALUE = re.compile(
    r"""^(?:
        |<[^>]*>|\$\{[^}]*\}|\$[A-Za-z_]\w*|\{\{.*\}\}|%\([^)]*\)s
        |(?i:changeme|change[-_ ]me|replace[-_ ]?me|your[-_ ].*|xxx+|\*+|todo|none|null|false|true
            |example|placeholder|secret|password|dummy|test|fixme|\.\.\.)
        |-?\d{1,6}
        |(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|[\w.-]+\.(?:local|example\.com|example\.org|internal|test))(?::\d+)?(?:/[\w./-]*)?
    )$""",
    re.VERBOSE,
)
_ENV_LINE = re.compile(r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P<sep>\s*[=:]\s*)(?P<val>.*)$")
# A line that is only a variable name (`DEBUG`, `export PORT`): the one kind of non-assignment
# line that is kept. Upper case only: `sk-live-...` or `ghp_...` alone on a line is a token.
_BARE_NAME = re.compile(r"\s*(?:export\s+)?[A-Z_][A-Z0-9_]*\s*")
_HEREDOC_TAG = re.compile(r"""<<[-~]?[ \t]*(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""")
_BLOCK_SCALAR = re.compile(r"[|>][+-]?[0-9]*[ \t]*(?:#.*)?")
_LINE_WITHHELD = "[line withheld]"
_VALUE_WITHHELD = "[value withheld]"
#: A variable name longer than this is not a name: it is a line of a key body
#: that happened to contain an `=`.
_MAX_KEY_CHARS = 80


#: `KEY=` somewhere in a comment line (`# KEY=value`, `# Set API_KEY=... to`): the key stands at
#: the start of the comment or after a space or an opening bracket, so a `?a=b` in a URL is not one.
_COMMENT_ASSIGN = re.compile(
    r"""^(?P<head>[ \t]*\#.*?(?<![^\s\#("'`,;])(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_.-]*[ \t]*=(?!=)[ \t]*)(?P<val>.*)$""")
#: `# key: value` is prose unless the key is a name a secret goes by (`# NOTE: set it` is a note).
_COMMENT_COLON = re.compile(
    r"^(?P<head>[ \t]*\#+[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:[ \t]*)(?P<val>.*)$")
_SECRETISH_KEY = re.compile(r"pass|pwd|secret|token|key|cred|auth|dsn|salt|signature|private|cert", re.IGNORECASE)
_INLINE_COMMENT = re.compile(r"[ \t]#")
#: A word of a comment that is only a word: letters, a plain number, a little punctuation. A
#: comment made of nothing else is documentation (`# seconds`, `# default 8080`); anything with
#: a digit among its letters, a hyphen or an underscore inside, a colon, a slash or an `@` may be a
#: token or a password in a comment, and is withheld.
_PLAIN_WORD = re.compile(
    r"(?:\(?[A-Za-z]{1,24}(?:'[a-z]{1,3})?|(?:[A-Za-z]\.){1,4}|\d{1,6})[.,;:!?)]*|[-\u2013\u2014]+|[.,;:!?]+")
_MAX_PLAIN_COMMENT_CHARS = 160


def _plain_comment(body: str) -> bool:
    words = body.split()
    return len(body) <= _MAX_PLAIN_COMMENT_CHARS and all(_PLAIN_WORD.fullmatch(w) for w in words)


def _with_comment_masked(value: str) -> str:
    """``value`` (a placeholder) with an inline comment kept only when it is plain words."""
    m = _INLINE_COMMENT.search(value)
    if not m:
        return value
    comment = value[m.start():]
    if _plain_comment(comment.lstrip().lstrip("#")):
        return value
    return f"{value[:m.start()]}{comment[:len(comment) - len(comment.lstrip())]}# [comment withheld]"


def _comment_assignment(line: str) -> tuple[str, str] | None:
    """``(head, value)`` when a comment line carries an assignment, else None."""
    m = _COMMENT_ASSIGN.match(line)
    if m:
        key = re.search(r"([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*$", m.group("head"))
        if key and len(key.group(1)) <= _MAX_KEY_CHARS:
            return m.group("head"), m.group("val")
    m = _COMMENT_COLON.match(line)
    if m and len(m.group("key")) <= _MAX_KEY_CHARS and _SECRETISH_KEY.search(m.group("key")):
        return m.group("head"), m.group("val")
    return None


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def is_placeholder(value: str) -> bool:
    v = _unquote(_INLINE_COMMENT.split(value, 1)[0]).strip()
    return bool(_PLACEHOLDER_VALUE.match(v))


class _Value:
    """Where a value stands after the lines read so far: still open means the
    next line belongs to it (a quoted string, a JSON blob or ``$( ... )``, a
    heredoc, a PEM block, a ``\\`` continuation)."""

    __slots__ = ("cont", "depth", "heredoc", "pem", "quote", "structured")

    def __init__(self) -> None:
        self.quote = ""
        self.depth = 0
        self.heredoc = ""
        self.pem = False
        self.cont = False
        self.structured = False

    @property
    def open(self) -> bool:
        return bool(self.quote or self.depth > 0 or self.heredoc or self.pem or self.cont)

    def feed(self, text: str, *, first: bool) -> None:
        """Read one physical line (of the value, for the first one)."""
        self.cont = False
        begin, end = text.rfind("-----BEGIN "), text.rfind("-----END ")
        if begin > end:
            self.pem = True
        elif end >= 0:
            self.pem = False
        if self.heredoc:
            if text.strip() == self.heredoc:
                self.heredoc = ""
            return
        n = len(text)
        j = n - len(text.lstrip()) if first else 0
        if first and j < n:
            c = text[j]
            if c in "\"'`":
                self.quote = c
                j += 1
            elif c in "{[(" or text.startswith(("$(", "<<"), j):
                self.structured = True
        while j < n:
            ch = text[j]
            if self.quote:
                if ch == "\\":
                    j += 2
                    continue
                if ch == self.quote:
                    self.quote = ""
            elif ch == "\\":
                if j + 1 >= n:
                    self.cont = True
                j += 2
                continue
            elif ch == "#" and (j == 0 or text[j - 1] in " \t") and self.depth == 0:
                break
            elif self.structured:
                if ch in "\"'`":
                    self.quote = ch
                elif ch in "{[(":
                    self.depth += 1
                elif ch in "}])":
                    self.depth = max(0, self.depth - 1)
                elif ch == "<" and text.startswith("<<", j):
                    m = _HEREDOC_TAG.match(text, j)
                    if m:
                        self.heredoc = m.group(2)
                        j = m.end()
                        continue
            j += 1


def mask_env_values(text: str) -> str:
    """Keep every ``KEY=`` and any empty or placeholder value; hide the rest.

    A value runs until it CLOSES, not to the end of its line: a quoted string
    that spans lines, a PEM block, a JSON blob, ``$( ... )``, a heredoc, a YAML
    block scalar and a ``\\`` continuation are withheld whole, continuation lines
    included. The output has one line for each line of the input (a withheld
    continuation line is empty), so a line number read from it still points at
    the same place; a line ends wherever any reader would end it (``\\r``, ``\\f``,
    U+2028 ...), not only at ``\\n``. A value that never closes withholds the rest of
    the file, and a line that is not a ``KEY=value`` pair at all is withheld too: when
    the structure is not understood, nothing after it is trusted.

    A commented-out assignment (``# OLD_KEY=...``) is an assignment: its value is
    withheld unless it is empty or a placeholder, and so is what its value goes on to
    (the lines of a commented PEM block). A comment after a placeholder value is kept
    only when it is plain words (:func:`_plain_comment`); one that could hold a token is
    replaced by ``# [comment withheld]``. A comment line without ``KEY=`` is kept.
    """
    lines = text.splitlines()
    out: list[str] = []
    i, total = 0, len(lines)
    while i < total:
        bare = lines[i]
        stripped = bare.strip()
        commented = stripped.startswith("#")
        if not stripped:
            out.append(bare)
            i += 1
            continue
        if commented:
            found = _comment_assignment(bare)
            if found is None:
                out.append(bare)
                i += 1
                continue
            head, val = found
            m = None
        else:
            m = _ENV_LINE.match(bare)
            if not m or len(m.group("key")) > _MAX_KEY_CHARS:
                out.append(bare if _BARE_NAME.fullmatch(bare) else _LINE_WITHHELD)
                i += 1
                continue
            lead, key, sep, val = m.group("lead"), m.group("key"), m.group("sep"), m.group("val")
            head = f"{lead}{key}{sep}"
            if ":" in sep.replace("=", "") and _BLOCK_SCALAR.fullmatch(val.strip()):
                base = len(bare) - len(bare.lstrip())
                j = i + 1
                while j < total and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > base):
                    j += 1
                out.append(f"{head}{_VALUE_WITHHELD}")
                out.extend([""] * (j - i - 1))
                i = j
                continue
        state = _Value()
        state.feed(val, first=True)
        if not state.open:
            out.append(f"{head}{_with_comment_masked(val)}" if is_placeholder(val) else f"{head}{_VALUE_WITHHELD}")
            i += 1
            continue
        j = i + 1
        while state.open and j < total:
            if commented:
                # a commented-out value goes on over the comment lines that follow, no further
                if not lines[j].lstrip().startswith("#"):
                    break
                state.feed(re.sub(r"^[ \t]*#+[ \t]?", "", lines[j]), first=False)
            else:
                state.feed(lines[j], first=False)
            j += 1
        note = _VALUE_WITHHELD if not state.open or commented else "[value withheld; rest of file withheld]"
        out.append(f"{head}{note}")
        out.extend([""] * (j - i - 1))
        i = j
    return "\n".join(out)


__all__ = [
    "FLOOR_DIR_NAMES",
    "FLOOR_NAME_GLOBS",
    "GIT_PATHSPEC_EXCLUDES",
    "REFUSED",
    "SNIFF_MAX_BYTES",
    "SOFT_DENY_DIRS",
    "SecretPathRefused",
    "classify",
    "classify_file",
    "content_verdict",
    "extra_globs",
    "is_git_internal",
    "is_placeholder",
    "is_secret_path",
    "mask_env_values",
    "plain_file",
    "reading_verdict",
    "safe_join",
]
