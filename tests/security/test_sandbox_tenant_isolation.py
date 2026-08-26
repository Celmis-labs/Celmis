"""Two tenants building at the same time must not be able to read each other.

Moving execution out of the api container closed the big leak — the Fernet key
and every tenant's git tokens live there. It left a smaller one exactly one
level down: jobs ran concurrently, in one container, as ONE user, under a 1777
/work. Tenant A's test command could read tenant B's checkout with `cat`. The
same failure, in a smaller room.

A queue — one job at a time — would also close it, and was rejected: this has
to hold under parallel load. So each job leases its own uid and the isolation
is the kernel's.

Most of what follows is about the seams of that, because the uid change itself
is the easy part:

  * the ORDER of extract → chown → chmod, since a directory that is briefly
    readable is readable;
  * what a job leaves RUNNING, because the uid is handed to the next tenant;
  * /tmp, which is 1777 by definition and is therefore the one place two
    tenants can still see each other;
  * the slot pool, because a uid that leaks out of it is capacity gone until
    the container restarts.
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import subprocess
import textwrap
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH = ROOT / "src" / "sandbox" / "server.py"
SERVER_SRC = SERVER_PATH.read_text()
DOCKERFILE = (ROOT / "Dockerfile.sandbox").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()


@pytest.fixture(scope="module")
def server():
    """The sandbox server module, loaded standalone.

    It is deliberately not importable as `src.sandbox.server` in the image —
    it ships as a single file with no Celmis on the path.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("sandbox_server_iso", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sandbox_service() -> str:
    i = COMPOSE.find("\n  sandbox:")
    j = COMPOSE.find("\nvolumes:", i)
    return COMPOSE[i:j if j > 0 else len(COMPOSE)]


def _uncommented(text: str) -> str:
    """YAML with comment lines and trailing comments removed.

    Trailing matters here: `- SETUID  # run each job as its own user` is one
    line that is both configuration and prose.
    """
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def _code(obj) -> str:
    """Python source with every comment and docstring removed.

    Grepping raw source is how a guard passes while the thing it guards is
    gone, because the comment that explains an ABSENCE contains the word:
    `_run` says "preexec_fn would be the obvious way and is documented as
    unsafe", and a test asserting `"preexec_fn" not in source` fails on the
    sentence congratulating the code for not using it.

    That has now happened four times on this repository, in four different
    files, always to a test written to protect something real. So the strip is
    done properly this time.

    Comments and docstrings are blanked IN PLACE, by token position, rather
    than the tree being reprinted: `ast.unparse` drops comments perfectly and
    also rewrites every literal it touches, turning `chmod(0o711)` into
    `chmod(457)` and `"TMPDIR"` into `'TMPDIR'` — so the assertions would have
    had to be written in a form nobody reading them could check against the
    file. Blanking keeps the source character-for-character.
    """
    source = textwrap.dedent(inspect.getsource(obj))
    lines = source.splitlines(keepends=True)

    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    # Comments: tokenize knows them exactly.
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            spans.append((tok.start, tok.end))

    # Docstrings: the AST, not a token heuristic. "A string with a newline on
    # each side" also describes a multi-line implicitly-concatenated argument —
    #     logger.info(
    #         "sandbox peak upload buffer = %d MB "
    #         "keep this under mem_limit", ...)
    # — and blanking THAT deletes real code from under the assertions, quietly,
    # in a helper whose whole job is to make assertions trustworthy. Only the
    # tree can tell a statement from an argument.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append(
                ((first.lineno, first.col_offset),
                 (first.end_lineno, first.end_col_offset)),
            )

    for (srow, scol), (erow, ecol) in spans:
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line.rstrip("\n"))
            lines[row - 1] = (
                line[:start] + " " * (end - start) + line[end:]
            )
    return "".join(lines)


def _finally_of_run(server) -> str:
    """The cleanup block of `_run`, as code rather than as text.

    The order of kill → rmtree → release is the whole correctness of uid
    reuse, and every one of those three words also appears in the comments
    explaining why that order matters.
    """
    body = _code(server._run)
    return body[body.rindex("finally:"):]


