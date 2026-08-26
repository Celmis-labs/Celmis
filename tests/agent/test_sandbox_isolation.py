"""The sandbox is a place to run a tenant's code that holds nothing worth stealing.

The api container holds /workspace/data/secrets/.master.key and
credentials.db — the Fernet key and every tenant's git tokens, in one
directory, owned by the same user the agent runs as. Verified on production,
along with postgres:5432, litellm:4000 and qdrant:6333 all answering from that
container. Executing a repository's own test command there is a cross-tenant
credential compromise that needs no exploit: `cat` is enough.

So these tests are almost entirely about ABSENCES — the volume that is not
mounted, the variable that is not passed, the network that is not joined. Each
one is a line somebody could add back in good faith while wiring up a feature,
and none of them would fail anything else.
"""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path

from src.agent import sandbox

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def _sandbox_block(*, with_comments: bool = True) -> str:
    i = COMPOSE.find("\n  sandbox:")
    assert i > 0, "there is no sandbox service"
    j = COMPOSE.find("\nvolumes:", i)
    block = COMPOSE[i:j if j > 0 else len(COMPOSE)]
    if with_comments:
        return block
    # The comments in that block NAME the things that must not be there, in
    # order to explain why they are absent. Only configuration counts.
    return "\n".join(
        line for line in block.splitlines()
        if not line.lstrip().startswith("#")
    )


# ─── what the sandbox must NOT have ──────────────────────────────────


def test_it_mounts_no_workspace_volume():
    """workspace_data is the directory with the Fernet key and the credential
    store. Mounting it here would undo the entire point in one line."""
    block = _sandbox_block(with_comments=False)
    for volume in ("workspace_data", "vault_data", "postgres_data"):
        assert volume not in block, f"{volume} is mounted into the sandbox"


def test_it_receives_no_secret_environment():
    block = _sandbox_block(with_comments=False)
    forbidden = ("POSTGRES_", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
                 "GITHUB_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN",
                 "CELMIS_JWT_SECRET", "LITELLM_", "OPENAI_API_KEY",
                 "CELMIS_OPS_TOKEN")
    leaked = [name for name in forbidden if name in block]
    assert not leaked, f"secret env reaches the sandbox: {leaked}"


def test_it_is_not_on_the_default_network():
    """`postgres`, `litellm` and `qdrant` are resolvable by name on the default
    network. The sandbox must not be able to name them."""
    block = _sandbox_block()
    assert "networks:" in block, "no explicit networks — it inherits `default`"
    assert "sandbox_net" in block
    assert re.search(r"networks:\s*\n\s*- default", block) is None, (
        "the sandbox is on the default network, where the databases live"
    )


def test_api_keeps_both_networks():
    """Naming any network drops the implicit default, so api has to list both
    or it loses postgres."""
    i = COMPOSE.find("\n  api:")
    j = COMPOSE.find("\n  web:", i)
    api = COMPOSE[i:j if j > 0 else len(COMPOSE)]
    assert "- default" in api and "- sandbox_net" in api


# ─── the limits that stop it taking the host with it ─────────────────


def test_the_root_filesystem_is_read_only_with_a_writable_tmpfs():
    block = _sandbox_block()
    assert "read_only: true" in block
    assert "tmpfs:" in block
    assert "/work:" in block
    # A test run has to execute what it just installed.
    assert "exec" in block


def test_resource_limits_are_set():
    """pids_limit is not decoration: without it a fork bomb in somebody's test
    suite takes the whole host down — the exact outcome this service exists to
    prevent."""
    block = _sandbox_block()
    for limit in ("pids_limit:", "mem_limit:", "cpus:"):
        assert limit in block, f"{limit} missing"
    assert "no-new-privileges:true" in block
    assert "cap_drop:" in block


def test_the_commands_run_as_a_non_root_user():
    """This used to assert `USER sandbox`, and that was the right shape while
    a job ran alone. It stopped being right when jobs became concurrent and
    cross-tenant: one user for every tenant means tenant A reads tenant B's
    checkout out of /work.

    So the SERVER is root now — setting a job's uid requires the privilege to
    set uids — and the JOBS are not. That is the property worth pinning, and
    tests/security/test_sandbox_tenant_isolation.py holds the rest of it.
    """
    dockerfile = (ROOT / "Dockerfile.sandbox").read_text()
    assert "useradd" in dockerfile
    server = (ROOT / "src" / "sandbox" / "server.py").read_text()
    assert "user=uid, group=uid, extra_groups=[]" in server, (
        "jobs are no longer given their own user"
    )
    # …and root is not the accident it would look like without this.
    assert "os.geteuid() != 0" in server


