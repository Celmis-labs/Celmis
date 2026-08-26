"""Per-session agent workspace — an isolated clone the agent may freely edit.

Deliberately NOT the shared clone in `repos_dir`: that one is chmod'd
read-only after every sync and `git reset --hard`-ed on every pull, so any
agent work there would be destroyed (and the agent would fight the indexer).

Every function here is synchronous git/subprocess work — callers run them via
`asyncio.to_thread` so the single uvicorn event loop never stalls.

Credential hygiene: the clone URL carries the workspace git token, so right
after cloning we reset `origin` to the clean URL. The token is re-injected
only for the one `git push` invocation (via a one-shot env askpass), which
keeps the token out of `.git/config` — the agent's Read tool can see every
file in its workspace.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Hooks are always disabled for runner-invoked git: the agent can Write into
# .git/hooks of its own workspace, and hooks would execute that on commit.
_GIT_BASE = ["git", "-c", "core.hooksPath=/dev/null"]

_AGENT_GIT_USER = ["-c", "user.name=Celmis Agent", "-c", "user.email=agent@celmis.local"]


def agent_workspaces_root() -> Path:
    from src.config import get_settings
    return get_settings().workspace_dir / "agent_workspaces"


@dataclass
class RepoCheckout:
    """One clone inside a session workspace."""

    slug: str
    path: Path
    clean_url: str       # origin URL without credentials
    push_url: str        # authenticated URL — used ONLY at push time
    default_branch: str


@dataclass
class AgentWorkspace:
    session_id: str
    repo_dir: Path       # the FIRST clone — every existing caller reads this
    home_dir: Path       # per-session HOME/CLAUDE_CONFIG_DIR (state isolation)
    clean_url: str       # first repo's origin URL without credentials
    push_url: str        # first repo's authenticated URL
    default_branch: str  # first repo's default branch
    #: The registered slug of the first repo.
    #:
    #: Needed because `repo_dir.name` is not it. A single-repo session clones
    #: into `<session>/repo`, so deriving the slug from the directory name
    #: reported EVERY single-repo push as `repo_slug: "repo"` — in the session
    #: result, in the `pushes` list the UI renders, and in the push
    #: notification. Observed on production on all three of a day's sessions.
    #: The multi-repo path never had the bug: it clones into `repos/<slug>/`
    #: and passes the slug explicitly, which is why one shape worked and the
    #: other, the common one, did not.
    repo_slug: str = ""
    #: Every clone, in pick order. A single-repo session derives it from the
    #: fields above so the push loop has exactly one shape to handle.
    repos: list[RepoCheckout] = field(default_factory=list)
    #: The agent's cwd AND the sandbox boundary: `repo_dir` for one repo, the
    #: shared parent for several. Never the session root — `home_dir` lives
    #: there, and widening the boundary to reach it would hand the agent the
    #: CLI's own state directory.
    root_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.repos:
            self.repos = [RepoCheckout(
                slug=self.repo_slug or self.repo_dir.name, path=self.repo_dir,
                clean_url=self.clean_url, push_url=self.push_url,
                default_branch=self.default_branch,
            )]
        if self.root_dir is None:
            self.root_dir = self.repo_dir


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    from src.sync.git_providers import strip_credentials
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git failed ({strip_credentials(' '.join(cmd))}): "
            f"{strip_credentials(proc.stderr.strip()[:500])}"
        )
    return proc


def prepare_workspace(
    session_id: str, repo_url: str, workspace_id: str,
    branch: str | None = None,
) -> AgentWorkspace:
    """Fresh clone under agent_workspaces/{session_id}/repo + a private HOME.

    `branch` — the repo's configured branch (None → provider default). A
    branch that no longer exists must not kill the session, so a failed
    targeted clone falls back to the default branch.

    Blocking (5-60s) — call via asyncio.to_thread.
    """
    from src.credentials import resolve_git_credential
    from src.sync.git_providers import build_authenticated_url, parse_repo_url

    parsed = parse_repo_url(repo_url)
    creds = resolve_git_credential(
        parsed.provider.value, workspace_id=workspace_id,
    )
    if creds is None:
        push_url = repo_url
    else:
        # Auth shape depends on the token type — see credentials/git_auth.py.
        from src.credentials.git_auth import describe_auth, git_auth_kwargs
        kw = git_auth_kwargs(parsed.provider.value, creds.secret, creds.metadata)
        # Log the CHOSEN shape (never the secret): when a clone 403s, this is
        # the difference between "wrong auth style" and "token lacks access".
        logger.info("agent_clone_auth session=%s repo=%s auth=%s meta_keys=%s",
                    session_id, parsed.slug,
                    describe_auth(parsed.provider.value, creds.secret, creds.metadata),
                    sorted((creds.metadata or {}).keys()))
        push_url = (
            build_authenticated_url(parsed, username=kw["username"],
                                    password=kw["password"])
            if "username" in kw
            else build_authenticated_url(parsed, token=kw["api_token"])
        )
    clean_url = repo_url

    root = agent_workspaces_root() / session_id
    repo_dir = root / "repo"
    home_dir = root / "home"
    home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    # depth 50 keeps clones fast; full blobs (no blob:none) so the agent's
    # file reads never trigger lazy network fetches mid-session.
    base_clone = _GIT_BASE + ["clone", "--depth", "50"]
    wanted = (branch or "").strip()
    try:
        _run(
            base_clone + (["--branch", wanted] if wanted else [])
            + [push_url, str(repo_dir)],
            cwd=root, timeout=600,
        )
    except RuntimeError:
        if not wanted:
            raise
        # Configured branch is gone (renamed/deleted) — don't fail the whole
        # session over it; clone the default branch and say so in the log.
        logger.warning(
            "agent_clone_branch_missing session=%s repo=%s branch=%s "
            "— falling back to the default branch",
            session_id, parsed.slug, wanted,
        )
        shutil.rmtree(repo_dir, ignore_errors=True)
        _run(base_clone + [push_url, str(repo_dir)], cwd=root, timeout=600)
    # Immediately scrub the credentialed URL out of .git/config.
    _run(_GIT_BASE + ["remote", "set-url", "origin", clean_url], cwd=repo_dir)

    head = _run(_GIT_BASE + ["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    default_branch = head.stdout.strip() or "main"

    branch = f"celmis-agent/{session_id[:8]}"
    _run(_GIT_BASE + ["checkout", "-b", branch], cwd=repo_dir)

    logger.info("agent_workspace_ready session=%s repo=%s branch=%s",
                session_id, parsed.slug, branch)
    return AgentWorkspace(
        session_id=session_id, repo_dir=repo_dir, home_dir=home_dir,
        clean_url=clean_url, push_url=push_url, default_branch=default_branch,
        repo_slug=parsed.slug,
    )


#: How much of the agent's own summary goes in the commit body. Long enough
#: for a real explanation, short enough that `git log --oneline` and a PR page
#: stay readable.
MAX_SUMMARY_CHARS = 3000


#: A git subject line is scanned in a list, not read. 72 is the width every
#: tool assumes.
SUBJECT_MAX_CHARS = 72


def _clip_subject(text: str) -> str:
    """72 characters, cut at a word boundary.

    `text[:72]` cut mid-word, so `git log --oneline` showed subjects ending
    "…distribute the remaind". A subject that stops in the middle of a word
    reads as a truncated file, not as a summary.
    """
    text = " ".join(text.split())
    if len(text) <= SUBJECT_MAX_CHARS:
        return text
    cut = text[:SUBJECT_MAX_CHARS - 1]
    space = cut.rfind(" ")
    if space >= SUBJECT_MAX_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def _subject_line(title: str, summary: str, session_id: str) -> str:
    """What `git log --oneline` shows for this commit.

    THE TITLE FIRST, and that ordering is the fix. The subject was taken from
    the agent's own first sentence, and an agent's first sentence is written
    to a person in a chat window, not to a git log. Two real ones, from two
    sessions on the same day:

        All 3 tests pass (`3 passed in 0.01s`).
        That failure is pre-existing and unrelated to my change — it's in
        `src/settlement.py`'s `split_settlement`, a file I did not touch.

    Neither says what the commit does. Both are perfectly good things to say
    at the end of a conversation. The session's TITLE — "Fix split_settlement
    remainder loss", "Fix outdated dependencies (gateway)" — is what someone
    wrote to describe the task, which is exactly what a subject is for.

    The agent's sentence stays as the fallback for a session with no title,
    and it is still better than what came before it, which was
    `Celmis agent session 8ca7b349` — a line saying an agent was here and
    nothing else.
    """
    if (title or "").strip():
        return _clip_subject(title.strip().lstrip("#").strip())

    # No title. Walk the summary for the first line that says something —
    # skipping a bare heading, because agents open with `## Summary` and
    # "Summary" is not a subject. A heading WITH content ("## Fix the retry
    # backoff") is one, so the test is the number of words, not the hashes.
    for line in (summary or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text and len(text.split()) > 1:
            return _clip_subject(text)
    return f"Celmis agent session {session_id[:8]}"


def _commit_message(ws: AgentWorkspace, repo, *, summary: str,
                    prompt: str, verifications: list[dict],
                    requested_by: str = "", title: str = "") -> str:
    """The record of what was changed and what proves it.

    This used to be `Celmis agent session 8ca7b349` — a line that says an
    agent was here and nothing else. Whoever finds the commit six weeks later,
    reviewing a branch that touched production, gets no answer to either
    question that matters: what was this for, and was it checked.

    So the message carries four things, in the order a reader needs them:

      1. a subject taken from the agent's own first sentence, so `git log
         --oneline` is readable;
      2. WHAT WAS ASKED — the prompt that started the session, because the
         change only makes sense against the request;
      3. WHAT WAS DONE — the agent's summary, plus `git diff --stat`, which
         is the fact-checked half: prose can drift from the diff, the stat
         cannot;
      4. WHAT WAS RUN — every sandbox command with its exit code, failures
         included. Listing only the green ones would read as proof while
         hiding the attempt that did not work.
    """
    stat = _run(_GIT_BASE + ["diff", "--cached", "--stat"], cwd=repo.path).stdout.strip()
    if not stat:
        stat = _run(_GIT_BASE + ["diff", "--stat", "HEAD"], cwd=repo.path).stdout.strip()

    summary = (summary or "").strip()
    subject = _subject_line(title, summary, ws.session_id)

    parts = [subject, ""]

    if prompt:
        parts += ["What was asked", "", _indent(prompt.strip()[:1000]), ""]

    if summary:
        parts += ["What the agent reports", "", summary[:MAX_SUMMARY_CHARS].strip(), ""]

    if stat:
        parts += ["What actually changed", "", _indent(stat), ""]

    if verifications:
        parts += ["What was run to check it", ""]
        # A one-line verdict BEFORE the log, because the log misleads a
        # skimmer and the log is the most valuable artefact this feature
        # produces.
        #
        # Measured on a real session. The agent ran three commands: the exec
        # sandbox unpacks the repo at a different root than the file tools
        # use, so `cd /workspace/…/repo` failed; `python -m pytest` then failed
        # because the image has no pytest; `pip install pytest -q && python -m
        # pytest tests/ -q` passed with "3 passed in 0.01s". Two probes and a
        # green run. Rendered as a bare list that reads
        #
        #     [FAIL] … pytest
        #     [FAIL] … pytest
        #     [ok  ] … pytest
        #
        # a reviewer opening the pull request concludes the tests are flaky
        # and stops trusting the change. Nothing is hidden — hiding a failed
        # command would be the opposite mistake, and this product's whole
        # argument is that it reports what it did not check. The failures stay
        # on the page; they just stop being the first thing read.
        passed = [v for v in verifications if v.get("ok")]
        if passed and len(passed) < len(verifications):
            last_ok = verifications[-1].get("ok")
            parts.append(
                f"  RESULT: {len(passed)} of {len(verifications)} commands "
                "succeeded"
                + (" — the final one passed; the earlier failures are attempts "
                   "on the way to it (a missing tool, a wrong path), not "
                   "failing checks."
                   if last_ok else
                   " — the final command did NOT pass. Read the branch before "
                   "trusting it.")
            )
            parts.append("")
        for v in verifications:
            code = v.get("exit_code")
            mark = "ok  " if v.get("ok") else "FAIL"
            secs = v.get("elapsed")
            parts.append(
                f"  [{mark}] exit={code} {secs}s  {v.get('command', '')[:160]}")
        if not passed:
            parts.append("")
            parts.append("  NOTHING PASSED. Read the branch before trusting it.")
        parts.append("")
    else:
        # Stated rather than left blank: "no tests were run" and "the tests
        # section is missing" look identical otherwise.
        parts += ["What was run to check it", "",
                  "  Nothing — no command was executed in this session.", ""]

    if requested_by:
        # Two trailers, doing two different jobs.
        #
        # `Requested-by` is the audit answer: from `git log` alone, without
        # any Celmis access, a reader can see which person authorised this.
        # `Co-authored-by` is the GitHub one — it renders an avatar on the
        # commit and makes it searchable by that account, which is what makes
        # the change attributable in the place people actually look.
        #
        # The AUTHOR stays "Celmis Agent". The agent wrote the code and
        # claiming otherwise would put a human's name on lines they never
        # read.
        parts += [f"Requested-by: {requested_by}"]
    parts += [f"Celmis session: {ws.session_id}"]
    if requested_by:
        parts += ["", f"Co-authored-by: {requested_by}"]
    return "\n".join(parts).rstrip() + "\n"


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def staging_root(session_id: str) -> Path:
    """Where attachments live before the clone exists.

    The paperclip used to appear only inside a running session, which is the
    wrong moment: the thing people want to attach is the production log that
    made them open the session in the first place. But there is no workspace to
    write into until the runner has cloned, and the runner does not clone until
    the session starts.

    So files land here first, under the session id, and `adopt_staged` moves
    them in as soon as the clone exists. Deliberately a sibling of
    `agent_workspaces/` rather than inside it: `sweep_stale_workspaces()`
    deletes everything under that directory at startup, which would throw away
    a queued session's attachments before it ever ran.
    """
    from src.config import get_settings
    return get_settings().workspace_dir / "agent_staging" / session_id