def test_the_stripper_removes_prose_and_keeps_code(server):
    """Guards the guard, in both directions.

    Every assertion below is only as trustworthy as `_code`, and it can fail
    two opposite ways. Too little: a comment explaining an absence keeps the
    word alive and the test passes on prose. Too much: its first version used a
    token heuristic — "a string with a newline on each side" — which is also
    what a multi-line implicitly concatenated ARGUMENT looks like, so it
    silently deleted a real `logger.info(...)` from under the assertions.

    Both hazards are checked against the actual server, so this cannot drift
    away from what it protects.
    """
    run = _code(server._run)
    main = _code(server.main)
    release = _code(server._release)

    # Prose is gone…
    assert "preexec_fn" not in run, "a comment survived"
    assert "provably empty" not in release, "a docstring survived"
    # …and code is not. Both of these are STRING ARGUMENTS spread over several
    # lines, which is what the first version of this helper mistook for a
    # docstring and deleted.
    assert "peak upload buffer" in main, (
        "a multi-line string argument was mistaken for a docstring"
    )
    assert "Refusing to start" in main, (
        "the message raised when there is no token was stripped as prose"
    )
    assert "WORK_ROOT.chmod(0o711)" in main, "a literal was rewritten"
    assert "user=uid, group=uid, extra_groups=[]" in run


# ─── one tenant, one user ────────────────────────────────────────────


def test_each_job_runs_as_its_own_user(server):
    body = _code(server._run)
    assert "user=uid" in body and "group=uid" in body


def test_the_child_does_not_keep_the_servers_groups(server):
    """Without extra_groups=[] the child inherits root's supplementary groups
    and the uid change buys nothing."""
    assert "extra_groups=[]" in _code(server._run)


def test_the_uid_change_does_not_go_through_preexec_fn(server):
    """`preexec_fn` is documented as unsafe in a threaded process, and this
    server is a ThreadingHTTPServer. user=/group= are applied in C between
    fork and exec instead."""
    assert "preexec_fn" not in _code(server._run)
    assert "ThreadingHTTPServer" in _code(server.main)


def test_two_concurrent_jobs_cannot_get_the_same_uid(server):
    """The pool IS the allocator: a leased uid is out of the queue until the
    finally block returns it."""
    server._uids.queue.clear()
    for offset in range(3):
        server._uids.put(server.UID_BASE + offset)
    first = server._uids.get_nowait()
    second = server._uids.get_nowait()
    assert first != second


def test_running_out_of_slots_refuses_rather_than_doubling_up(server):
    """Taking a slot anyway is how one busy minute becomes an OOM kill that
    takes the other tenants' jobs with it."""
    run = _code(server._run)
    lease = _code(server._lease)
    # The waiting and the refusal live in the allocator now; `_run` turns a
    # refusal into an answer rather than taking a slot anyway.
    assert "queue.Empty" in lease
    # The bound is an ARGUMENT now, not this module's constant read from
    # inside the allocator: the wait a caller will actually tolerate is the
    # caller's fact, and `_run` folds it together with this server's ceiling.
    assert "max_wait" in lease
    assert "SLOT_WAIT_SECONDS" in run
    assert "if leased is None:" in run
    assert "the sandbox is busy" in run


def test_a_slot_is_returned_on_every_path(server):
    """A uid lost on an error path is capacity gone until the container
    restarts — and four of those is a sandbox that answers only "busy"."""
    finally_block = _finally_of_run(server)
    # Every slot the job took, not just the one it ran as — a heavy job holds
    # several, and leaking the extras shrinks the pool silently.
    assert "for held in leased:" in finally_block
    assert "_release(held)" in finally_block


# ─── the seams ───────────────────────────────────────────────────────


def test_the_checkout_is_locked_down_before_the_job_starts(server):
    """Order is the whole thing: chown and chmod must both land before the
    command runs, or there is a window where the tree is readable."""
    body = _code(server._run)
    assert body.index("_chown_tree(work, uid)") < body.index("subprocess.Popen")
    assert body.index("work.chmod(0o700)") < body.index("subprocess.Popen")


