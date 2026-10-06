"""Which files a dev tool may never show, whoever asks.

Secrets live in files that were committed by mistake or on purpose (`.env`,
keys, state files). A code-search tool that indexes the repository would
otherwise hand them to anyone with read access to the code. The rule here is
independent of the access rules: it applies to an owner too.

Single source of truth for the profile: every tool that reads a file or lists
a path asks :func:`is_secret_path`, and `grep` passes :func:`git_excludes` to
git so the file is never even opened.

INTEGRATION: the central policy lives in ``src.security.secret_files``
(`classify`, `GIT_PATHSPEC_EXCLUDES`). When that module is present it is used
and this list is only the floor under it; when it is absent (this lane was
built without it) the floor alone applies.
"""

from __future__ import annotations

import fnmatch
import logging
from functools import cache

logger = logging.getLogger(__name__)

#: Basename patterns (fnmatch, case-insensitive).
_NAME_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".htpasswd", "*.tfstate",
    "*.tfstate.backup", "*.tfvars", "credentials", "credentials.*", "*credentials*.json",
    "service-account*.json", "serviceaccount*.json", "secrets.yml",
    "secrets.yaml", "secrets.json", "secrets.toml", "*.gpg", "*.asc",
)

#: Directory names under which nothing is shown.
_DIR_NAMES = frozenset({
    "secrets", ".secrets", "secret", ".ssh", ".gnupg", ".aws", ".kube",
    "private_keys",
})


def _extra() -> tuple[str, ...]:
    try:
        from src.config import get_settings

        return tuple(getattr(get_settings(), "secret_path_globs_extra", ()) or ())
    except Exception:  # noqa: BLE001
        return ()


def is_secret_path(rel_path: str) -> bool:
    """True when `rel_path` (repo-relative, forward slashes) must never be shown."""
    rel = (rel_path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return False
    central = _central()
    if central is not None:
        try:
            if central.classify(rel) != "ok":
                return True
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("secret_files_classify_failed err=%s", exc)
            return True
    parts = rel.split("/")
    name = parts[-1].lower()
    if any(fnmatch.fnmatch(name, g) for g in _NAME_GLOBS):
        return True
    if any(p.lower() in _DIR_NAMES for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(name, g) for g in _extra())


@cache
def _central():
    try:
        from src.security import secret_files

        return secret_files if hasattr(secret_files, "classify") else None
    except Exception:  # noqa: BLE001
        return None


def git_excludes() -> tuple[str, ...]:
    """Pathspec magic that makes git skip secret files (`:(exclude,glob)...`)."""
    out: list[str] = []
    central = _central()
    if central is not None:
        out.extend(getattr(central, "GIT_PATHSPEC_EXCLUDES", ()) or ())
    for g in _NAME_GLOBS:
        out.append(f":(exclude,glob)**/{g}")
    for d in sorted(_DIR_NAMES):
        out.append(f":(exclude,glob)**/{d}/**")
    for g in _extra():
        out.append(f":(exclude,glob)**/{g}")
    return tuple(dict.fromkeys(out))


def is_unsafe_rel_path(rel_path: str) -> bool:
    """A path that could leave the repository, or be mistaken for an option."""
    p = rel_path or ""
    return (
        not p or "\0" in p or p.startswith(("/", "-", "~"))
        or ".." in p.replace("\\", "/").split("/")
    )