def adopt_staged(session_id: str, ws: AgentWorkspace) -> list[str]:
    """Move anything staged before the session started into its workspace.

    Returns the relative paths, so the runner can tell the agent what it has.
    Best effort: an attachment that fails to move is worth a log line, not a
    dead session — the prompt usually still makes sense without it.
    """
    src = staging_root(session_id)
    if not src.exists():
        return []
    dest = (ws.root_dir or ws.repo_dir) / "_attachments"
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for item in sorted(src.iterdir()):
        if not item.is_file():
            continue
        try:
            target = dest / item.name
            item.replace(target)
            moved.append(f"_attachments/{item.name}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("staged_attachment_move_failed session=%s file=%s err=%s",
                           session_id, item.name, exc)
    with contextlib.suppress(OSError):
        src.rmdir()
    if moved:
        logger.info("staged_attachments_adopted session=%s count=%d",
                    session_id, len(moved))
    return moved


def commit_and_push(ws: AgentWorkspace, *, summary: str = "",
                    prompt: str = "", requested_by: str = "",
                    title: str = "",
                    verifications: list[dict] | None = None) -> dict | None:
    """Commit any agent edits and push the session branch.

    Returns the first repo's {repo_slug, branch, compare_url, commit} plus a
    `pushes` list covering every repo that changed, or None when none did.
    Blocking — call via asyncio.to_thread. Invoked after any CONTROLLED stop,
    including one that ended on an error result such as the turn limit: a
    half-finished branch the user can read beats edits silently deleted with
    the workspace. A crashed run never reaches here.
    """
    branch = f"celmis-agent/{ws.session_id[:8]}"
    pushed: list[dict] = []

    for repo in ws.repos:
        status = _run(_GIT_BASE + ["status", "--porcelain"], cwd=repo.path)
        if not status.stdout.strip():
            # Untouched repo. A session that reads three and edits one must
            # not leave two empty branches behind for a human to clean up.
            continue
        _run(_GIT_BASE + ["add", "-A"], cwd=repo.path)
        message = _commit_message(
            ws, repo, summary=summary, prompt=prompt,
            requested_by=requested_by, title=title,
            verifications=verifications or [])
        _run(_GIT_BASE + _AGENT_GIT_USER + ["commit", "-m", message],
             cwd=repo.path)
        # Re-inject credentials only for this single push invocation.
        _run(_GIT_BASE + ["push", repo.push_url, f"HEAD:refs/heads/{branch}"],
             cwd=repo.path, timeout=300)
        sha = _run(_GIT_BASE + ["rev-parse", "HEAD"], cwd=repo.path).stdout.strip()
        pushed.append({
            "repo_slug": repo.slug,
            "branch": branch,
            "compare_url": _compare_url(repo.clean_url, repo.default_branch, branch),
            "commit": sha,
        })
        logger.info("agent_branch_pushed session=%s repo=%s branch=%s",
                    ws.session_id, repo.slug, branch)

    if not pushed:
        return None
    # Flat keys for the first repo keep every existing reader working — the
    # session card, the PR opener and the push notification all read
    # result["branch"]. `pushes` carries the rest.
    first = pushed[0]
    return {**first, "pushes": pushed}


