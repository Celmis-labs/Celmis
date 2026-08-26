"""Multi-host git clone + read-only lock.

Supported providers: Bitbucket, GitHub, GitLab. Detection and URL
normalization live in `git_providers.py`. This module focuses on git
mechanics (clone, pull, progress, read-only lock).

Auth pipeline (priority high→low):
    1. Explicit `api_token` parameter (new, recommended)
    2. Explicit `username + password` (legacy app password style)
    3. Per-provider env vars (BITBUCKET_TOKEN, GITHUB_TOKEN, GITLAB_TOKEN)
    4. Anonymous clone (for public repos)
    5. Git credential helper (Keychain/etc.) — git picks it up itself
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from git import RemoteProgress, Repo
from git.exc import GitCommandError

from src.config import Settings, get_settings
from src.sync.git_providers import (
    GitProvider,
    ParsedRepo,
    build_authenticated_url,
    build_clone_url,
    is_repo_public,
    parse_repo_url,
    strip_credentials,
)

logger = logging.getLogger(__name__)


# Prevents git from silently hanging on a credential prompt in a subprocess
# (without a TTY git cannot ask for a password, but without these env vars
# it just hangs)
_GIT_NO_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "SSH_ASKPASS": "echo",
}


class CloneError(RuntimeError):
    """Clone/pull error with clean stderr (no nested repr)."""

    def __init__(self, stderr: str, original: Exception | None = None) -> None:
        self.stderr = stderr
        self.original = original
        super().__init__(stderr or "git operation failed")


class _ProgressHandler(RemoteProgress):
    """Feeds git progress to the callback + captures non-progress lines (errors)."""

    def __init__(self, callback: Callable[[str], None] | None) -> None:
        super().__init__()
        self._cb = callback
        self._last_msg = ""
        self.dropped_lines: list[str] = []

    def update(self, op_code, cur_count, max_count=None, message=""):
        if self._cb is None:
            return
        op = {
            self.COUNTING: "counting",
            self.COMPRESSING: "compressing",
            self.WRITING: "writing",
            self.RECEIVING: "receiving",
            self.RESOLVING: "resolving",
            self.FINDING_SOURCES: "finding",
            self.CHECKING_OUT: "checkout",
        }.get(op_code & self.OP_MASK, f"op{op_code}")
        msg = (
            f"{op} {int(cur_count)}/{int(max_count)}" if max_count
            else f"{op} {int(cur_count)}"
        )
        if message:
            msg += f" · {message.strip()}"
        if msg != self._last_msg:
            self._last_msg = msg
            # A progress callback must never abort a clone.
            with contextlib.suppress(Exception):
                self._cb(msg)

    def line_dropped(self, line: str) -> None:
        """Git stderr lines that are not progress — typically warnings/errors."""
        line = line.strip()
        if not line:
            return
        self.dropped_lines.append(line)
        if self._cb:
            # A progress callback must never abort a clone.
            with contextlib.suppress(Exception):
                self._cb(f"git: {line}")

    @property
    def captured_stderr(self) -> str:
        return "\n".join(self.dropped_lines)


@dataclass
class SyncResult:
    """Result of a sync operation."""

    repo_slug: str
    path: Path
    commit_sha: str
    changed: bool  # whether there were changes compared to the previous state
    previous_sha: str | None = None
    provider: GitProvider | None = None  # which provider was used


def _resolve_token_with_meta(
    provider: GitProvider,
    settings: Settings,
    *,
    account_label: str = "default",
    user_id: str = "default",
) -> tuple[str | None, dict]:
    """Resolve a token for the provider through the priority chain:
        1. CredentialStore (per-user, then fallback to legacy 'default')
        2. Env vars (BITBUCKET_TOKEN/GITHUB_TOKEN/GITLAB_TOKEN)
        3. settings.bitbucket_token (legacy backward compat)

    Returns None if no token was found — the caller then falls back to a
    public clone or to the credential helper.
    """
    # Path 1: encrypted credential store — try given user, fall back to 'default'
    try:
        from src.credentials import get_credential_store
        store = get_credential_store()
        stored = store.load(
            provider=provider.value,
            user_id=user_id,
            account_label=account_label,
            update_last_used=True,
        )
        if stored is None and user_id != "default":
            stored = store.load(
                provider=provider.value,
                user_id="default",
                account_label=account_label,
                update_last_used=True,
            )
        if stored is not None:
            return stored.secret, dict(stored.metadata or {})
    except Exception as exc:  # noqa: BLE001
        # CredentialStore can fail on a corrupt master key — fall back to env
        logger.debug("credential_store_read_failed err=%s", exc)

    # Path 2: env vars (also used in CI scenarios)
    env_var = {
        GitProvider.BITBUCKET: "BITBUCKET_TOKEN",
        GitProvider.GITHUB: "GITHUB_TOKEN",
        GitProvider.GITLAB: "GITLAB_TOKEN",
    }.get(provider)
    if env_var and (token := os.environ.get(env_var)):
        return token, {}

    # Path 3: settings.bitbucket_token (legacy)
    if provider == GitProvider.BITBUCKET and settings.bitbucket_token:
        return settings.bitbucket_token.get_secret_value(), {}

    return None, {}


def _resolve_token(
    provider: GitProvider,
    settings: Settings,
    *,
    account_label: str = "default",
    user_id: str = "default",
) -> str | None:
    """Secret only — kept for callers that do not need the metadata."""
    token, _meta = _resolve_token_with_meta(
        provider, settings, account_label=account_label, user_id=user_id,
    )
    return token


# Backward-compat alias — some external callers (the orchestrator)
# may still reference it. Remove during a cleanup pass.
_resolve_token_from_env = _resolve_token


def _build_url_for_clone(
    repo: ParsedRepo,
    settings: Settings,
    *,
    override_username: str | None = None,
    override_password: str | None = None,
    api_token: str | None = None,
    user_id: str = "default",
) -> tuple[str, str]:
    """Builds a clone URL with the appropriate authentication.

    Returns:
        (url_for_clone, auth_mode)
        auth_mode — a label for the logs: 'basic' / 'token' for explicit
        credentials, 'anonymous' without them, and for ones resolved from the
        store — whatever `describe_auth` returns ('github:token-url',
        'bitbucket:email+api-token', …), because it is precisely the shape of
        the authentication that explains why the clone failed.
    """
    # Path 1: explicit username+token — MUST be checked before the token-only
    # path. An Atlassian API token carries its email in `username`, and sending
    # it token-only yields Bitbucket's "API token must be used with an
    # atlassian registered email" (i.e. auth failure) instead of a clone.
    if override_username and (override_password or api_token):
        return (
            build_authenticated_url(
                repo, username=override_username,
                password=override_password or api_token,
            ),
            "basic",
        )

    # Path 2: explicit api_token only
    if api_token:
        return build_authenticated_url(repo, token=api_token), "token"

    # Path 3: explicit username + password (legacy)
    if override_username and override_password:
        return (
            build_authenticated_url(repo, username=override_username, password=override_password),
            "basic",
        )

    # Path 3: token from credential store / env / legacy settings
    resolved_token, resolved_meta = _resolve_token_with_meta(
        repo.provider, settings, user_id=user_id,
    )
    if resolved_token:
        # Bitbucket auth shape depends on the TOKEN TYPE, not on settings —
        # see src/credentials/git_auth.py (an ATATT token sent any other way
        # gets "API token must be used with an atlassian registered email").
        from src.credentials.git_auth import describe_auth, git_auth_kwargs

        meta = dict(resolved_meta or {})
        if repo.provider == GitProvider.BITBUCKET and settings.bitbucket_username:
            meta.setdefault("username", settings.bitbucket_username)
        kw = git_auth_kwargs(repo.provider.value, resolved_token, meta)
        label = describe_auth(repo.provider.value, resolved_token, meta)
        if "username" in kw:
            return (
                build_authenticated_url(
                    repo, username=kw["username"], password=kw["password"],
                ),
                label,
            )
        return build_authenticated_url(repo, token=kw["api_token"]), label

    # Path 4: anonymous (public repo) — the git credential helper picks it
    # up if one is configured.
    return build_clone_url(repo), "anonymous"


def _chmod_readonly(path: Path) -> None:
    """Makes the whole tree read-only (555 for dirs, 444 for files).

    This is a safeguard: analyzers physically cannot change the code.
    For the .git directory we leave write on, because git pull needs to write.
    """
    for root, dirs, files in os.walk(path):
        # Skip .git — it has to stay writable for git operations
        if ".git" in Path(root).parts:
            continue
        for d in dirs:
            if d == ".git":
                continue
            dir_path = Path(root) / d
            if ".git" not in dir_path.parts:
                dir_path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        for f in files:
            file_path = Path(root) / f
            if ".git" not in file_path.parts and not file_path.is_symlink():
                file_path.chmod(stat.S_IRUSR | stat.S_IRGRP)


def _chmod_writable(path: Path) -> None:
    """Temporarily restores write mode — needed before git pull.

    Symlinks are skipped, not chmod'ed. `Path.chmod` follows the link, and a
    link whose target is not in the checkout — cal.com commits
    `packages/prisma/.env -> ../../.env` with the target gitignored — raises
    FileNotFoundError from the chmod, which killed every pull of that repo and
    therefore every index of it (six attempts, then dead). The link's own mode
    is irrelevant to git; only the real files need to be writable.
    """
    for root, dirs, files in os.walk(path):
        if ".git" in Path(root).parts:
            continue
        for d in dirs:
            dir_path = Path(root) / d
            if not dir_path.is_symlink():
                dir_path.chmod(0o755)
        for f in files:
            file_path = Path(root) / f
            if not file_path.is_symlink():
                file_path.chmod(0o644)


class RepoSync:
    """Manages the lifecycle of a cloned repo (multi-host)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def clone_or_update(
        self,
        repo_identifier: str,
        branch: str | None = "dev",
        *,
        username: str | None = None,
        password: str | None = None,
        api_token: str | None = None,
        user_id: str = "default",
        progress_callback: Callable[[str], None] | None = None,
    ) -> SyncResult:
        """Clones the repo if it is not there yet, otherwise does a pull.

        Args:
            repo_identifier: any of the forms:
                - 'owner/name' (legacy slug — Bitbucket by default)
                - 'github:owner/name' (explicit prefix)
                - 'https://github.com/owner/name'
                - 'https://github.com/owner/name/tree/main' (browser URL)
                - 'git@github.com:owner/name.git' (SSH)
            branch: target branch. None → clone default branch.
                'dev' default — backward compat for the acme Bitbucket flow.
                If the branch does not exist — fall back to the default branch.
            username/password: legacy basic auth (Bitbucket app password etc.)
            api_token: scoped API token (Bitbucket API token, GitHub PAT, GitLab PAT)
            progress_callback: callable(msg: str) — for UI updates
        """
        # Parse identifier → ParsedRepo
        repo = parse_repo_url(repo_identifier)

        # If the URL was browser-form with a branch hint — and the CLI did not
        # set an explicit branch → take it from the URL. If the CLI set a
        # different one — prefer the CLI.
        if repo.branch_hint and (branch is None or branch == "dev"):
            # 'dev' is the default — we assume the user did not set it explicitly
            if repo.branch_hint != branch:
                logger.info(
                    "branch_hint_from_url url_branch=%s param_branch=%s — using url",
                    repo.branch_hint, branch,
                )
                if progress_callback:
                    progress_callback(
                        f"using branch '{repo.branch_hint}' from URL"
                    )
            branch = repo.branch_hint

        target = self.settings.repo_path(repo.slug)
        url, auth_mode = _build_url_for_clone(
            repo,
            self.settings,
            override_username=username,
            override_password=password,
            api_token=api_token,
            user_id=user_id,
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "sync_start provider=%s slug=%s branch=%s auth=%s",
            repo.provider.value, repo.slug, branch, auth_mode,
        )

        # Pre-flight public-repo check for the anonymous clone scenario:
        # if the API says the repo is private/not-found — fail fast with a
        # clear error, avoiding the confusing 'Repository not found' that git
        # gives without a TTY.
        # If the API is unavailable (None) — skip it, let git fail on its own.
        if auth_mode == "anonymous" and not target.exists():
            public_status = is_repo_public(repo)
            if public_status is False:
                raise CloneError(
                    f"Repository {repo.full_path} ({repo.provider.value}) "
                    f"is private or not found, but no credentials provided. "
                    f"Add token via env var "
                    f"({repo.provider.value.upper()}_TOKEN) or pass `api_token`."
                )

        # Set env vars for all git operations in the current process.
        # Without this a git subprocess can wait FOREVER on a credential prompt.
        for k, v in _GIT_NO_PROMPT_ENV.items():
            os.environ[k] = v

        if target.exists() and (target / ".git").exists():
            result = self._pull(target, branch, progress_callback)
        else:
            result = self._clone(url, target, branch, progress_callback)

        # Read-only lock
        _chmod_readonly(target)
        logger.info(
            "sync_done provider=%s slug=%s sha=%s changed=%s",
            repo.provider.value, repo.slug, result.commit_sha, result.changed,
        )
        return SyncResult(
            repo_slug=repo.slug,
            path=target,
            commit_sha=result.commit_sha,
            changed=result.changed,
            previous_sha=result.previous_sha,
            provider=repo.provider,
        )

    def _clone(
        self,
        url: str,
        target: Path,
        branch: str | None,
        progress_callback: Callable[[str], None] | None,
    ) -> SyncResult:
        # Pre-flight branch existence check — a quick ls-remote instead of
        # parsing GitPython error messages (they are not captured consistently
        # with --progress).
        # If the given branch does not exist — fall back to the default branch
        # (= None) without cloning into a failed state.
        if branch is not None and not _remote_branch_exists(url, branch):
            logger.info(
                "remote_branch_missing url=%s branch=%s — falling back to default",
                strip_credentials(url), branch,
            )
            if progress_callback:
                progress_callback(
                    f"branch '{branch}' not on remote, using default branch"
                )
            branch = None

        logger.info("cloning %s → %s (branch=%s)", strip_credentials(url), target, branch)
        if progress_callback:
            depth_msg = f"shallow, depth={self.settings.git_clone_depth}"
            progress_callback(f"starting clone ({depth_msg}, branch={branch or 'default'})")

        progress = _ProgressHandler(progress_callback)
        try:
            clone_kwargs: dict[str, object] = {
                "url": url,
                "to_path": str(target),
                "depth": self.settings.git_clone_depth,
                "filter": self.settings.git_clone_filter,
                "progress": progress,
                "single_branch": True,
            }
            if branch is not None:
                clone_kwargs["branch"] = branch
            repo = Repo.clone_from(**clone_kwargs)
        except GitCommandError as exc:
            stderr = strip_credentials(_combine_git_errors(progress, exc))
            logger.error("clone_failed: stderr=%s", stderr[:500])
            raise CloneError(stderr, original=exc) from exc

        sha = repo.head.commit.hexsha
        return SyncResult(repo_slug="", path=target, commit_sha=sha, changed=True, previous_sha=None)

    def _pull(
        self,
        target: Path,
        branch: str | None,
        progress_callback: Callable[[str], None] | None,
    ) -> SyncResult:
        logger.info("pulling %s (branch=%s)", target, branch)
        if progress_callback:
            progress_callback("fetching updates")
        _chmod_writable(target)
        progress = _ProgressHandler(progress_callback)
        try:
            repo = Repo(str(target))
            prev_sha = repo.head.commit.hexsha
            origin = repo.remotes.origin
            origin.fetch(progress=progress)
            # If no branch was given — use the currently active branch
            target_branch = branch or (
                repo.active_branch.name if not repo.head.is_detached else None
            )
            # An explicit branch on an EXISTING clone = a branch change (the
            # repo was reconfigured). The clone was made with --single-branch,
            # so the origin refspec is narrowed and `checkout dev` would fail
            # with "pathspec did not match" — we have to fetch the branch
            # separately.
            explicit = branch is not None and self._ensure_remote_branch(
                repo, branch, progress_callback,
            )
            if branch is not None and not explicit:
                target_branch = (
                    repo.active_branch.name if not repo.head.is_detached else None
                )
            if target_branch is None:
                # detached HEAD — just fetch + leave it as is
                logger.warning("repo in detached HEAD state, skipping checkout")
                new_sha = repo.head.commit.hexsha
            else:
                if explicit:
                    # -B, not a bare checkout: DWIM ("checkout dev" → create a
                    # local branch from origin/dev) looks at the CONFIGURED
                    # refspec, which in a single-branch clone contains only the
                    # original branch — so for a new branch it silently does
                    # not fire.
                    repo.git.checkout(
                        "--force", "-B", target_branch, f"origin/{target_branch}",
                    )
                else:
                    repo.git.checkout(target_branch)
                repo.git.reset("--hard", f"origin/{target_branch}")
                new_sha = repo.head.commit.hexsha
            return SyncResult(
                repo_slug="",
                path=target,
                commit_sha=new_sha,
                changed=(new_sha != prev_sha),
                previous_sha=prev_sha,
            )
        except GitCommandError as exc:
            stderr = strip_credentials(_combine_git_errors(progress, exc))
            logger.error("pull_failed: %s", stderr[:500])
            raise CloneError(stderr, original=exc) from exc

    def _ensure_remote_branch(
        self,
        repo,
        branch: str,
        progress_callback: Callable[[str], None] | None,
    ) -> bool:
        """Refresh `origin/{branch}` so `checkout` can switch to it.

        Always fetches the explicit refspec rather than trusting the previous
        `origin.fetch()`: a --single-branch clone keeps a NARROW
        `remote.origin.fetch` config, so the plain fetch only ever updates the
        branch the repo was cloned on. Without this an explicitly configured
        branch would be checked out once and then silently never advance.

        Returns False when the branch cannot be fetched (renamed/deleted on the
        remote) and no stale copy exists — the caller then keeps the branch the
        clone is already on instead of failing the whole sync on a stale config
        value.
        """
        if progress_callback:
            progress_callback(f"fetching branch '{branch}'")
        try:
            repo.git.fetch(
                "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                "--depth", str(self.settings.git_clone_depth),
            )
            return True
        except GitCommandError as exc:
            logger.warning(
                "branch_fetch_failed path=%s branch=%s err=%s",
                repo.working_dir, branch, strip_credentials(str(exc))[:300],
            )
        try:  # already fetched earlier? then work off that copy
            repo.git.rev_parse("--verify", "--quiet", f"refs/remotes/origin/{branch}")
            return True
        except GitCommandError:
            if progress_callback:
                progress_callback(
                    f"branch '{branch}' not on remote, keeping current branch"
                )
            return False