def test_the_work_root_cannot_be_listed_by_a_job(server):
    """0700 on each job dir stops a read. 0711 on the parent stops the job
    from learning the names in the first place — two independent reasons,
    which is what makes a single mistake survivable."""
    body = _code(server.main)
    assert "WORK_ROOT.chmod(0o711)" in body
    service = _uncommented(_sandbox_service())
    assert "/work:rw,exec,mode=0711" in service, (
        "the tmpfs is mounted world-writable again; the chmod in main() is "
        "then the only thing standing between two tenants"
    )
    assert "mode=1777,size=2g" not in service


def test_each_job_gets_a_private_tmpdir(server):
    """/tmp is 1777 because that is what /tmp means, so it is the one place
    two tenants can still see each other's files. A build writing a temp file
    with source in it, at the default umask, would be readable next door."""
    body = _code(server._run)
    for var in ('"TMPDIR"', '"TEMP"', '"TMP"'):
        assert var in body, f"{var} still points at the shared /tmp"
    assert "tmpdir.chmod(0o700)" in body


def test_symlinks_are_never_followed_out_of_the_tree(server):
    """chown following a symlink would hand a file outside the job's tree to
    the job's user."""
    assert "os.lchown" in _code(server._chown_tree)
    assert "os.chown(" not in SERVER_SRC


# ─── the uid is handed to the next tenant ────────────────────────────


def test_what_a_job_leaves_running_is_killed(server):
    """`subprocess.run(timeout=)` kills the process it started and nothing it
    spawned. A survivor sits inside the checkout as a user that is about to
    belong to somebody else."""
    body = _code(server._run)
    assert "_kill_owned_by(uid)" in body
    kill = _code(server._kill_owned_by)
    assert "SIGKILL" in kill


def test_the_kill_loops_until_nothing_is_left(server):
    """One pass is not enough, for two independent reasons: a process that
    forks between the listdir and the kill is never seen, and SIGKILL is
    queued rather than executed, so a process still exists for a moment after
    being signalled.

    Neither showed up in the live probe, because the `sleep 300` it left
    behind neither forks nor lingers. Four independent reviewers found it by
    reading. That is what this test is for.
    """
    kill = _code(server._kill_owned_by)
    assert "for _ in range(passes)" in kill, "still a single pass"
    assert "if not pids:" in kill, "no convergence check — it just repeats"
    assert "time.sleep(grace)" in kill, "no pause, so the loop races the kernel"


def test_the_set_is_frozen_before_it_is_killed(server):
    """A loop alone does not converge on a process that forks as fast as the
    sweep can read /proc — each pass finds children the last pass created. A
    stopped process cannot fork, so freezing first turns an unbounded chase
    into two ordinary passes."""
    kill = _code(server._kill_owned_by)
    assert "SIGSTOP" in kill
    assert kill.index("SIGSTOP") < kill.index("SIGKILL")


def test_a_recycled_pid_is_never_signalled(server):
    """This process is root with CAP_KILL, and a pid is a number the kernel
    reuses. The pid was learned by reading /proc a moment earlier, so an
    unchecked kill could land on another tenant's job."""
    send = _code(server._signal_pid)
    assert "!= uid" in send, "the uid is not re-checked before signalling"
    assert "pidfd_open" in send, (
        "a descriptor names the process rather than the number, so nothing "
        "after the check can redirect the signal"
    )
    # And the fallback for a kernel without pidfd is still uid-checked.
    assert send.index("!= uid") < send.index("os.kill")


def test_every_signal_goes_through_that_check(server):
    kill = _code(server._kill_owned_by)
    assert "os.kill" not in kill, "a raw kill bypasses the recycled-pid check"
    assert kill.count("_signal_pid") >= 3


def test_the_upload_ceiling_accounts_for_concurrency(server):
    """It is per REQUEST and the requests are concurrent, so what the box has
    to hold is SLOTS × this. 200MB was sized for one job at a time."""
    assert server.MAX_UPLOAD_BYTES <= 128 * 1024 * 1024
    assert "peak upload buffer" in _code(server.main), (
        "raising SANDBOX_SLOTS is a one-word change with a memory cost that "
        "is otherwise invisible until an OOM kill during an unrelated job"
    )