def _compare_url(clean_url: str, base: str, branch: str) -> str | None:
    """Best-effort web URL to open a PR/MR from the pushed branch."""
    u = clean_url.removesuffix(".git")
    if "github.com" in u:
        return f"{u}/compare/{base}...{branch}?expand=1"
    if "gitlab" in u:
        return f"{u}/-/merge_requests/new?merge_request%5Bsource_branch%5D={branch}"
    if "bitbucket.org" in u:
        return f"{u}/pull-requests/new?source={branch}"
    return None


def cleanup_workspace(session_id: str) -> None:
    """Remove the session's clone + HOME. Best-effort."""
    root = agent_workspaces_root() / session_id
    try:
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_workspace_cleanup_failed session=%s err=%s", session_id, exc)


def sweep_stale_workspaces() -> int:
    """Startup sweep — remove leftover workspaces (crashed runs). Returns count."""
    root = agent_workspaces_root()
    if not root.exists():
        return 0
    n = 0
    for child in root.iterdir():
        try:
            shutil.rmtree(child, ignore_errors=True)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def prepare_multi_workspace(
    session_id: str, specs: list[tuple[str, str, str | None]], workspace_id: str,
) -> AgentWorkspace:
    """Clone several repos as siblings under `<session>/repos/<slug>/`.

    `specs` is [(slug, url, branch)] in pick order. The agent's cwd — and the
    sandbox boundary — becomes the shared parent, so it can read across the
    set, which is the whole reason the feature exists.

    A single spec goes through `prepare_workspace` untouched: that layout is
    what every existing session, path and log line refers to, and "we also
    support N" is no reason to move everyone's floor.
    """
    if not specs:
        raise ValueError("prepare_multi_workspace needs at least one repo")
    if len(specs) == 1:
        slug, url, branch = specs[0]
        return prepare_workspace(session_id, url, workspace_id, branch)

    root = agent_workspaces_root() / session_id
    repos_dir = root / "repos"
    home_dir = root / "home"
    home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    repos_dir.mkdir(parents=True, exist_ok=True)

    checkouts: list[RepoCheckout] = []
    for slug, url, branch in specs:
        # Reuse the single-repo clone for its credential resolution, branch
        # fallback and origin scrubbing, then move the result into place —
        # duplicating that logic is how one of the two copies drifts.
        staged = prepare_workspace(f"{session_id}/staging-{_slugify(slug)}",
                                   url, workspace_id, branch)
        target = repos_dir / _slugify(slug)
        shutil.move(str(staged.repo_dir), str(target))
        shutil.rmtree(staged.home_dir.parent, ignore_errors=True)
        checkouts.append(RepoCheckout(
            slug=slug, path=target, clean_url=staged.clean_url,
            push_url=staged.push_url, default_branch=staged.default_branch,
        ))

    first = checkouts[0]
    return AgentWorkspace(
        session_id=session_id, repo_dir=first.path, home_dir=home_dir,
        clean_url=first.clean_url, push_url=first.push_url,
        default_branch=first.default_branch, repo_slug=first.slug,
        repos=checkouts, root_dir=repos_dir,
    )


def _slugify(slug: str) -> str:
    """Directory name for a repo slug — a slug can carry a slash."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-") or "repo"


__all__ = [
    "AgentWorkspace",
    "RepoCheckout",
    "agent_workspaces_root",
    "prepare_multi_workspace",
    "prepare_workspace",
    "commit_and_push",
    "cleanup_workspace",
    "sweep_stale_workspaces",
]