def _remote_branch_exists(url: str, branch: str, *, timeout: float = 15.0) -> bool:
    """Quick pre-flight check via `git ls-remote --heads URL branch`.

    Avoids having to parse GitPython error messages (which do not consistently
    propagate through RemoteProgress when the --progress flag is active).

    Returns:
        True  — the branch exists on the remote
        False — the branch does NOT exist on the remote (we cleanly know that
                a fallback is needed)
        True  — on a network/timeout error: we optimistically go on with the
                clone, let git itself fail with the real error if the network
                is down
    """
    env = {**os.environ, **_GIT_NO_PROMPT_ENV}
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", url, branch],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # `e` stripped too, not only `url`. `TimeoutExpired.__str__` is
        # "Command '<argv>' timed out after Ns" and that argv is the very
        # command built two lines up — so the token stripped out of the first
        # argument was being printed in full in the third.
        logger.warning(
            "ls_remote_failed url=%s branch=%s err=%s — assuming branch exists",
            strip_credentials(url), branch, strip_credentials(str(e))[:300],
        )
        return True

    if result.returncode != 0:
        # Auth-related failures, network issues, etc. — optimistically clone
        logger.debug(
            "ls_remote_nonzero url=%s branch=%s rc=%d stderr=%s",
            strip_credentials(url), branch, result.returncode,
            strip_credentials(result.stderr)[:200],
        )
        return True

    # ls-remote prints '<sha>\trefs/heads/<branch>' for branches that exist,
    # and nothing when the branch does not exist
    return bool(result.stdout.strip())


