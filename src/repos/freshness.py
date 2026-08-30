"""Is the indexed graph still the code that is on the branch?

`repo_index_state` recorded what was indexed and when. It could not answer the
question a person actually opens the page to ask — *is this current?* — and
the product answered questions from whatever was indexed the last time
somebody pressed a button, silently.

The gap was not the machinery. `src/sync/incremental.py` has walked
`git diff last_sha..HEAD` since it was written, and the queue has had a
handler registered for it. Nothing ever enqueued the job: every call site
enqueued `index_repo_full`, the webhook parsed pull-request events only, and
the poller looked for pull requests to review rather than commits to index.
So a fully-built incremental indexer sat connected to nothing.

WHAT THIS ASKS AND HOW. `git ls-remote <url> <ref>` — one network round trip,
no clone, no fetch, no working tree. It answers with the sha the branch points
at right now. Comparing that to `last_indexed_sha` is the entire check.

THE CREDENTIAL DOES NOT GO IN THE URL. `ls-remote` on a private repository
needs one, and putting it in the argument would place it in `ps auxww` for
every check on every repository every day — the exposure that
`src/agent/workspace.py` was carrying until this week, multiplied by a
schedule. It travels in the environment, read by a credential helper, with the
inherited helper list cleared first: `-c credential.helper=<x>` APPENDS, so a
helper configured in the image would otherwise answer first and hand git a
credential belonging to somebody else.

THREE OUTCOMES, NOT TWO. Up to date, behind, and *could not tell* — a remote
that refused, a repository with no recorded revision, a network that failed.
The third is reported as itself. A check that cannot reach the remote and
renders as "no new changes" is worse than no check, because it answers the
question wrongly with the authority of a fresh timestamp.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

#: One network round trip, and a bound on how long it may take. A remote that
#: hangs must not hold a scheduler tick or an HTTP request open.
LS_REMOTE_TIMEOUT_SECONDS = 25

#: Hand git the credential without putting it in argv. The empty value first
#: is load-bearing — see the module docstring.
_CREDENTIAL_ARGS = [
    "-c", "credential.helper=",
    "-c", ('credential.helper=!f() { echo "username=$CELMIS_GIT_USER"; '
           'echo "password=$CELMIS_GIT_PW"; }; f'),
]

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FreshnessCheck:
    """What one look at the remote learned."""

    repo_slug: str
    #: "up_to_date" | "behind" | "never_indexed" | "unreachable"
    state: str
    remote_sha: str | None = None
    indexed_sha: str | None = None
    detail: str | None = None
    #: Set when the check queued an incremental re-index.
    reindex_job_id: str | None = None
    checked_at: datetime | None = None

    @property
    def changed(self) -> bool:
        return self.state == "behind"

    @property
    def known(self) -> bool:
        """False when the check could not reach a conclusion.

        Callers reporting "no new changes" must gate on this. `state !=
        "behind"` is not the same test: `unreachable` is also not "behind",
        and saying "no new changes" for a remote nobody could reach is the
        confident-wrong answer this module is built to avoid.
        """
        return self.state in ("up_to_date", "behind")


def _run_ls_remote(url: str, ref: str, env_extra: dict[str, str] | None) -> str:
    """The sha `ref` points at on `url`. Raises on any failure."""
    import os

    env = {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        # ls-remote over https; no ssh agent, no known_hosts prompt.
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=no",
    }
    env.update(env_extra or {})
    args = ["git", *(_CREDENTIAL_ARGS if env_extra else []), "ls-remote", url, ref]
    proc = subprocess.run(
        args, capture_output=True, text=True,
        timeout=LS_REMOTE_TIMEOUT_SECONDS, env=env,
    )
    if proc.returncode != 0:
        from src.sync.git_providers import strip_credentials
        raise RuntimeError(strip_credentials(proc.stderr.strip()[:300]) or "ls-remote failed")

    # THE MATCH IS ON THE REF NAME, NOT ON LINE ORDER. `ls-remote <url> main`
    # is a glob: on a repository that also has `feature/main` it answers with
    # BOTH, sorted by refname, and `feature/main` sorts first. Taking line
    # zero silently compared against the wrong branch — a repository that
    # would then look permanently behind, or permanently current, depending on
    # which neighbour it happened to have.
    # `HEAD` is a ref name in its own right — a remote answers a HEAD query
    # with a line literally named HEAD — so it must not be qualified into the
    # branch namespace.
    wanted = ref if (ref == "HEAD" or ref.startswith("refs/")) else f"refs/heads/{ref}"
    for line in (proc.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        sha, name = sha.strip(), name.strip()
        if _SHA.match(sha) and name == wanted:
            return sha
    # No matching line means the ref does not exist on the remote — a renamed
    # default branch, a deleted branch. A real condition with a real remedy,
    # and emphatically not "no changes".
    raise RuntimeError(f"remote has no ref {wanted!r}")


def _checked_out_branch(repo_slug: str) -> str | None:
    """The branch the local clone is standing on, or None.

    None for a clone that does not exist yet — there is nothing to be current
    with — and for a detached HEAD, which names no branch. Both fall back to
    the provider default, which is the best available answer when the checkout
    cannot name one.

    Never raises: this sits inside a check whose whole contract is that it
    returns a result rather than an exception.
    """
    try:
        from src.config import get_settings

        path = get_settings().repo_path(repo_slug)
        if not (path / ".git").exists():
            return None
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        name = (out.stdout or "").strip()
        if out.returncode != 0 or not name or name == "HEAD":
            return None
        return name
    except Exception as exc:  # noqa: BLE001
        logger.warning("checked_out_branch_failed repo=%s err=%s", repo_slug, exc)
        return None


def remote_head(repo_slug: str, *, workspace_id: str, user_id: str = "default") -> str:
    """The sha the repo's tracked branch points at on the remote.

    Raises with a redacted message on any failure — the caller turns that into
    an `unreachable` result rather than swallowing it, because a check that
    cannot reach the remote must not look like a check that found nothing.
    """
    from src.api.auto_review import get_auto_review_store
    from src.credentials import resolve_git_credential
    from src.sync.git_providers import build_clone_url, parse_repo_url

    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, repo_slug) or store.get(user_id, repo_slug)
    if cfg is None:
        raise RuntimeError("repository is not registered in this workspace")

    parsed = parse_repo_url(cfg.url or repo_slug)
    url = build_clone_url(parsed)
    # Fully qualified, so the remote cannot answer about a different branch:
    # a bare `main` is a glob that also matches `feature/main`.
    #
    # WHICH BRANCH, WHEN NOBODY SAID. Falling straight through to HEAD asked
    # the remote about the PROVIDER DEFAULT while the clone on disk could be
    # standing somewhere else — `RepoSync.clone_or_update` takes `branch="dev"`
    # by default and only falls back to the default branch when `dev` does not
    # exist. A repository that has a `dev` branch and was added without naming
    # one is therefore indexed from `dev` and was being compared against
    # `main`: two different shas that never converge, so it reads as behind for
    # ever, re-indexes every day, and every re-index leaves it behind again.
    #
    # The clone is the authority on what was indexed, because it is the thing
    # that was indexed. Asking it means this check and `_advance_to_remote`,
    # which resets onto the checked-out branch's remote, name the same ref by
    # construction rather than by both happening to guess the same way.
    branch = (getattr(cfg, "branch", None) or "").strip() or _checked_out_branch(repo_slug)
    ref = f"refs/heads/{branch}" if branch else "HEAD"

    env_extra = None
    creds = resolve_git_credential(cfg.provider, user_id=user_id, workspace_id=workspace_id)
    if creds is not None:
        env_extra = _basic_auth(cfg.provider, creds)
    return _run_ls_remote(url, ref, env_extra)


def _basic_auth(provider: str, creds) -> dict[str, str] | None:
    """The username/password pair git should present, or None if we have none.

    THROUGH `git_auth_kwargs`, not a lookup table of our own. The username
    slot is not cosmetic and is not one value per provider: Bitbucket wants
    `x-bitbucket-api-token-auth` for an ATATT token, `x-token-auth` for an
    ATCTT one, and the stored username for a legacy app password — three
    answers that depend on the token, not on the host. A first version of this
    function mapped github/gitlab and defaulted everything else to
    `x-access-token`, which authenticates as nobody on Bitbucket and reads as
    a bad token rather than a wrong username. It also dropped
    `creds.metadata`, where the legacy username lives.

    `git_auth_kwargs` is where those rules are already written down and
    already verified against the real providers; a second copy of them here
    is a second copy to drift.
    """
    from src.credentials.git_auth import git_auth_kwargs

    secret = getattr(creds, "secret", None)
    if not secret:
        return None
    kw = git_auth_kwargs(str(provider), secret, getattr(creds, "metadata", None) or {})
    token = kw.get("api_token")
    if token:
        # A token URL carries its own username; ask the same builder what it
        # would have used rather than guessing a second time.
        from src.sync.git_providers import GitProvider, ParsedRepo, build_authenticated_url
        try:
            probe = build_authenticated_url(
                ParsedRepo(provider=GitProvider(str(provider).lower()),
                           owner="o", name="n"), token=token)
            user = probe.split("//", 1)[1].split(":", 1)[0]
        except Exception:  # noqa: BLE001
            user = "x-access-token"
        return {"CELMIS_GIT_USER": user, "CELMIS_GIT_PW": token}
    if kw.get("username") and kw.get("password"):
        return {"CELMIS_GIT_USER": kw["username"], "CELMIS_GIT_PW": kw["password"]}
    return None


def check_repo(
    repo_slug: str,
    *,
    workspace_id: str,
    user_id: str = "default",
    reindex: bool = True,
) -> FreshnessCheck:
    """Ask the remote, record the answer, and queue a re-index if it moved.

    Records the check EVERY time, including when nothing changed — that write
    is the difference between "indexed three days ago" and "checked an hour
    ago, unchanged", and only the second answers the question.

    Never raises. A failed check is a result (`unreachable`), not an
    exception: this runs from a scheduler tick over every repository, and one
    unreachable remote must not stop the rest.
    """
    from src.repos.index_state import read_index_state, record_remote_check

    state = read_index_state(repo_slug)
    indexed = state.last_indexed_sha if state else None

    try:
        sha = remote_head(repo_slug, workspace_id=workspace_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)[:300]
        record_remote_check(repo_slug, remote_sha=None, error=detail)
        logger.info("freshness_unreachable repo=%s err=%s", repo_slug, detail)
        return FreshnessCheck(repo_slug, "unreachable", indexed_sha=indexed, detail=detail)

    record_remote_check(repo_slug, remote_sha=sha, error=None)

    if not indexed:
        # Never indexed, or indexed without recording a revision. Either way
        # there is nothing to compare against, and calling that "up to date"
        # would be a guess wearing a timestamp.
        return FreshnessCheck(repo_slug, "never_indexed", remote_sha=sha)

    if sha == indexed:
        logger.info("freshness_up_to_date repo=%s sha=%s", repo_slug, sha[:8])
        return FreshnessCheck(repo_slug, "up_to_date", remote_sha=sha, indexed_sha=indexed)

    job_id = None
    if reindex:
        try:
            from src.sync.queue import KIND_INDEX_REPO, enqueue

            # Incremental: `run_index` walks `git diff last_sha..HEAD` and
            # touches only what moved. The full path exists for the first
            # index and for a deliberate rebuild; using it here would re-parse
            # an entire repository because one file changed.
            job_id = enqueue(
                kind=KIND_INDEX_REPO,
                payload={"repo_slug": repo_slug, "since_sha": indexed,
                         "workspace_id": workspace_id},
                dedup_key=f"index_repo:{repo_slug}",
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001
            # The finding stands even if the queue would not take it. Losing
            # the whole answer because the follow-up could not be scheduled
            # would turn a database hiccup into "no new changes".
            logger.warning("freshness_enqueue_failed repo=%s err=%s", repo_slug, exc)
    logger.info("freshness_behind repo=%s indexed=%s remote=%s queued=%s",
                repo_slug, indexed[:8], sha[:8], bool(job_id))
    return FreshnessCheck(repo_slug, "behind", remote_sha=sha,
                          indexed_sha=indexed, reindex_job_id=job_id)


__all__ = ["FreshnessCheck", "check_repo", "remote_head",
           "LS_REMOTE_TIMEOUT_SECONDS"]