def test_the_kill_reports_whether_it_actually_converged(server):
    """A cleanup that cannot fail is a cleanup nobody checks."""
    kill = _code(server._kill_owned_by)
    assert "return signalled, True" in kill
    assert "return signalled, not _pids_of(uid)" in kill


def test_a_uid_that_will_not_clear_is_never_reissued(server):
    """The whole design in one line: the next tenant's checkout is chowned to
    this uid, so handing it over with somebody's process still on it makes
    0700 protect nothing at all."""
    release = _code(server._release)
    assert "if clean:" in release
    assert "_uids.put(uid)" in release
    assert "_quarantine.add(uid)" in release
    # …and the release path is the only way back into the pool.
    body = _code(server._run)
    assert "_uids.put" not in body, "a job returns its uid without proving it clean"


def test_a_quarantined_slot_is_not_lost_forever(server):
    """Four stuck uids and the sandbox only ever answers "busy". The reaper is
    what makes quarantine a delay rather than a leak."""
    reaper = _code(server._reaper)
    assert "_pids_of(uid)" in reaper
    assert "_uids.put(uid)" in reaper
    assert "_quarantine.discard(uid)" in reaper
    assert "uid-reaper" in _code(server.main), "the reaper is never started"


# ─── one tenant must not be able to exhaust the others ───────────────


def test_output_is_bounded_as_it_arrives(server):
    """`capture_output=True` accumulates BOTH streams whole in this process
    before anything is clipped, so one tenant printing a few gigabytes
    OOM-kills the container and takes the other jobs with it."""
    body = _code(server._run)
    assert "subprocess.Popen" in body
    assert "capture_output" not in body
    assert "_drain" in body


def test_the_reader_keeps_reading_after_the_limit(server):
    """Stopping at the limit fills the pipe and blocks the child in write(),
    which turns "printed too much" into "hung until timeout, holding a slot"."""
    drain = _code(server._drain)
    body = drain[drain.index("while True:"):]
    assert "break" in body
    # The only break is EOF — not the limit.
    assert body.index("if not chunk:") < body.index("break")
    assert "dropped += len(chunk)" in drain, "past the limit it stops reading"


def test_a_timeout_kills_the_whole_uid_before_waiting_on_the_readers(server):
    """A grandchild holding the pipe open outlives the process that was
    killed, and the drain threads would wait on it forever."""
    body = _code(server._run)
    timeout_block = body[body.index("except subprocess.TimeoutExpired:"):]
    assert timeout_block.index("_kill_owned_by(uid)") < timeout_block.index("proc.wait")


def test_a_timed_out_job_still_returns_what_it_printed(server):
    """The output up to the timeout is usually the only clue as to why."""
    body = _code(server._run)
    assert '"error": f"timed out after {timeout}s",' in body
    timed = body[body.index("if timed_out:"):]
    assert '"stdout": out' in timed


def test_the_archive_cannot_unpack_into_a_bomb(server):
    """The client caps the COMPRESSED upload. A few hundred megabytes of zeros
    is a few gigabytes on this side, into a tmpfs shared by every job."""
    extract = _code(server._safe_extract)
    assert "MAX_EXTRACTED_BYTES" in extract
    assert "total += member.size" in extract


def test_extraction_does_not_take_mode_bits_from_the_archive(server):
    """It runs as root. The default filter restores mode, uid and gid from
    bytes the archive chose — setuid included."""
    assert 'filter="data"' in _code(server._safe_extract)


def test_the_job_directory_is_private_before_anything_is_written(server):
    """Creating it at 0755 and tightening afterwards leaves the whole
    extraction — the slowest part of setup — with the tree world-readable,
    defended only by an unguessable name."""
    extract = _code(server._safe_extract)
    assert "mode=0o700" in extract
    assert extract.index("mkdir") < extract.index("extractall")
    assert "dest.chmod(0o700)" in extract, "mkdir's mode is masked by the umask"


def test_a_malformed_timeout_answers_instead_of_dropping_the_connection(server):
    """It used to be coerced above the guard, where a non-numeric value raised
    through the handler thread and the caller saw a closed socket."""
    body = _code(server._run)
    assert body.index("except (TypeError, ValueError)") < body.index("_lease(")
    assert "timeout is not a number" in body