def _combine_git_errors(progress: _ProgressHandler, exc: GitCommandError) -> str:
    """Combine the progress-captured stderr (warnings, network) with the actual
    git fatal error from GitCommandError.stderr. The two sources are often
    complementary — progress holds connection issues, exc.stderr holds
    branch/auth errors.
    """
    parts = []
    captured = progress.captured_stderr.strip() if progress.captured_stderr else ""
    extracted = _extract_git_stderr(exc).strip()
    if captured:
        parts.append(captured)
    if extracted and extracted not in captured:
        parts.append(extracted)
    return "\n".join(parts) or "git operation failed"


def _extract_git_stderr(exc: GitCommandError) -> str:
    """Extracts stderr from GitCommandError — sometimes bytes, sometimes str."""
    stderr = getattr(exc, "stderr", None)
    if stderr is None:
        return str(exc)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    stderr = stderr.strip()
    for prefix in ("stderr: ", "'stderr: "):
        if stderr.startswith(prefix):
            stderr = stderr[len(prefix) :].strip("'")
    return stderr or str(exc)


# Module-level convenience function
def clone_or_update(
    repo_identifier: str,
    branch: str | None = "dev",
    *,
    username: str | None = None,
    password: str | None = None,
    api_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> SyncResult:
    return RepoSync().clone_or_update(
        repo_identifier,
        branch,
        username=username,
        password=password,
        api_token=api_token,
        progress_callback=progress_callback,
    )


def list_synced_repos(settings: Settings | None = None) -> list[dict]:
    """Returns the list of already cloned repos with basic meta.

    Works for every provider — slug detection via _detect_provider_from_slug.
    """
    from git import Repo as _Repo

    s = settings or get_settings()
    out: list[dict] = []
    if not s.repos_dir.exists():
        return out
    for sub in sorted(s.repos_dir.iterdir()):
        if not (sub / ".git").exists():
            continue
        try:
            r = _Repo(str(sub))
            commit = r.head.commit
            branch = r.active_branch.name if not r.head.is_detached else "(detached)"
            provider = _detect_provider_from_slug(sub.name)
            out.append(
                {
                    "slug": sub.name,
                    "path": str(sub),
                    "provider": provider,
                    "branch": branch,
                    "commit_sha": commit.hexsha,
                    "commit_short": commit.hexsha[:8],
                    "commit_message": commit.message.splitlines()[0] if commit.message else "",
                    "vault_exists": s.repo_vault_path(sub.name).exists(),
                    "vault_notes": _count_vault_notes(s.repo_vault_path(sub.name)),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cant read repo %s: %s", sub.name, exc)
    return out


def _detect_provider_from_slug(slug: str) -> str:
    """Reverse: slug → provider.

    'github_foo-bar'    → 'github'
    'gitlab_grp-repo'   → 'gitlab'
    'acme-frontend' → 'bitbucket' (legacy default)
    """
    if slug.startswith("github_"):
        return "github"
    if slug.startswith("gitlab_"):
        return "gitlab"
    return "bitbucket"


def _count_vault_notes(vault_path: Path) -> int:
    if not vault_path.exists():
        return 0
    return sum(1 for _ in vault_path.rglob("*.md"))