def test_the_image_carries_no_celmis_source_beyond_the_server():
    """A stray `src.*` import would drag settings and a database driver into
    the container that runs untrusted code."""
    dockerfile = (ROOT / "Dockerfile.sandbox").read_text()
    copies = re.findall(r"^COPY\s+(?!--)(\S+)", dockerfile, re.M)
    assert copies == ["src/sandbox/server.py"], f"copies more than the server: {copies}"
    # Comments here name PYTHONPATH in order to say why it is absent, so only
    # instructions count.
    instructions = "\n".join(
        line for line in dockerfile.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "PYTHONPATH" not in instructions, (
        "PYTHONPATH into the Celmis tree — a stray src.* import would drag "
        "settings and a database driver into the box that runs untrusted code"
    )


def test_the_server_has_no_third_party_import():
    """A web framework inside the box that runs untrusted code is one more
    thing to keep patched."""
    source = (ROOT / "src" / "sandbox" / "server.py").read_text()
    for banned in ("import fastapi", "import flask", "from fastapi",
                   "import httpx", "from src."):
        assert banned not in source, f"{banned} in the sandbox server"


# ─── the payload ─────────────────────────────────────────────────────


def test_git_never_travels(tmp_path):
    """.git carries the remote URL, and a credentialed URL carries the token
    with it. The clone and the push are both done by api."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = https://x:token@github.com/a/b")
    (tmp_path / "app.py").write_text("x = 1\n")

    blob = sandbox._pack(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        names = tar.getnames()
    assert not any(".git" in n for n in names), names
    assert any(n.endswith("app.py") for n in names)


def test_heavy_directories_are_left_behind(tmp_path):
    for junk in ("node_modules", ".venv", "__pycache__"):
        d = tmp_path / junk
        d.mkdir()
        (d / "big").write_text("x" * 1000)
    (tmp_path / "main.py").write_text("y = 2\n")

    with tarfile.open(fileobj=io.BytesIO(sandbox._pack(tmp_path))) as tar:
        names = tar.getnames()
    assert not any("node_modules" in n or ".venv" in n for n in names)
    assert any(n.endswith("main.py") for n in names)


def test_host_ownership_does_not_travel(tmp_path):
    (tmp_path / "a.py").write_text("z = 3\n")
    with tarfile.open(fileobj=io.BytesIO(sandbox._pack(tmp_path))) as tar:
        for member in tar.getmembers():
            assert member.uid == 0 and member.uname == ""


def test_an_unconfigured_sandbox_fails_closed_rather_than_half_working(monkeypatch, tmp_path):
    """A deployment without a sandbox must keep working with execution simply
    unavailable — never a feature that half runs."""
    monkeypatch.setattr(sandbox, "SANDBOX_TOKEN", "")
    assert sandbox.available() is False
    result = sandbox.run(tmp_path, "echo hi")
    assert not result.ok and not result.succeeded
    assert "no sandbox" in result.error


def test_a_failed_run_reports_rather_than_raises(monkeypatch, tmp_path):
    """The runner must never die because the sandbox is down."""
    monkeypatch.setattr(sandbox, "SANDBOX_TOKEN", "t")
    (tmp_path / "a.py").write_text("1\n")

    # The seam moved with the egress conversion: run() no longer calls the
    # module verb httpx.post (which built an unguarded client per call) but
    # posts on a client from src.http.build_client, so "down" is simulated
    # where the client now comes from.
    import src.http

    class _Down:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise OSError("down")

    monkeypatch.setattr(src.http, "build_client", lambda *a, **k: _Down())
    result = sandbox.run(tmp_path, "pytest")
    assert not result.ok
    assert "unreachable" in result.error


def test_success_is_exit_zero_and_nothing_else():
    ok = sandbox.SandboxResult(ok=True, exit_code=0)
    assert ok.succeeded
    for bad in (sandbox.SandboxResult(ok=True, exit_code=1),
                sandbox.SandboxResult(ok=False, exit_code=0)):
        assert not bad.succeeded


# ─── the server's own guards ─────────────────────────────────────────


def test_the_server_refuses_an_archive_that_escapes(tmp_path):
    """`extractall` writes through `../` on older Pythons, and this input
    arrives from a caller treated as semi-trusted at best."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sandbox_server", ROOT / "src" / "sandbox" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        payload = b"x"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    import pytest
    with pytest.raises(ValueError, match="escapes"):
        server._safe_extract(buf.getvalue(), tmp_path / "work")


def test_the_server_clips_its_output():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sandbox_server2", ROOT / "src" / "sandbox" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    text, clipped = server._clip("a" * (server.MAX_OUTPUT_BYTES * 2))
    assert clipped
    assert "bytes dropped" in text
    assert len(text.encode()) < server.MAX_OUTPUT_BYTES * 1.2
