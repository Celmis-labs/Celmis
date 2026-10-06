"""Which files in a repository must never be indexed, read or returned.

A clone holds whatever was ever committed, and the occasional ``.env`` or
private key is among it. Code intelligence has no use for the *values* in such
files, and an agent that can read them through MCP is one prompt injection
away from handing them to a stranger. So the decision is made once, here, and
every reader (the indexing walker, the code reader, the dev tools, ``howto``,
the exploration agent) asks the same function:

* ``deny``      the file does not exist as far as Celmis is concerned;
* ``keys_only`` the file documents configuration (``.env.example``): names
                are useful, values are shown only when empty or an obvious
                placeholder (see :func:`mask_env_values`);
* ``ok``        an ordinary file (still redacted on the way out).

The default list cannot be shortened. ``settings.secret_path_globs_extra``
only adds to it.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Literal

Verdict = Literal["deny", "keys_only", "ok"]

REFUSED = "path withheld (secret file)"


class SecretPathRefused(PermissionError):
    """Raised by :func:`safe_join`. ``str(exc)`` never says why."""

    def __init__(self) -> None:
        super().__init__(REFUSED)


# ─── The lists ───────────────────────────────────────────────────────

# Directory names: anything below one of them is denied.
DENY_DIRS: frozenset[str] = frozenset({
    "secrets", ".secrets", ".ssh", ".gnupg", ".aws", ".kube",
})

# File names / extensions, matched on the lower-cased base name.
DENY_NAME_GLOBS: tuple[str, ...] = (
    # env files and key material
    ".env", ".env.*", "*.env", ".envrc", ".env-*", ".env_*", "*.env.local",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx",
    "*.ppk", "*.p8", "*.asc", "*.gpg", "*.pem.*", "*.key.txt",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    # credential stores
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd",
    ".pgpass", ".my.cnf", ".dockercfg", ".s3cfg", "*.keytab",
    ".vault-pass*", ".vault_pass*", "vault-pass*", "vault_pass*", "credentials",
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

# Whole relative paths (suffix match on the normalised path).
DENY_PATH_SUFFIXES: tuple[str, ...] = (
    ".docker/config.json",
)

# The .env files that exist to be read.
KEYS_ONLY_NAMES: frozenset[str] = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    ".env.defaults", ".env.tpl", ".env.test.example", "env.example",
    "example.env", "sample.env", "template.env", "dist.env",
})
KEYS_ONLY_GLOBS: tuple[str, ...] = (
    ".env.*.example", ".env.*.sample", ".env.*.template", ".env.*.dist",
    "*.env.example", "*.env.sample", "*.env.template",
)

_SNIFF_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_SNIFF_SERVICE_ACCOUNT = re.compile(r"\"type\"\s*:\s*\"service_account\"")
_SNIFF_K8S_SECRET = re.compile(r"^kind:\s*[\"']?Secret[\"']?\s*(?:#.*)?$", re.MULTILINE)
_SNIFF_BYTES = 8192


def _extra_globs() -> tuple[str, ...]:
    try:
        from src.config import get_settings

        return tuple(getattr(get_settings(), "secret_path_globs_extra", None) or ())
    except Exception:  # noqa: BLE001 - config trouble must not open the gate
        return ()


_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def _norm(rel_path: str) -> str:
    # A name is judged as it will be READ, not as it was typed: ".env ", ".env."
    # and ".env<zero-width space>" are the env file to every tool that opens it.
    parts = [
        seg.translate(_INVISIBLE).strip().rstrip(". ") or seg.strip()
        for seg in str(rel_path).replace("\\", "/").split("/")
    ]
    rel = "/".join(parts)
    rel = posixpath.normpath(rel) if rel else rel
    return rel.lstrip("/") if rel not in ("", ".") else rel


def classify(rel_path: str, head: bytes | None = None) -> Verdict:
    """Classify a repo-relative path. ``head`` is the file's first bytes, when
    the caller already has them, for the content sniff."""
    rel = _norm(rel_path)
    if not rel or rel == ".":
        return "ok"
    parts = [p.lower() for p in PurePosixPath(rel).parts]
    name = parts[-1]

    if any(p in DENY_DIRS for p in parts[:-1]):
        return "deny"
    low = "/".join(parts)
    if any(low == s or low.endswith("/" + s) for s in DENY_PATH_SUFFIXES):
        return "deny"

    if name in KEYS_ONLY_NAMES or any(fnmatch.fnmatchcase(name, g) for g in KEYS_ONLY_GLOBS):
        return "keys_only"
    if any(fnmatch.fnmatchcase(name, g) for g in DENY_NAME_GLOBS):
        return "deny"
    for g in _extra_globs():
        gl = g.lower()
        if fnmatch.fnmatchcase(low, gl) or fnmatch.fnmatchcase(name, gl):
            return "deny"

    if head:
        text = head[:_SNIFF_BYTES].decode("utf-8", errors="ignore")
        if (
            _SNIFF_PRIVATE_KEY.search(text)
            or _SNIFF_K8S_SECRET.search(text)
            or _SNIFF_SERVICE_ACCOUNT.search(text)
        ):
            return "deny"
    return "ok"


# Only files that are data (not source code) are sniffed: opening every file
# of a large repository on each walk would cost more than it protects, and a
# secret literal inside source code is the redactor's business, not a reason
# to hide the file.
_SNIFF_EXT = frozenset({
    "", ".yml", ".yaml", ".json", ".txt", ".conf", ".cfg", ".ini", ".toml", ".properties",
    ".crt", ".cer", ".b64", ".secret", ".secrets", ".data", ".tpl", ".tmpl", ".sh",
})


def classify_file(repo_path: Path, rel_path: str) -> Verdict:
    """Like :func:`classify` but reads the head of a data file for the sniff."""
    verdict = classify(rel_path)
    if verdict != "ok":
        return verdict
    if PurePosixPath(_norm(rel_path)).suffix.lower() not in _SNIFF_EXT:
        return "ok"
    try:
        fp = Path(repo_path) / rel_path
        if fp.is_file():
            with fp.open("rb") as fh:
                return classify(rel_path, fh.read(_SNIFF_BYTES))
    except OSError:
        return "ok"
    return "ok"


def safe_join(repo_path: Path | str, rel: str) -> Path:
    """``repo_path / rel`` or :class:`SecretPathRefused`.

    Refuses a path that leaves the repository (``../``, absolute paths,
    symlinks pointing out) and a path that is a secret file, whether named
    directly or reached through a symlink. All refusals read the same, so the
    answer is not an oracle for what exists.
    """
    root = Path(repo_path).resolve()
    rel_s = str(rel).replace("\\", "/")
    if "\x00" in rel_s:
        raise SecretPathRefused
    if classify(_norm(rel_s)) == "deny":
        raise SecretPathRefused
    target = (root / rel_s).resolve()
    try:
        inside = target.relative_to(root)
    except ValueError:
        raise SecretPathRefused from None
    inside_s = inside.as_posix()
    if classify(inside_s) == "deny":
        raise SecretPathRefused
    if target.is_file() and classify_file(root, inside_s) == "deny":
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


def _glob_to_pathspec(glob: str) -> str:
    return f":(exclude,glob,icase)**/{glob}" if "/" not in glob else f":(exclude,glob,icase){glob}"


def _build_pathspecs() -> tuple[str, ...]:
    specs: list[str] = []
    for d in sorted(DENY_DIRS):
        specs.append(f":(exclude,glob,icase)**/{d}/**")
    for g in DENY_NAME_GLOBS:
        specs.append(_glob_to_pathspec(g))
    for s in DENY_PATH_SUFFIXES:
        specs.append(f":(exclude,glob,icase)**/{s}")
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


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def is_placeholder(value: str) -> bool:
    v = _unquote(value.split(" #", 1)[0]).strip()
    return bool(_PLACEHOLDER_VALUE.match(v))


def mask_env_values(text: str) -> str:
    """Keep every ``KEY=`` and any empty or placeholder value; hide the rest."""
    out: list[str] = []
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            out.append(line)
            continue
        val = m.group("val")
        if is_placeholder(val):
            out.append(line)
        else:
            out.append(f"{m.group('lead')}{m.group('key')}{m.group('sep')}[value withheld]")
    return "\n".join(out)


__all__ = [
    "GIT_PATHSPEC_EXCLUDES",
    "REFUSED",
    "SecretPathRefused",
    "classify",
    "classify_file",
    "is_placeholder",
    "plain_file",
    "mask_env_values",
    "safe_join",
]
