"""The sandbox: runs a command against a tarball, returns exit code and output.

This process exists so that "test it" can mean running a tenant's own test
suite without that execution happening next to everybody else's secrets.

What it deliberately does NOT have, and why each one matters:

  * No `workspace_data` volume. The api container holds
    `/workspace/data/secrets/.master.key` and `credentials.db` — the Fernet key
    and every tenant's git tokens, in one directory, owned by the same user the
    agent runs as. Running `bash` there needs no exploit: `cat` is enough.
  * No secret environment. It is handed a tarball and a command, nothing else.
  * Its own docker network. `postgres`, `litellm` and `qdrant` are not
    resolvable from here, and the host's published ports are bound to
    127.0.0.1 so the bridge gateway is not a way round that either.

What it DOES have, on purpose: outbound internet, because `pip install` and
`npm ci` need it and a sandbox that cannot install dependencies cannot run a
real repository's tests. The blast radius of that is one tenant's own source —
which they gave us and already own — never another tenant's anything.

Residuals, stated rather than hidden:

  * api and the sandbox share a network so api can reach it, which means the
    reverse is reachable at the IP level too. The api's endpoints all require a
    signed token this process never receives.
  * /proc is shared, so one job can read another's COMMAND LINE — verified, not
    assumed: a job running as sbx3 sees `sbx2 :: sleep 10`. It cannot read the
    other job's files, environment or output; /proc/<pid>/environ and the work
    tree are both owner-only, and that was checked the same way. Closing this
    needs hidepid=2 or a per-job PID namespace, and both need CAP_SYS_ADMIN —
    a far larger grant than the leak is worth. What crosses is the text of a
    command the tenant's own repository configured.
  * ONE NETWORK NAMESPACE for every job. A job that binds 127.0.0.1:5432 is
    reachable by its neighbours, and so is an abstract unix socket. Nothing is
    read that way, but tenant A can answer a connection tenant B's test suite
    meant for its own fixture, and change whether that suite passes. A per-job
    netns needs CAP_SYS_ADMIN, the same grant refused above; splitting jobs
    across replica containers is the honest fix and is a scheduling change, not
    a flag. Recorded so the next person does not discover it in production.
  * ONE RESOURCE POOL. mem_limit, pids_limit and the /work tmpfs are
    container-wide, so a job that eats all of one degrades its neighbours.
    SLOTS is what bounds this, and it is a capacity choice, deliberately made:
    per-job cgroups would need CAP_SYS_ADMIN as well.
  * NO TENANT IDENTITY on the wire, so the slot pool cannot be fair — one
    workspace can hold every slot. The api is the only caller and knows the
    workspace, so this is a contract change rather than a limitation, and it is
    the next thing to do here.

No third-party dependency on purpose. `http.server` is unglamorous and its
attack surface is a known quantity; a web framework here would be one more
thing to keep patched inside the box that runs untrusted code.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sandbox")

#: Everything runs under here — a tmpfs in the compose service, so the work
#: tree never touches the image's read-only root and vanishes on restart.
WORK_ROOT = Path(os.environ.get("SANDBOX_WORK_ROOT", "/work"))

#: Hard ceilings. The compose service also sets mem_limit/cpus/pids_limit;
#: these are the ones this process can enforce itself.
#:
#: This one is per REQUEST and the requests are concurrent, so what the box
#: actually has to hold is SLOTS × this, plus every upload still arriving for a
#: slot that is not free yet. It was 200MB while one job ran at a time; four
#: uploads of that size is most of the container's memory before a single job
#: has started. `main()` says so out loud at startup rather than leaving the
#: arithmetic to whoever raises SANDBOX_SLOTS later.
MAX_UPLOAD_BYTES = int(os.environ.get("SANDBOX_MAX_UPLOAD", 100 * 1024 * 1024))
MAX_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_MAX_TIMEOUT", 900))
#: Output past this is dropped with a marker. A command that prints a gigabyte
#: must not be able to exhaust the caller through the reply.
MAX_OUTPUT_BYTES = int(os.environ.get("SANDBOX_MAX_OUTPUT", 256 * 1024))
#: Ceiling on the UNPACKED archive. The caller caps the compressed upload, and
#: a few hundred megabytes of zeros is a few gigabytes on the other side — into
#: a RAM-backed tmpfs shared by every concurrent job.
MAX_EXTRACTED_BYTES = int(os.environ.get("SANDBOX_MAX_EXTRACTED", 2 * 1024 * 1024 * 1024))
#: How often quarantined uids are retried. Long, because reaching quarantine at
#: all means something refused SIGKILL, which does not resolve in milliseconds.
REAP_INTERVAL_SECONDS = int(os.environ.get("SANDBOX_REAP_INTERVAL", 30))

# ─── Job weight: what a command is going to cost ─────────────────────
#
# The two shapes of job are genuinely different animals. "Does it pass" reads
# files and runs a test binary. "Build it" downloads a dependency tree,
# compiles, and peaks at a gigabyte or more — `npm ci` on a real front end is
# the usual offender.
#
# We cannot give them different MEMORY limits: mem_limit is container-wide and
# a per-job cgroup needs CAP_SYS_ADMIN, which this container deliberately does
# not have. What we CAN do is let a heavy job occupy more than one slot. With
# four slots that allows four checks, or two builds, or one build beside two
# checks — the ceiling on concurrent heavy work without any new privilege.
#
# And it is decided from the command, not from a flag the caller has to
# understand. A person asking for their tests to run should not first have to
# know what `npm ci` costs.
_INSTALL_MARKERS = (
    "npm ci", "npm i", "npm install", "yarn install", "yarn --",
    "pnpm i", "pnpm install", "bun install",
    "pip install", "pip3 install", "poetry install", "uv sync", "uv pip",
    "bundle install", "composer install", "go mod download", "go install",
    "cargo build", "cargo install", "gradle", "mvn ", "make ",
    "apt-get install", "apk add", "docker build",
)

#: Slots a heavy job takes. Two, not four: a build must be able to run beside
#: something small, or a single long `npm ci` blocks every quick check for its
#: whole duration, which is the failure this weighting exists to avoid.
BUILD_WEIGHT = int(os.environ.get("SANDBOX_BUILD_WEIGHT", 2))
#: …and longer to finish, because it is downloading the internet.
BUILD_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_BUILD_TIMEOUT", 900))
CHECK_TIMEOUT_SECONDS = int(os.environ.get("SANDBOX_CHECK_TIMEOUT", 300))


def classify(command: str) -> str:
    """`build` if the command installs or compiles, else `check`.

    Substring matching on purpose. A command arrives as a shell string that may
    chain several things — `pip install -r requirements.txt && pytest` is one
    job and it is a build. Anything that merely looks like a build costs one
    extra slot and gets a longer clock; being wrong in that direction is cheap,
    being wrong the other way is an OOM that takes the neighbours down.
    """
    lowered = command.lower()
    return "build" if any(m in lowered for m in _INSTALL_MARKERS) else "check"


#: A shared secret, so a stray process on the same network cannot drive this.
#: Not a security boundary on its own — the isolation is the absence of
#: anything worth reaching — but it keeps the surface honest.
TOKEN = os.environ.get("SANDBOX_TOKEN", "")

# ─── Per-job identities ──────────────────────────────────────────────
#
# Jobs run CONCURRENTLY and belong to DIFFERENT TENANTS. Running them all as
# one user, as this did at first, means tenant A's test suite can read tenant
# B's checkout out of /work while both are live — the same cross-tenant read
# the sandbox exists to prevent, moved one level down.
#
# A queue (one job at a time) would also close it, and is wrong here: this has
# to hold up under parallel load. So each job leases its own uid instead, and
# the isolation is the kernel's, not a lock's.
#
# The image pre-creates SANDBOX_UID_BASE … +SANDBOX_UID_POOL. Slots can be
# raised in .env up to that ceiling without rebuilding the image; past it, the
# image has to grow first.
UID_BASE = int(os.environ.get("SANDBOX_UID_BASE", 20000))
UID_POOL = int(os.environ.get("SANDBOX_UID_POOL", 64))
#: How many jobs may run at once. Four by default because the box has four
#: cores and ~5GB free, and `npm ci` is not a cheap neighbour. This is the
#: number that costs memory; the pool above only costs entries in /etc/passwd.
SLOTS = max(1, min(int(os.environ.get("SANDBOX_SLOTS", 4)), UID_POOL))
#: The CEILING on how long a caller waits for a free slot. The real bound is
#: whichever is smaller, this or what the caller says it will wait for.
#:
#: "Shorter than the client's read timeout" is what the old comment here
#: claimed, and it compared two numbers that are not alternatives but
#: SEQUENTIAL: the server queues for a slot AND THEN runs the command, so it
#: can hold the connection for `SLOT_WAIT_SECONDS + timeout`, while the client
#: allowed `timeout + 60`. Any queue wait past a minute and the caller hung up
#: on a job this process then went on running to completion — work paid for
#: whose result reached nobody, and only under load, which is exactly when the
#: queue is not empty.
#:
#: So the caller now declares its patience (`wait_budget`) and this bounds
#: itself by it. The party that will hang up is the one that gets to decide,
#: and a caller with no room left is told "busy, try again" — an answer it can
#: read — instead of being cut off mid-run.
SLOT_WAIT_SECONDS = int(os.environ.get("SANDBOX_SLOT_WAIT", 120))

#: Of the caller's declared patience, what is NOT available for queueing:
#: reading the tar off the wire, writing the checkout, and sending the reply
#: all happen inside the same read timeout the caller is counting down.
WAIT_BUDGET_RESERVE_SECONDS = 30

#: Leased uids. A plain queue IS the allocator — `get` blocks while everything
#: is busy, which is the backpressure, and `put` in a finally is the release.
_uids: queue.Queue[int] = queue.Queue()

#: uids that would not come clean. Held back rather than reissued — a uid with
#: somebody's surviving process on it is worse than a missing slot — and
#: returned by `_reaper` the moment they empty.
_quarantine: set[int] = set()
_quarantine_lock = threading.Lock()


def _clip(text: str) -> tuple[str, bool]:
    data = text.encode("utf-8", "replace")
    if len(data) <= MAX_OUTPUT_BYTES:
        return text, False
    head = data[: MAX_OUTPUT_BYTES // 2].decode("utf-8", "replace")
    tail = data[-MAX_OUTPUT_BYTES // 2:].decode("utf-8", "replace")
    return f"{head}\n… [{len(data) - MAX_OUTPUT_BYTES} bytes dropped] …\n{tail}", True


def _drain(stream, limit: int, into: list) -> None:
    """Read a pipe to EOF, keeping at most `limit` bytes.

    Reading to the END is the part that matters, and it is why this cannot
    simply stop at the limit: a pipe nobody drains fills, and the child blocks
    in `write` forever — which turns "a job printed too much" into "a job hung
    until its timeout", holding a slot the whole time.

    Keeping only `limit` is the other half. `capture_output=True` accumulates
    everything in this process's memory before anything gets clipped, so one
    tenant printing a few gigabytes OOM-kills the container and takes the other
    three tenants' jobs down with it.
    """
    kept = bytearray()
    dropped = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = limit - len(kept)
            if room > 0:
                kept += chunk[:room]
                dropped += max(0, len(chunk) - room)
            else:
                dropped += len(chunk)
    except (OSError, ValueError):
        # The pipe was closed under us — the job was killed. Whatever was read
        # up to that point is still worth reporting.
        pass
    into.append((bytes(kept), dropped))


def _safe_extract(blob: bytes, dest: Path) -> None:
    """Unpack a tarball, refusing anything that points outside `dest`.

    Python's `extractall` happily writes through `../` and absolute paths on
    older versions, and this input arrives from a caller we treat as
    semi-trusted at best.

    The directory is created 0700 BEFORE anything is written into it. Creating
    it at the default 0755 and tightening afterwards leaves the whole
    extraction window — the slowest part of a job's setup — with the tree
    world-readable, defended only by an unguessable name.
    """
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is masked by the umask, so say it again plainly.
    dest.chmod(0o700)
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as tar:
        total = 0
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                # A link can point anywhere; the payload is a source checkout
                # and does not need them.
                raise ValueError(f"link in archive: {member.name}")
            target = (root / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"path escapes the work dir: {member.name}")
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                # The caller caps the COMPRESSED upload; a few hundred
                # megabytes of zeros is a few gigabytes unpacked, and /work is
                # a RAM-backed tmpfs shared by every concurrent job.
                raise ValueError(
                    f"archive unpacks to over "
                    f"{MAX_EXTRACTED_BYTES // 1024 // 1024} MB"
                )
        # filter="data" is the whole reason to name a filter here: extraction
        # runs as root, and the default would take mode, uid and gid straight
        # from bytes the archive chose — including setuid bits.
        tar.extractall(root, filter="data")  # noqa: S202 — checked above


def _chown_tree(root: Path, uid: int) -> None:
    """Hand a freshly unpacked checkout to the job's user.

    `os.walk` with `followlinks=False` (the default) and `os.lchown` so a
    symlink is never followed out of the tree — `_safe_extract` already
    refuses links, and this stays correct if that ever softens.
    """
    os.lchown(root, uid, uid)
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            try:
                os.lchown(os.path.join(base, name), uid, uid)
            except OSError:
                continue


def _pids_of(uid: int) -> list[int]:
    """Every live pid whose EFFECTIVE uid is `uid`.

    Read from /proc rather than shelling out to pkill: this must work whether
    or not procps is in the image, and it must not itself become a command
    injection surface.
    """
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status", "rb") as fh:
                for line in fh:
                    if line.startswith(b"Uid:"):
                        # real, effective, saved, fs — effective is the one
                        # that decides what the process can read.
                        if int(line.split()[2]) == uid:
                            pids.append(int(entry))
                        break
        except (OSError, ValueError, IndexError):
            # It exited while we were reading, or /proc raced us.
            continue
    return pids


def _signal_pid(pid: int, uid: int, sig: int) -> bool:
    """Signal `pid`, but only while it still belongs to `uid`.

    Two hazards, both from the same place — the pid was learned by reading
    /proc a moment ago, and a pid is a number the kernel reuses:

      * this process is root and CAP_KILL, so an unchecked `os.kill` on a
        recycled pid would signal whatever now holds that number — including
        another tenant's running job;
      * `pidfd_open` narrows the window to the instant between the check and
        the open, because the descriptor names the process rather than the
        number, so nothing that happens afterwards can redirect the signal.

    The uid is re-read immediately before signalling. That leaves a window of
    microseconds rather than the milliseconds a whole sweep takes, and closing
    it entirely would need the kernel to hand out the descriptor and the
    credentials in one call, which it does not.
    """
    try:
        with open(f"/proc/{pid}/status", "rb") as fh:
            for line in fh:
                if line.startswith(b"Uid:"):
                    if int(line.split()[2]) != uid:
                        return False   # recycled — not ours to touch
                    break
            else:
                return False
    except (OSError, ValueError, IndexError):
        return False

    try:
        fd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError, AttributeError):
        # Old kernel or the process is already gone. os.kill is the fallback,
        # and it has just been uid-checked above.
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False
    try:
        signal.pidfd_send_signal(fd, sig)
        return True
    except (ProcessLookupError, OSError):
        return False
    finally:
        os.close(fd)


def _kill_owned_by(uid: int, *, passes: int = 8, grace: float = 0.25) -> tuple[int, bool]:
    """SIGKILL everything running as `uid` until nothing is left.

    Returns (how many were signalled, whether the uid is now clean).

    Two things need this and both matter. `subprocess.run(timeout=)` kills the
    process it started and nothing it spawned, so a test suite that forks a dev
    server leaves it running. And a uid goes back in the pool for the NEXT
    tenant — a survivor would be sitting inside their checkout as a user that
    can read and write it.

    It LOOPS, and that is the correction to the first version of this. One
    pass is not enough for two independent reasons: a process that forks
    between the listdir and the kill is never seen at all, and SIGKILL is
    queued rather than executed, so a process still exists for a moment after
    it is signalled. Both were found by review rather than by the first probe,
    because a `sleep` neither forks nor lingers.

    Convergence — an empty scan, not a completed pass — is the exit condition,
    and the caller is told when it was not reached.
    """
    signalled = 0
    for _ in range(passes):
        pids = _pids_of(uid)
        if not pids:
            return signalled, True
        # STOP the whole set before killing any of it. A process that is
        # forking as fast as the sweep can read /proc is the one case a loop
        # alone does not converge on — each pass finds children the last pass
        # created. A stopped process cannot fork, so freezing first turns an
        # unbounded chase into two ordinary passes.
        for pid in pids:
            _signal_pid(pid, uid, signal.SIGSTOP)
        # Re-scan: anything forked between the scan and the freeze is caught
        # here, and its parent is already stopped so the set cannot grow again.
        for pid in _pids_of(uid):
            _signal_pid(pid, uid, signal.SIGSTOP)
        for pid in _pids_of(uid):
            # SIGKILL acts on a stopped process without it having to run.
            if _signal_pid(pid, uid, signal.SIGKILL):
                signalled += 1
        time.sleep(grace)
    return signalled, not _pids_of(uid)


def _release(uid: int) -> None:
    """Give a uid back, but only once it is provably empty.

    A uid that will not clear is QUARANTINED rather than reissued. Handing it
    over anyway is the exact failure this design exists to prevent — the next
    tenant's checkout is chowned to a uid somebody else's process is still
    running as, and 0700 then protects nothing.

    Losing the slot permanently would be its own bug, so the reaper below
    keeps trying and returns it the moment it comes clean.
    """
    _, clean = _kill_owned_by(uid)
    if clean:
        _uids.put(uid)
        return
    logger.error("uid_quarantined uid=%d reason=processes_survived_kill", uid)
    with _quarantine_lock:
        _quarantine.add(uid)


def _reaper() -> None:
    """Retry quarantined uids forever, returning each once it is empty.

    Without this a uid that briefly refused to die — a process wedged in an
    uninterruptible read, say — would cost a slot until the container
    restarted, and four of those is a sandbox that only ever answers "busy".
    """
    while True:
        time.sleep(REAP_INTERVAL_SECONDS)
        with _quarantine_lock:
            pending = list(_quarantine)
        for uid in pending:
            if not _pids_of(uid):
                with _quarantine_lock:
                    _quarantine.discard(uid)
                _uids.put(uid)
                logger.info("uid_recovered uid=%d", uid)
            else:
                _kill_owned_by(uid, passes=2)


def _lease(count: int, max_wait: float) -> list[int] | None:
    """Take `count` uids, or none at all.

    All-or-nothing because partial leases deadlock: two heavy jobs each holding
    half the pool, each blocked waiting for the other half. Whatever was taken
    is returned before giving up, so a refusal costs nothing.

    Only the FIRST uid is used to run anything. The rest are held purely to
    keep the pool from over-committing memory — a build that peaks at a
    gigabyte is charged for the space it actually occupies.
    """
    held: list[int] = []
    deadline = time.monotonic() + max_wait
    while len(held) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for uid in held:
                _uids.put(uid)
            return None
        try:
            held.append(_uids.get(timeout=remaining))
        except queue.Empty:
            for uid in held:
                _uids.put(uid)
            return None
    return held


def _make_git_repo(work: Path, uid: int, branch: str) -> None:
    """Give the checkout a real git repository of its own.

    The tenant's `.git` is deliberately NOT shipped: it holds the remote URL,
    and a credentialed one would carry a token straight out of the api
    container. But its absence broke a whole class of tooling for a reason
    that has nothing to do with the code under test — `git status`, `git diff`,
    `pre-commit`, and anything that shells out to git, all failing with "not a
    git repository".

    So we make one here instead. No remote, no credentials, no history from
    anywhere: `git init`, everything staged and committed once, on a branch
    named after the real one so `git rev-parse --abbrev-ref HEAD` answers
    truthfully. `git diff` then shows what the command itself changed, which is
    usually what a formatter or a codegen check is actually asking.

    What this still cannot give: tags, and therefore `git describe` and
    setuptools_scm versioning. Those need history we do not have and will not
    invent.

    Best effort. A repository that fails to initialise is worth a log line, not
    a failed job — the command may not care about git at all.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Celmis sandbox",
        "GIT_AUTHOR_EMAIL": "sandbox@celmis.local",
        "GIT_COMMITTER_NAME": "Celmis sandbox",
        "GIT_COMMITTER_EMAIL": "sandbox@celmis.local",
    }
    steps = (
        ["git", "init", "-q", "-b", branch],
        ["git", "add", "-A"],
        # --no-verify: the tree may contain the tenant's own hooks, and running
        # them here would execute repo code as a side effect of SETUP, before
        # the command we were actually asked to run.
        ["git", "commit", "-q", "--no-verify", "--allow-empty",
         "-m", "sandbox baseline"],
    )
    for step in steps:
        try:
            r = subprocess.run(  # noqa: S603 — fixed argv, no shell
                step, cwd=str(work), env=env, capture_output=True,
                text=True, timeout=120,
                user=uid, group=uid, extra_groups=[],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("git_setup_failed step=%s err=%s", step[1], exc)
            return
        if r.returncode != 0:
            logger.warning("git_setup_failed step=%s rc=%d err=%s",
                           step[1], r.returncode, (r.stderr or "")[:200])
            return


def _run(job: dict) -> dict:
    command = job.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        return {"ok": False, "error": "no command"}
    try:
        # Inside the guard: this used to sit above it, where a non-numeric
        # `timeout` raised through the handler thread and the caller got a
        # dropped connection instead of an error it could read.
        timeout = min(int(job.get("timeout") or 300), MAX_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return {"ok": False, "exit_code": None, "error": "timeout is not a number"}

    kind = classify(command)
    weight = min(BUILD_WEIGHT, SLOTS) if kind == "build" else 1
    ceiling = BUILD_TIMEOUT_SECONDS if kind == "build" else CHECK_TIMEOUT_SECONDS
    timeout = min(timeout, ceiling)

    # Take `weight` slots, and take them ALL OR NOTHING. Grabbing them one at a
    # time is a deadlock: two builds each holding half the pool, each waiting
    # for the other's half, until both time out.
    # Never queue for longer than the caller says it will wait. Absent — an
    # older api that does not send it — falls back to this server's own
    # ceiling, which is what it did before and no worse than before.
    declared = job.get("wait_budget")
    try:
        patience = float(declared) - WAIT_BUDGET_RESERVE_SECONDS
    except (TypeError, ValueError):
        patience = float(SLOT_WAIT_SECONDS)
    max_wait = max(0.0, min(float(SLOT_WAIT_SECONDS), patience))

    leased = _lease(weight, max_wait)
    if leased is None:
        logger.warning(
            "job_rejected reason=no_free_slot kind=%s need=%d of=%d waited=%.0fs",
            kind, weight, SLOTS, max_wait)
        return {"ok": False, "exit_code": None,
                "error": f"the sandbox is busy ({SLOTS} slots, this job needs "
                         f"{weight}); try again shortly"}
    uid = leased[0]

    work = WORK_ROOT / f"job-{uuid.uuid4().hex[:12]}"
    try:
        blob = job.get("_tar") or b""
        if blob:
            _safe_extract(blob, work)
        else:
            work.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 0700 from the moment it exists — `_safe_extract` sets it before it
        # writes anything, so there is no window where the tree is world
        # readable while a large checkout unpacks. WORK_ROOT being 0711 means a
        # neighbour cannot learn the name either; two reasons, not one.
        work.chmod(0o700)
        # /tmp is 1777, as /tmp has to be, which makes it the one place two
        # concurrent tenants can still see each other's files — a build writing
        # a temp file with source in it, at the default umask, is readable by
        # the neighbours. Give each job its own and point everything at it.
        tmpdir = work / ".tmp"
        tmpdir.mkdir(exist_ok=True)
        _chown_tree(work, uid)
        work.chmod(0o700)
        tmpdir.chmod(0o700)
        # A deliberately small environment: no inherited secrets, no proxy
        # variables the caller did not ask for.
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(work),
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
            "CI": "1",
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        _make_git_repo(work, uid, job.get("branch") or "work")
        logger.info("job_start uid=%d kind=%s weight=%d cmd=%s timeout=%ss",
                    uid, kind, weight, command[:120], timeout)
        proc = subprocess.Popen(  # noqa: S602 — running a command is the job
            ["/bin/bash", "-lc", command],
            cwd=str(work), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # user/group/extra_groups are applied by CPython between fork and
            # exec, in C. `preexec_fn` would be the obvious way to do this and
            # is documented as unsafe in a threaded process — which this is.
            #
            # extra_groups=[] is not decoration: without it the child keeps
            # root's supplementary groups and the uid change buys nothing.
            user=uid, group=uid, extra_groups=[],
        )
        # Popen rather than run(capture_output=True), and the difference is the
        # memory: run() accumulates the WHOLE of both streams here before
        # anything is clipped, so one tenant printing a few gigabytes OOM-kills
        # the container and takes the other tenants' jobs with it. These
        # threads keep a bounded slice and go on draining the rest.
        out_box: list = []
        err_box: list = []
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, MAX_OUTPUT_BYTES, out_box),
                             daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, MAX_OUTPUT_BYTES, err_box),
                             daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning("job_timeout uid=%d after %ss", uid, timeout)
            # Kill the whole uid, not just this process: a grandchild holding
            # the pipe open would keep the readers below blocked forever.
            _kill_owned_by(uid)
            proc.wait(timeout=30)

        for reader in readers:
            reader.join(timeout=30)
        stdout_bytes, out_dropped = out_box[0] if out_box else (b"", 0)
        stderr_bytes, err_dropped = err_box[0] if err_box else (b"", 0)
        out = stdout_bytes.decode("utf-8", "replace")
        err = stderr_bytes.decode("utf-8", "replace")
        if out_dropped:
            out += f"\n… [{out_dropped} bytes dropped]"
        if err_dropped:
            err += f"\n… [{err_dropped} bytes dropped]"

        if timed_out:
            return {"ok": False, "exit_code": None,
                    "error": f"timed out after {timeout}s",
                    "stdout": out, "stderr": err,
                    "clipped": bool(out_dropped or err_dropped)}

        logger.info("job_done uid=%d exit=%s", uid, proc.returncode)
        return {
            "ok": True, "exit_code": proc.returncode,
            "stdout": out, "stderr": err,
            "clipped": bool(out_dropped or err_dropped),
        }
    except Exception as exc:  # noqa: BLE001 — never take the server down
        logger.warning("job_failed uid=%d err=%s", uid, exc)
        return {"ok": False, "error": str(exc)[:400], "exit_code": None}
    finally:
        # Order matters, in this order and no other:
        #   kill    — a live process recreates whatever rmtree removes;
        #   rmtree  — while nothing is left to write;
        #   release — which kills again and only hands the uid back once it is
        #             provably empty, quarantining it if it is not.
        try:
            signalled, _ = _kill_owned_by(uid)
            if signalled:
                logger.warning("job_leftovers uid=%d killed=%d", uid, signalled)
        except Exception as exc:  # noqa: BLE001
            logger.error("job_cleanup_failed uid=%d err=%s", uid, exc)
        shutil.rmtree(work, ignore_errors=True)
        for held in leased:
            _release(held)


