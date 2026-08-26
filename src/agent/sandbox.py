"""Run a command against a checkout, somewhere that holds no secrets.

The api container holds `/workspace/data/secrets/.master.key` and
`credentials.db` — the Fernet key and every tenant's git tokens, in one
directory, owned by the same user the agent runs as. Executing a repository's
own build or test command there needs no exploit to be a cross-tenant
credential compromise: `cat` is enough. So it does not happen there.

This module is the api-side half. It packs a directory into a tarball, posts
it to the sandbox service with a command, and gets back an exit code and
clipped output. Nothing else crosses:

  * no credential travels — `.git` is excluded from the tarball, and the clone
    and the push are both done by the api, before and after;
  * the sandbox initiates nothing — this is the only direction;
  * the reply is data, never something to evaluate. Output is clipped by the
    sandbox and clipped again here.

`available()` is deliberately cheap and never raises. The sandbox is optional:
a deployment without it keeps working exactly as before, with execution simply
unavailable rather than the feature half-broken.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://sandbox:8900")
SANDBOX_TOKEN = os.environ.get("SANDBOX_TOKEN", "")

#: How long, beyond the command's own budget, this caller will hold the
#: connection open — the sandbox's queue, the tar upload and the reply.
#:
#: It is also SENT, so the sandbox can bound its queue wait by it rather than
#: by its own `SANDBOX_SLOT_WAIT` alone. Two processes with two independent
#: numbers is how the old pairing (`timeout + 60` here, 120 there) came to
#: guarantee a hang-up on any job that queued for a minute; one number,
#: declared by the side that does the hanging up, cannot drift.
#:
#: 180 rather than 60 because the sandbox's own ceiling is 120 and the wire
#: work needs room after it. Raising it makes the caller more patient, never
#: the sandbox slower.
WAIT_BUDGET_SECONDS = int(os.environ.get("SANDBOX_CLIENT_WAIT_BUDGET", 180))


def _sandbox_host() -> tuple[str, ...]:
    """The one extra host the two calls below may reach.

    SANDBOX_URL is deploy-time infrastructure — compose sets it right next to
    the token — so naming its host follows src/http.py's rule: the allowlist
    exception comes from the same configuration the request is built from,
    never from anything a user typed. A compose-internal name like `sandbox`
    is exactly what the shipped public allowlist will never carry.
    """
    from urllib.parse import urlsplit

    host = urlsplit(SANDBOX_URL).hostname or ""
    return (host,) if host else ()

#: Bigger than this and it is not a source checkout any more. The sandbox
#: enforces its own ceiling too; this one keeps a doomed upload off the wire.
MAX_TAR_BYTES = 150 * 1024 * 1024

#: What never goes in the tarball. `.git` is the important one — it carries
#: the remote URL, and a credentialed URL would carry the token with it.
_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", "dist", "build", ".tox", ".ruff_cache",
    "target", ".gradle", ".terraform",
}


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    clipped: bool = False

    @property
    def succeeded(self) -> bool:
        return self.ok and self.exit_code == 0

    def as_text(self, limit: int = 8000) -> str:
        """One block for a model or a log line. Stderr last — it is what a
        failure explains itself with."""
        if not self.ok:
            return f"[sandbox] {self.error or 'failed to run'}"
        parts = [f"[exit {self.exit_code}]"]
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append("--- stderr ---\n" + self.stderr.strip())
        if self.clipped:
            parts.append("[output was clipped]")
        text = "\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n… [clipped]"


def available() -> bool:
    """Is a sandbox configured and answering? Never raises."""
    if not SANDBOX_TOKEN:
        return False
    try:
        from src.http import build_client
        with build_client(timeout=3.0, extra_allowed_hosts=_sandbox_host()) as client:
            r = client.get(f"{SANDBOX_URL}/health")
        return r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.debug("sandbox_unavailable err=%s", exc)
        return False


def _pack(directory: Path) -> bytes:
    """Tar a checkout, without the directories nobody needs and the one
    directory that must not travel."""
    buf = io.BytesIO()
    root = directory.resolve()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(part in _EXCLUDE_DIRS for part in parts):
            return None
        # A symlink can point outside the tree; the sandbox refuses them on
        # unpack, so drop them here rather than send a payload it will reject.
        if info.issym() or info.islnk():
            return None
        # Ownership is meaningless in the sandbox and leaks the host's uids.
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(root), arcname=".", filter=_filter)
    data = buf.getvalue()
    if len(data) > MAX_TAR_BYTES:
        raise ValueError(
            f"checkout is {len(data) // 1024 // 1024} MB packed, over the "
            f"{MAX_TAR_BYTES // 1024 // 1024} MB limit"
        )
    return data


def run(directory: Path, command: str, *, timeout: int = 300,
        branch: str = "work") -> SandboxResult:
    """Run `command` against a copy of `directory`. Never raises.

    The copy is one-way: whatever the command writes stays in the sandbox and
    is discarded. This answers "does it pass", not "apply this change" — edits
    are made by the agent through its own tools, in the api's workspace, where
    they can be reviewed and pushed.
    """
    if not SANDBOX_TOKEN:
        return SandboxResult(False, None, error="no sandbox configured")
    try:
        payload = _pack(directory)
    except Exception as exc:  # noqa: BLE001
        return SandboxResult(False, None, error=str(exc)[:300])

    # `branch` names the branch the sandbox's throwaway git repo is created on,
    # so `git rev-parse --abbrev-ref HEAD` in there answers with the real one
    # rather than whatever `git init` would have defaulted to.
    header = json.dumps({"command": command, "timeout": timeout,
                         "branch": branch,
                         "wait_budget": WAIT_BUDGET_SECONDS}).encode()
    body = header + b"\n" + payload
    try:
        import httpx

        from src.http import build_client

        # The read timeout has to outlast the QUEUE AND the command, in that
        # order — the sandbox waits for a free slot and only then runs
        # anything, so its two clocks add up. `timeout + 60` covered the
        # command and about a minute of queueing; past that the caller hung up
        # on a job the sandbox went on running to completion, which is work
        # paid for whose result reached nobody. It only ever happened under
        # load, which is precisely when the queue is not empty.
        #
        # `wait_budget` is the same number, told to the server, so it can
        # bound its own queue wait by our patience and answer "busy, try
        # again" — something the caller can read — instead of being cut off.
        with build_client(
            timeout=httpx.Timeout(connect=5.0, read=timeout + WAIT_BUDGET_SECONDS,
                                  write=120.0, pool=5.0),
            extra_allowed_hosts=_sandbox_host(),
        ) as client:
            r = client.post(
                f"{SANDBOX_URL}/run", content=body,
                headers={"X-Sandbox-Token": SANDBOX_TOKEN,
                         "Content-Type": "application/octet-stream"},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sandbox_call_failed err=%s", exc)
        return SandboxResult(False, None, error=f"sandbox unreachable: {exc}"[:300])

    if r.status_code != 200:
        return SandboxResult(False, None,
                             error=f"sandbox returned {r.status_code}")
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return SandboxResult(False, None, error="sandbox returned no JSON")

    return SandboxResult(
        ok=bool(data.get("ok")),
        exit_code=data.get("exit_code"),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        error=str(data.get("error") or ""),
        clipped=bool(data.get("clipped")),
    )


__all__ = ["SandboxResult", "available", "run"]
