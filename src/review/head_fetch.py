"""Make the head commit of an OPEN pull request readable in the local clone.

Why this exists:
    Three review features read repository code at the pull request's head sha
    from the local clone of the code graph (`graph_context._clone_root`):
    `code_context` (enclosing functions, call sites), `verifier_evidence` (what
    the veto greps) and `structural_constants` (the undefined class constant
    check). The indexer keeps that clone at the default branch, so for an open
    pull request the head commit is usually NOT in it: call sites were dropped,
    the veto searched the default branch, and the constant check stayed silent
    - on exactly the pull requests the review exists for.

What it does (once per review, before those consumers):
    1. Finds the clone the way the consumers do. No clone: nothing to do.
    2. Head commit already there: nothing to do.
    3. `git fetch --no-tags --quiet origin +refs/heads/<source>:<private ref>`
       and, when the sha is still missing (the branch moved on, was deleted, or
       lives in a fork), `git fetch origin +<sha>:<private ref>`.
    The private ref is `refs/celmis/pr-heads/<number>`. It is outside
    `refs/heads`, so no local branch, no HEAD and no file of the working tree
    is touched (git itself may also refresh a matching `refs/remotes/origin/*`
    ref, as any fetch of that branch does): the indexer's checkout is
    exactly what it was. The ref only keeps the fetched objects from being
    garbage-collected while the review reads them; the newest
    `KEEP_PRIVATE_REFS` of them are kept and older ones are deleted.

Authentication - the same as the freshness check and the indexer:
    A credential from the credential store (workspace, then user), else the
    provider's token env var / legacy setting, turned into a username and
    password by `src.credentials.git_auth.git_auth_kwargs` (the username slot
    depends on the token type) and handed to git through a one-shot credential
    helper that reads `CELMIS_GIT_USER`/`CELMIS_GIT_PW` from the process
    environment, the helper list cleared first. The secret is never in argv, in
    `.git/config` or in a log line; every message that leaves this module goes
    through `strip_credentials`. A self-hosted GitLab adds its pinned address
    and CA bundle through `instance.git_config()`, after the egress check.

Concurrency:
    One thread lock per clone (reviews in this process) and an advisory
    `flock` on `<gitdir>/celmis-review-fetch.lock` (other processes: a second
    worker). The indexer takes no such lock; git's own `index.lock`/ref locks
    protect it, and this fetch touches neither the index nor any branch ref.
    The wait for the lock and the fetch share ONE deadline,
    `fetch_head_timeout_s`.

Fail-open, always:
    Any failure - no credential, network, timeout, lock busy - ends in
    `HeadFetch(fetched=False, ...)` with a short reason and the review goes on
    with the behaviour it had before. The result becomes the stage
    "Fetch pull request head" so the run record says whether the head was
    available ("head fetched into clone" / "unavailable: <reason>") and which
    revision the consumers read for the PR's code (`code_revision`: the head,
    the PR's base commit plus its diff, or the clone's HEAD labelled as newer
    than the pull request).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STAGE_KEY = "head_fetch"
STAGE_NAME = "Fetch pull request head"

PRIVATE_REF_PREFIX = "refs/celmis/pr-heads/"
#: How many private refs to keep; older ones (by commit date) are deleted.
KEEP_PRIVATE_REFS = 50
LOCK_FILE = "celmis-review-fetch.lock"

_SHA = re.compile(r"[0-9a-f]{7,64}")
#: A branch name git would accept AND that cannot be read as an option or a
#: refspec operator (`:`, `+`, `^`, `~`, `?`, `*`, `[`, `\\`, whitespace).
_BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./@-]*")

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class HeadFetch:
    """What the step found out. `available` is what the consumers care about."""

    #: True when the head commit is readable in the clone afterwards.
    available: bool
    #: "present" | "fetched" | "unavailable" | "skipped"
    state: str
    reason: str = ""
    #: Which revision the consumers will read for "the PR's code", per
    #: `code_revision.resolve_code_revision`: "head" | "base_plus_diff" |
    #: "clone_head" | "none". Filled in by `ensure_head_in_clone`.
    source_kind: str = ""
    source_label: str = ""

    @property
    def stage_status(self) -> str:
        return "success" if self.available else "skipped"

    @property
    def sentence(self) -> str:
        base = self._state_sentence()
        if self.source_kind in ("base_plus_diff", "clone_head"):
            return f"{base}; code read from: {self.source_label}"
        return base

    def _state_sentence(self) -> str:
        if self.state == "fetched":
            return "head fetched into clone"
        if self.state == "present":
            return "head already in clone"
        if self.state == "skipped":
            return f"skipped: {self.reason}"
        return f"unavailable: {self.reason}"


def _clean(text: str) -> str:
    from src.sync.git_providers import strip_credentials

    return strip_credentials((text or "").strip())[:200]


def _has_commit(root: Path, sha: str) -> bool:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, timeout=20, check=False,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _find_clone(pr: Any) -> Path | None:
    # The clone's location lives on the application settings (`repo_path`),
    # not on the review settings: asking the review settings found no
    # clone on a real server, and every consumer of it went quiet.
    from src.config import get_settings
    from src.review.graph_context import _clone_root, graph_slug_candidates

    settings = get_settings()
    for slug in graph_slug_candidates(pr):
        root = _clone_root(slug, settings)
        if root is not None and (root / ".git").exists():
            return root
    return None


def _git_env(pr: Any, *, workspace_id: str, user_id: str) -> dict[str, str]:
    """Environment for the fetch: no prompt, plus the credential (see module doc)."""
    from src.sync.clone import _GIT_NO_PROMPT_ENV, _git_config_env

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CELMIS_GIT_", "GIT_CONFIG_"))}
    env.update(_GIT_NO_PROMPT_ENV)
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
    provider = str(getattr(pr, "provider", "") or "").lower()

    auth: dict[str, str] | None = None
    creds = None
    try:
        from src.credentials import resolve_git_credential

        creds = resolve_git_credential(provider, user_id=user_id, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("head_fetch_credential_lookup_failed err=%s", _clean(str(exc)))
    if creds is None:
        # The indexer's own chain: token env var / legacy setting.
        from types import SimpleNamespace

        from src.config import get_settings
        from src.sync.clone import _resolve_token_with_meta
        from src.sync.git_providers import GitProvider

        try:
            token, meta = _resolve_token_with_meta(
                GitProvider(provider), get_settings(), user_id=user_id)
        except Exception:  # noqa: BLE001
            token, meta = None, {}
        if token:
            creds = SimpleNamespace(secret=token, metadata=meta)
    if creds is not None:
        from src.repos.freshness import _basic_auth

        auth = _basic_auth(provider, creds)

    pairs: list[tuple[str, str]] = []
    if auth:
        from src.sync.clone import _ENV_CREDENTIAL_HELPER

        # Cleared first: `credential.helper` APPENDS, and a keychain helper
        # would otherwise answer before ours.
        pairs += [("credential.helper", ""), ("credential.helper", _ENV_CREDENTIAL_HELPER)]
        env.update(auth)
    if provider == "gitlab":
        from src.http import allowed_hosts
        from src.security.egress import assert_url_allowed
        from src.sync.git_providers import build_clone_url, parse_repo_url
        from src.sync.gitlab_instance import DEFAULT_INSTANCE, instance_for_credential

        instance = instance_for_credential(creds) if creds is not None else DEFAULT_INSTANCE
        url = build_clone_url(parse_repo_url(f"gitlab:{pr.repo}"),
                              gitlab_base_url=instance.base_url)
        assert_url_allowed(url, allowed_hosts(() if instance.is_default else (instance.host,)))
        pairs += instance.git_config()
    if pairs:
        env.update(_git_config_env(pairs))
    return env


@contextlib.contextmanager
def _clone_lock(root: Path, deadline: float):
    """Thread lock + advisory file lock for `root`; TimeoutError past `deadline`."""
    key = str(root.resolve())
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not tlock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("clone busy (another review is fetching)")
    fh = None
    try:
        gitdir = root / ".git"
        if gitdir.is_dir():
            try:
                import fcntl

                fh = open(gitdir / LOCK_FILE, "a")  # noqa: SIM115 - closed below
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("clone busy (lock held by another process)") from None
                        time.sleep(0.2)
            except ImportError:  # no fcntl (Windows): the thread lock only
                pass
        yield
    finally:
        if fh is not None:
            fh.close()  # releases the flock
        tlock.release()


def _run_git(root: Path, args: list[str], env: dict[str, str], deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out")
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, env=env, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("timed out") from None
    if proc.returncode != 0:
        raise RuntimeError(_clean(proc.stderr) or f"git exit {proc.returncode}")


def _prune(root: Path) -> None:
    """Delete private refs beyond the newest `KEEP_PRIVATE_REFS`. Best effort."""
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "for-each-ref", "--sort=-committerdate",
         "--format=%(refname)", PRIVATE_REF_PREFIX],
        capture_output=True, text=True, timeout=20, check=False,
    ).stdout.split()
    for ref in out[KEEP_PRIVATE_REFS:]:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "update-ref", "-d", ref],
            capture_output=True, timeout=20, check=False,
        )


def ensure_head_in_clone(pr: Any, *, workspace_id: str = "default",
                         user_id: str = "default", timeout_s: float | None = None) -> HeadFetch:
    """Best effort: make `pr.head_sha` readable in the clone. Never raises."""
    try:
        result = _ensure(pr, workspace_id, user_id, timeout_s)
    except Exception as exc:  # noqa: BLE001 - fail open, always
        result = HeadFetch(False, "unavailable", _clean(f"{type(exc).__name__}: {exc}"))
    # Say which revision the consumers will fall back to when the head is not
    # readable (the PR's base commit plus its diff, or the NEWER clone HEAD).
    try:
        import dataclasses

        from src.review.code_revision import resolve_code_revision

        revision = resolve_code_revision(pr, _find_clone(pr))
        result = dataclasses.replace(
            result, source_kind=revision.kind, source_label=revision.label)
    except Exception:  # noqa: BLE001
        logger.debug("head_fetch_source_failed", exc_info=True)
    return result


def _ensure(pr: Any, workspace_id: str, user_id: str, timeout_s: float | None) -> HeadFetch:
    if timeout_s is None:
        from src.review.settings import get_review_settings

        timeout_s = float(get_review_settings().fetch_head_timeout_s)
    if timeout_s <= 0:
        return HeadFetch(False, "skipped", "disabled (REVIEW_FETCH_HEAD_TIMEOUT_S=0)")
    sha = str(getattr(pr, "head_sha", "") or "").lower()
    if not _SHA.fullmatch(sha):
        return HeadFetch(False, "skipped", "no valid head sha")
    root = _find_clone(pr)
    if root is None:
        return HeadFetch(False, "skipped", "no local clone")
    if _has_commit(root, sha):
        return HeadFetch(True, "present")

    deadline = time.monotonic() + timeout_s
    ref = f"{PRIVATE_REF_PREFIX}{int(getattr(pr, 'number', 0) or 0)}"
    branch = str(getattr(pr, "head_ref", "") or "")
    try:
        env = _git_env(pr, workspace_id=workspace_id, user_id=user_id)
        with _clone_lock(root, deadline):
            # The indexer, or another review, may have brought it meanwhile.
            if _has_commit(root, sha):
                return HeadFetch(True, "present")
            errors: list[str] = []
            if _BRANCH.fullmatch(branch) and ".." not in branch:
                try:
                    _run_git(root, ["fetch", "--no-tags", "--quiet", "origin",
                                    f"+refs/heads/{branch}:{ref}"], env, deadline)
                except RuntimeError as exc:
                    errors.append(str(exc))
            else:
                errors.append("source branch name not usable")
            if not _has_commit(root, sha):
                try:
                    _run_git(root, ["fetch", "--no-tags", "--quiet", "origin",
                                    f"+{sha}:{ref}"], env, deadline)
                except RuntimeError as exc:
                    errors.append(str(exc))
            if _has_commit(root, sha):
                with contextlib.suppress(Exception):
                    _prune(root)
                return HeadFetch(True, "fetched")
            reason = errors[-1] if errors else "commit not on the remote"
            return HeadFetch(False, "unavailable", _clean(reason))
    except TimeoutError as exc:
        return HeadFetch(False, "unavailable", str(exc))


def record_head_fetch(stages: Any, result: HeadFetch) -> None:
    """Write the result as one stage; never raises."""
    try:
        stages.add(STAGE_KEY, STAGE_NAME, result.stage_status, result.sentence,
                   meta={"state": result.state, "code_source": result.source_kind})
    except Exception:  # noqa: BLE001
        logger.debug("head_fetch_stage_failed", exc_info=True)