class _Handler(BaseHTTPRequestHandler):
    server_version = "celmis-sandbox"

    def log_message(self, fmt, *args):  # noqa: A003 — quieten the default logger
        logger.debug(fmt, *args)

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        self._reply(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/run":
            return self._reply(404, {"error": "not found"})
        if TOKEN and self.headers.get("X-Sandbox-Token") != TOKEN:
            return self._reply(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, {"error": "bad length"})
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            return self._reply(413, {"error": "payload too large"})

        raw = self.rfile.read(length)
        # Wire format: a JSON header line, then the raw tar bytes. Simpler and
        # cheaper than multipart, and there is exactly one producer.
        try:
            newline = raw.index(b"\n")
            job = json.loads(raw[:newline].decode("utf-8"))
            job["_tar"] = raw[newline + 1:]
        except Exception as exc:  # noqa: BLE001
            return self._reply(400, {"error": f"bad payload: {exc}"[:200]})
        self._reply(200, _run(job))


def main() -> None:
    # An empty token disables the check in `do_POST` entirely — `if TOKEN and
    # …` — so starting without one would serve arbitrary command execution to
    # anything that can reach the port. Refusing to start is the only safe
    # reading of "no token": the api treats an unreachable sandbox as no
    # sandbox and carries on, so this costs a feature, never the deployment.
    if not TOKEN:
        raise SystemExit(
            "SANDBOX_TOKEN is empty. Refusing to start: without it every "
            "request would be accepted, and this process runs commands."
        )
    # Dropping to a per-job uid needs the privilege to do so. Without it every
    # job would silently run as this process's own user — which is the
    # cross-tenant read this design exists to close — so it is checked once,
    # loudly, at startup rather than discovered from a leak later.
    if os.geteuid() != 0:
        raise SystemExit(
            f"Running as uid {os.geteuid()}, so jobs cannot be given separate "
            "users and concurrent tenants would share one. Start this "
            "container as root with cap_add: [SETUID, SETGID, CHOWN, KILL] — "
            "the JOBS still run unprivileged, which is the point."
        )

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    # 0711: a job can traverse into its own directory by name and cannot list
    # what else is here. With 0700 job dirs that is two independent reasons a
    # concurrent tenant's checkout stays unreadable.
    WORK_ROOT.chmod(0o711)

    for offset in range(SLOTS):
        _uids.put(UID_BASE + offset)
    threading.Thread(target=_reaper, name="uid-reaper", daemon=True).start()
    logger.info("sandbox slots=%d uids=%d..%d work_root=%s",
                SLOTS, UID_BASE, UID_BASE + SLOTS - 1, WORK_ROOT)
    # The upload ceiling is per request and the requests are concurrent, so the
    # figure that matters is the product. Stated at startup because raising
    # SANDBOX_SLOTS in .env is a one-word change with a memory cost nobody is
    # otherwise reminded of, and the symptom is an OOM kill during an unrelated
    # job.
    logger.info(
        "sandbox peak upload buffer = %d MB (%d slots x %d MB) — keep this "
        "well under the container's mem_limit",
        SLOTS * MAX_UPLOAD_BYTES // 1024 // 1024,
        SLOTS, MAX_UPLOAD_BYTES // 1024 // 1024,
    )

    port = int(os.environ.get("SANDBOX_PORT", "8900"))
    logger.info("sandbox listening on :%d", port)
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