def test_zombies_are_reaped_by_something(server):
    """PID 1 here is the python server, which never wait()s on processes it
    did not spawn. Every orphan a job leaves — and every one the sweep kills —
    stays as a zombie against the shared pids_limit, filling it slowly until
    nothing can fork."""
    assert "init: true" in _sandbox_service()


def test_shared_memory_cannot_be_listed_by_a_neighbour():
    """Unlike /tmp nothing can redirect /dev/shm — POSIX shared memory goes
    where the library decides. 0711 is the difference between stumbling on a
    neighbour's segment and having to guess its name."""
    service = _uncommented(_sandbox_service())
    assert "/dev/shm:rw,nosuid,nodev,mode=0711" in service


def test_the_residuals_that_were_not_closed_are_written_down():
    """A shared network namespace, one resource pool, and no tenant identity
    on the wire. Each needs either CAP_SYS_ADMIN or a contract change, and
    each was found by review rather than by the probe that passed. Silence
    about them would read as "the sandbox is airtight"."""
    for residual in ("ONE NETWORK NAMESPACE", "ONE RESOURCE POOL",
                     "NO TENANT IDENTITY"):
        assert residual in SERVER_SRC, residual


def test_the_kill_happens_before_the_uid_is_reused(server):
    """Order again: releasing first means the next tenant can be handed a uid
    that still has the previous one's processes under it."""
    finally_block = _finally_of_run(server)
    assert finally_block.index("_kill_owned_by") < finally_block.index("_release(held)")


def test_the_kill_happens_before_the_tree_is_removed(server):
    """A live process recreates what rmtree just deleted."""
    finally_block = _finally_of_run(server)
    assert finally_block.index("_kill_owned_by") < finally_block.index("rmtree")


def test_a_failed_cleanup_does_not_strand_the_slot(server):
    """If the kill raises, the uid still has to go back — losing it is worse
    than a stale process, and the next lease kills whatever it finds anyway."""
    finally_block = _finally_of_run(server)
    assert "except Exception" in finally_block
    assert finally_block.index("except Exception") < finally_block.index("_release(held)")


def test_the_process_sweep_needs_no_extra_binary(server):
    """`pkill` is not guaranteed to be in the image, and building a command
    line out of anything would be a new injection surface in the one process
    that runs shell commands."""
    scan = _code(server._pids_of)
    assert "/proc" in scan
    assert "subprocess" not in scan and "pkill" not in scan


def test_the_sweep_reads_the_effective_uid(server):
    """/proc/<pid>/status Uid: is real, effective, saved, fs. Effective is the
    one that decides what the process can read."""
    assert "line.split()[2]" in _code(server._pids_of)


def test_the_sweep_survives_a_process_exiting_underneath_it(server):
    """Between listdir and open, a pid can be gone. Raising there would skip
    the release below."""
    assert "except (OSError, ValueError, IndexError)" in _code(server._pids_of)


# ─── the privilege this costs, and the fences around it ──────────────


def test_the_server_needs_root_and_says_why(server):
    """Setting a job's uid requires the privilege to set uids. Without it this
    process could only run every tenant as ITSELF — the exact leak it exists
    to close — so it refuses to start rather than quietly do that."""
    body = _code(server.main)
    assert "os.geteuid() != 0" in body
    assert "SystemExit" in body


