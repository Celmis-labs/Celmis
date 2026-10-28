"""An uploaded archive is read by the dev tools the way a clone is.

An archive is unpacked without git (src/repos/upload.py), and every list of the
repositories on disk looked for `.git`. An uploaded repository was indexed, shown
as indexed on the web, and invisible to every MCP token, its owner's included: a
share of it answered its holder "no repositories you can read". Now:

  * the slug listings take an unpacked upload (the record `install_archive`
    writes for it) as a checkout, and still skip a directory that is neither;
  * the idx line names the archive: branch `upload`, its sha-256 as the
    revision, `fresh` while that archive is the one indexed, `STALE` once a newer
    one waits for its index;
  * `grep` and the readers take the unpacked files at that revision and nothing
    else: the secret-file floor holds, the archive's `.gitignore` is honoured, a
    path that leaves the tree is refused, and a revision the files no longer are
    has nothing to read;
  * a share of an uploaded repository reaches its holder.

World: `tests/security/mcp_world.py` + `share_world.py`, with the shared
repository uploaded instead of cloned.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.config import Settings
from tests.security.mcp_world import (
    FOREIGN,
    GRANTED,
    RULED,
    SHARED,
    SHARED_FILES,
    SHARED_MARKER,
    WILD,
    WS_B,
    set_env,
)
from tests.security.share_world import holder_token, make_share

ARCHIVE = "5e" * 32
NEWER = "a7" * 32
#: What the archive holds beside the shared files: a vendored copy of the marker
#: that its own `.gitignore` names, which a clone would not have.
IGNORED = {".gitignore": "vendor/\n", "vendor/lib.py": f"# {SHARED_MARKER} vendored\n"}


def _write_meta(settings: Settings, slug: str, sha: str) -> None:
    data = settings.repo_data_path(slug)
    data.mkdir(parents=True, exist_ok=True)
    (data / "upload.json").write_text(
        json.dumps({"sha256": sha, "size": 1, "filename": "code.zip"}), encoding="utf-8")


@pytest.fixture(scope="module")
def mcp_graphs(tmp_path_factory):
    """`mcp_world`'s repositories, SHARED unpacked from an archive: no `.git`."""
    from tests.security.test_mcp_graph_obeys_access_rules_single_tenant import _build

    root = tmp_path_factory.mktemp("mcp-upload-world")
    settings = Settings(workspace_dir=root)
    for slug in (GRANTED, RULED, WILD, FOREIGN, SHARED):
        _build(settings, slug)
        (settings.repos_dir / slug / ".git").mkdir(parents=True, exist_ok=True)
    tree = settings.repos_dir / SHARED
    shutil.rmtree(tree / ".git")
    for rel, text in {**SHARED_FILES, **IGNORED}.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, encoding="utf-8")
    _write_meta(settings, SHARED, ARCHIVE)
    return root


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def world(request, mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    mcp_world.mode = request.param
    return mcp_world


@pytest.fixture(autouse=True)
def _no_call_in_flight():
    from src.mcp_server import callctx

    callctx.reset()
    yield
    callctx.reset()


def _call(world, bearer: str, tool: str = "grep"):
    from src.mcp_server import callctx

    world.as_(bearer)
    return callctx.begin(tool)


def _holder(world) -> str:
    return holder_token(world, [make_share(world)], own=[WILD], who="owner")[0]


def _indexed(monkeypatch, sha: str) -> None:
    """The index state records ``sha`` as the revision SHARED was indexed at."""
    from src.repos import index_state

    info = SimpleNamespace(
        last_indexed_sha=sha, indexed_branch=None, up_to_date=None,
        last_checked_at=None, last_indexed_at=datetime.now(UTC))
    monkeypatch.setattr(index_state, "read_index_states",
                        lambda slugs: {s: info for s in slugs if s == SHARED})


# ─── listings ────────────────────────────────────────────────────────


def test_the_slug_listings_take_an_unpacked_upload_and_skip_a_bare_directory(world):
    from src.config import get_settings
    from src.mcp_server import tools
    from src.mcp_server.dev_profile.access import indexed_slugs

    bare = get_settings().repos_dir / "github_nobody-bare"
    bare.mkdir()
    try:
        assert SHARED in tools.list_repo_slugs()
        assert SHARED in indexed_slugs()
        assert "github_nobody-bare" not in tools.list_repo_slugs()
    finally:
        bare.rmdir()


def test_a_share_of_an_uploaded_repository_reaches_its_holder(world):
    from src.mcp_server.dev_profile import tools_repos

    _call(world, _holder(world), "repos")
    out = tools_repos.run()
    assert "no repositories you can read" not in out
    line = next(ln for ln in out.splitlines() if ln.startswith(SHARED))
    assert line.endswith(" code shared")


def test_the_owner_lists_its_own_uploaded_repository(world):
    from src.mcp_server.dev_profile import tools_repos

    token = world.issue("outsider", [SHARED], kind="pat", profile="dev", workspace=WS_B)[0]
    _call(world, token, "repos")
    lines = tools_repos.run().splitlines()
    assert any(ln.startswith(SHARED) and ln.endswith(" code") for ln in lines)


# ─── the idx line ────────────────────────────────────────────────────


def test_the_idx_line_names_the_indexed_archive_as_fresh(world, monkeypatch):
    from src.mcp_server.dev_profile.freshness import read_freshness

    _indexed(monkeypatch, ARCHIVE)
    entry = read_freshness([SHARED])[SHARED]
    assert (entry.branch, entry.sha, entry.state) == ("upload", ARCHIVE, "fresh")
    assert entry.entry().startswith(f"{SHARED} upload@{ARCHIVE[:8]} ")


def test_a_newer_archive_waiting_for_its_index_is_stale_and_unread(world, monkeypatch):
    from src.config import get_settings
    from src.mcp_server.dev_profile import git_io
    from src.mcp_server.dev_profile.freshness import read_freshness

    _indexed(monkeypatch, ARCHIVE)
    _write_meta(get_settings(), SHARED, NEWER)
    try:
        assert read_freshness([SHARED])[SHARED].state == "STALE"
        # The files on disk are the newer archive: the indexed revision is gone.
        assert not git_io.has_commit(SHARED, ARCHIVE)
        assert git_io.show_file(SHARED, ARCHIVE, "src/app.py") is None
        assert git_io.grep(SHARED, ARCHIVE, SHARED_MARKER) == []
    finally:
        _write_meta(get_settings(), SHARED, ARCHIVE)


def test_without_an_index_record_the_archive_is_named_unknown(world):
    from src.mcp_server.dev_profile.freshness import read_freshness

    entry = read_freshness([SHARED])[SHARED]
    assert (entry.branch, entry.sha, entry.state) == ("upload", ARCHIVE, "unknown")


# ─── reading ─────────────────────────────────────────────────────────


def test_grep_and_the_readers_take_the_unpacked_files(world):
    from src.mcp_server.dev_profile import tools_grep, tools_read

    _call(world, _holder(world))
    found = tools_grep.run(SHARED_MARKER, repo=SHARED)
    assert "src/app.py" in found
    assert "def handler" in tools_read.read_symbol(SHARED, "handler")


def test_the_secret_floor_and_the_archive_gitignore_hold(world):
    from src.mcp_server.dev_profile import git_io, tools_grep

    _call(world, _holder(world))
    assert "vendor/lib.py" not in tools_grep.run(SHARED_MARKER, repo=SHARED)
    assert "PLACEHOLDER_SETTING" not in tools_grep.run("PLACEHOLDER_SETTING", repo=SHARED)
    assert all(h.path != ".env" for h in git_io.grep(SHARED, ARCHIVE, "PLACEHOLDER"))


def test_a_path_out_of_the_unpacked_tree_is_refused(world):
    from src.config import get_settings
    from src.mcp_server.dev_profile import git_io

    assert git_io.show_file(SHARED, ARCHIVE, "src/util.py")[0] == "def helper():"
    assert git_io.show_file(SHARED, ARCHIVE, f"../{GRANTED}/x") is None
    assert git_io.show_file(SHARED, ARCHIVE, "src") is None
    outside = get_settings().workspace_dir / "outside.txt"
    outside.write_text(f"{SHARED_MARKER}\n", encoding="utf-8")
    link = get_settings().repos_dir / SHARED / "src" / "link.py"
    link.symlink_to(outside)
    try:
        assert git_io.show_file(SHARED, ARCHIVE, "src/link.py") is None
        assert all(h.path != "src/link.py" for h in git_io.grep(SHARED, ARCHIVE, SHARED_MARKER))
    finally:
        link.unlink()
        outside.unlink()


def test_a_sha_that_is_not_the_archive_reads_nothing(world):
    from src.mcp_server.dev_profile import git_io

    assert git_io.has_commit(SHARED, ARCHIVE)
    assert not git_io.has_commit(SHARED, NEWER)
    assert git_io.show_file(SHARED, NEWER, "src/app.py") is None
    assert git_io.grep(SHARED, NEWER, SHARED_MARKER) == []
