"""Which files a dev tool may never show, whoever asks.

Secrets live in files that were committed by mistake or on purpose (`.env`,
keys, state files). A code-search tool that indexes the repository would
otherwise hand them to anyone with read access to the code. The rule here is
independent of the access rules: it applies to an owner too.

The rule itself lives in ``src.security.secret_files`` (:func:`is_secret_path`,
:data:`GIT_PATHSPEC_EXCLUDES`) so that the Q&A retriever (``ask``, the web chat)
and the plain-text search ask the very same question as the dev tools: every
tool that reads a file or lists a path asks :func:`is_secret_path`, and `grep`
passes :func:`git_excludes` to git so the file is never even opened.
"""

from __future__ import annotations

from src.security.secret_files import (
    GIT_PATHSPEC_EXCLUDES,
    extra_globs,
    is_secret_path,
)

__all__ = ["git_excludes", "is_secret_path", "is_unsafe_rel_path"]


def git_excludes() -> tuple[str, ...]:
    """Pathspec magic that makes git skip secret files (`:(exclude,glob,icase)...`).

    The built-in lists are ``GIT_PATHSPEC_EXCLUDES`` (floor included, case-blind
    like the classifier); the operator's globs are added the same way. It is a
    belt: what git still returns is dropped by ``is_secret_path`` afterwards, so
    a name a pathspec cannot express (a source-only exception in ``secret/``)
    is decided there, by the one classifier.
    """
    out: list[str] = list(GIT_PATHSPEC_EXCLUDES)
    for g in extra_globs():
        out.append(f":(exclude,glob,icase)**/{g}")
    return tuple(dict.fromkeys(out))


def is_unsafe_rel_path(rel_path: str) -> bool:
    """A path that could leave the repository, or be mistaken for an option."""
    p = rel_path or ""
    return (
        not p or "\0" in p or p.startswith(("/", "-", "~"))
        or ".." in p.replace("\\", "/").split("/")
    )