def test_the_image_no_longer_drops_to_a_single_user():
    """`USER sandbox` in the Dockerfile would make the check above fail at
    startup — correctly, but only after a deploy."""
    instructions = "\n".join(
        line for line in DOCKERFILE.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "USER sandbox" not in instructions
    assert "sbx" in instructions and "20000" in instructions


def test_only_the_capabilities_the_design_names_are_granted():
    """Root in a container is a real cost and is paid for exactly six things.
    Anything beyond this list needs its own reason."""
    service = _uncommented(_sandbox_service())
    assert "cap_drop:" in service and "- ALL" in service
    granted = set(re.findall(r"^\s*-\s+([A-Z_]+)\s*$", service, re.M)) - {"ALL"}
    assert granted, "the cap list did not parse — check _uncommented"
    assert granted == {"SETUID", "SETGID", "CHOWN", "FOWNER",
                       "DAC_OVERRIDE", "KILL"}, sorted(granted)


def test_a_job_cannot_pick_those_capabilities_back_up():
    """no_new_privileges stops execve from granting privilege, so a setuid
    binary in the image is not a route back to root."""
    assert "no-new-privileges:true" in _sandbox_service()


def test_the_limits_account_for_more_than_one_job():
    """512 pids and 2g were sized for one job at a time. Four concurrent
    builds sharing them is one `npm ci` away from OOM-killing the others."""
    service = _uncommented(_sandbox_service())
    pids = int(re.search(r"pids_limit:\s*(\d+)", service).group(1))
    assert pids >= 2048, pids
    assert "SANDBOX_SLOTS" in service


def test_the_slot_count_cannot_exceed_the_users_that_exist(server):
    """A slot beyond the pool would lease a uid with no account behind it, and
    subprocess would fail every job with the same opaque error."""
    assert server.SLOTS <= server.UID_POOL
    pool = int(re.search(r"SANDBOX_UID_POOL=(\d+)", DOCKERFILE).group(1))
    assert pool >= server.UID_POOL
    # The image really creates them.
    assert "seq 0 63" in DOCKERFILE
    assert "20000 + i" in DOCKERFILE


def test_the_job_users_are_not_logins():
    """They are identities, not accounts: no shell, no home, no password."""
    assert "--shell /usr/sbin/nologin" in DOCKERFILE
    assert "--no-create-home" in DOCKERFILE


# ─── the parts that must keep working ────────────────────────────────


def test_the_server_still_has_no_third_party_import():
    for banned in ("import fastapi", "import flask", "from fastapi",
                   "import httpx", "from src."):
        assert banned not in SERVER_SRC, banned


def test_the_server_still_parses():
    subprocess.run(["python3", "-c", f"import ast;ast.parse(open({str(SERVER_PATH)!r}).read())"],
                   check=True)


def test_the_archive_guard_survived(server):
    """Extraction happens as root now, which raises the stakes on it."""
    guard = _code(server._safe_extract)
    assert "escapes the work dir" in guard
    assert "link in archive" in guard


def test_extraction_is_not_trusted_to_set_ownership(server):
    """tarfile restores uid/gid from the archive when it runs as root. The
    chown afterwards is what makes the job's user the owner regardless of what
    the tarball claimed."""
    body = _code(server._run)
    assert body.index("_safe_extract") < body.index("_chown_tree")


def test_the_proc_leak_is_written_down_rather_than_forgotten():
    """One job CAN read another's command line through the shared /proc —
    checked by doing it, not assumed: a job running as sbx3 sees
    `sbx2 :: sleep 10`. Files, environment and output do not cross.

    It is documented instead of fixed because closing it needs hidepid=2 or a
    per-job PID namespace, and both need CAP_SYS_ADMIN — a far bigger grant
    than a command string is worth. This test exists so the decision stays a
    decision: if the docstring loses it, somebody has quietly concluded the
    sandbox is airtight when it is not.
    """
    assert "/proc is shared" in SERVER_SRC
    assert "CAP_SYS_ADMIN" in SERVER_SRC
    service = _uncommented(_sandbox_service())
    assert "SYS_ADMIN" not in service, (
        "SYS_ADMIN was granted — if that was to close the /proc leak, the "
        "trade just went the wrong way round"
    )


def test_nothing_reintroduced_a_global_queue(server):
    """A single lock around `_run` would close the leak too, and cost the
    parallelism this whole design was chosen for.

    There IS a lock now — `_quarantine_lock` — and it guards a set, not the
    work: it is held for a membership change and never across a job. The test
    is therefore about where the lock is, not whether one exists.
    """
    body = _code(server._run)
    assert "Lock" not in body, "a lock reached the job path"
    assert "acquire()" not in body
    guarded = _code(server._reaper) + _code(server._release)
    assert "_quarantine_lock" in guarded
    assert "subprocess" not in _code(server._release), (
        "the quarantine lock is held across a job"
    )


# ─── git, and what a job is allowed to cost ──────────────────────────


def test_a_job_gets_a_real_git_repository(server):
    """The tenant's own .git is not shipped — it holds the remote URL and a
    credentialed one would carry a token out of the api container. But its
    absence broke `git status`, `git diff` and pre-commit for a reason nothing
    to do with the code under test, so a fresh one is made here instead."""
    body = _code(server._make_git_repo)
    assert '"git", "init"' in body
    assert "-b" in body, "the branch is not named, so HEAD answers with a default"
    assert '"git", "commit"' in body
    # No remote, ever. That is the whole reason .git is not shipped.
    assert "remote" not in body.lower() or "no remote" in body.lower()


def test_repo_setup_runs_as_the_job_user_not_as_root(server):
    """Everything else in the job drops to the leased uid; a setup step that
    stayed root would leave root-owned objects in the tree the job then cannot
    write."""
    body = _code(server._make_git_repo)
    assert "user=uid, group=uid, extra_groups=[]" in body


def test_repo_setup_does_not_run_the_tenants_hooks(server):
    """The tree may carry the tenant's own hooks. Running them during SETUP
    executes repo code before the command we were actually asked to run."""
    assert "--no-verify" in _code(server._make_git_repo)


def test_a_failed_git_setup_does_not_fail_the_job(server):
    """The command may not care about git at all."""
    body = _code(server._make_git_repo)
    assert "return" in body
    assert "logger.warning" in body
    assert "raise" not in body


def test_the_job_kind_is_derived_not_asked_for(server):
    """A person asking for their tests to run should not first have to know
    what `npm ci` costs."""
    assert server.classify("pytest -q") == "check"
    assert server.classify("npm ci && npm test") == "build"
    assert server.classify("pip install -r requirements.txt && pytest") == "build"
    assert server.classify("ruff check .") == "check"
    # A chained command is one job, and it is the heavy one.
    assert server.classify("echo hi; poetry install") == "build"


def test_a_build_costs_more_slots_than_a_check(server):
    """mem_limit is container-wide and a per-job cgroup needs CAP_SYS_ADMIN,
    which this container does not have. Charging a heavy job more slots is the
    ceiling on concurrent heavy work that needs no new privilege."""
    import ast

    assert server.BUILD_WEIGHT >= 2
    body = _code(server._run)
    assert "BUILD_WEIGHT" in body
    # Structural, not textual: what must hold is that the weight is what `_run`
    # asks the allocator for. Pinning the literal `_lease(weight)` made the
    # test fail the day the call gained a second argument, which is the shape
    # of a test keyed on how the code is spelled rather than what it does.
    tree = ast.parse(body.lstrip())
    leases = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_lease"]
    assert leases, "_run no longer leases slots"
    assert any(getattr(a, "id", None) == "weight" for a in leases[0].args), (
        "the lease is not asked for the job's weight"
    )


def test_a_build_still_leaves_room_for_a_check(server):
    """Weighting a build at the whole pool would let one long `npm ci` block
    every quick check for its duration — the failure this exists to avoid."""
    assert server.BUILD_WEIGHT < server.SLOTS or server.SLOTS == 1


def test_slots_are_taken_all_or_nothing(server):
    """Grabbing them one at a time deadlocks: two builds each holding half the
    pool, each waiting for the other's half."""
    body = _code(server._lease)
    assert "return None" in body
    # Whatever was taken is handed back before giving up.
    put_backs = body.count("_uids.put(uid)")
    assert put_backs >= 2, "a refused lease leaks the slots it already took"


def test_a_refused_lease_returns_every_slot(server):
    """Exercised, not read: the pool must be exactly as full afterwards."""
    server._uids.queue.clear()
    for offset in range(2):
        server._uids.put(server.UID_BASE + offset)
    before = server._uids.qsize()

    assert server._lease(5, 0.0) is None          # more than exist
    assert server._uids.qsize() == before, "slots were lost on a refusal"


def test_a_heavy_job_gets_a_longer_clock(server):
    assert server.BUILD_TIMEOUT_SECONDS > server.CHECK_TIMEOUT_SECONDS
    assert "BUILD_TIMEOUT_SECONDS" in _code(server._run)
