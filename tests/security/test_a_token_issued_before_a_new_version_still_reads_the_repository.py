"""A new archive version changes what an uploaded repository holds, not who may read it.

Tokens name a repository (by name or by pattern), shares name it by slug and
identity, and none of them is touched by an update: the slug, the registry row
and the grants stay. So a token minted BEFORE the update reads the NEW content
afterwards and finds nothing of the removed content; a share keeps reaching its
holder; and the idx line names the new version.

World: `mcp_world` + `share_world`, SHARED unpacked from an archive (no `.git`).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
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
    WILD,
    WS_B,
    set_env,
)
from tests.security.share_world import holder_token, make_share

OLD_MARK = "old-only-marker-b3a1"
NEW_MARK = "new-only-marker-77de"
FIRST = "5e" * 32


@pytest.fixture(scope="module")
def mcp_graphs(tmp_path_factory):
    from tests.security.test_mcp_graph_obeys_access_rules_single_tenant import _build

    root = tmp_path_factory.mktemp("mcp-upload-version-world")
    settings = Settings(workspace_dir=root)
    for slug in (GRANTED, RULED, WILD, FOREIGN, SHARED):
        _build(settings, slug)
        (settings.repos_dir / slug / ".git").mkdir(parents=True, exist_ok=True)
    tree = settings.repos_dir / SHARED
    shutil.rmtree(tree / ".git")
    for rel, text in {**SHARED_FILES, "src/gone.py": f"# {OLD_MARK}\ndef gone():\n    return 0\n"}.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, encoding="utf-8")
    data = settings.repo_data_path(SHARED)
    data.mkdir(parents=True, exist_ok=True)
    (data / "upload.json").write_text(
        json.dumps({"sha256": FIRST, "size": 1, "filename": "code.zip", "version": 1}),
        encoding="utf-8")
    return root


@pytest.fixture
def world(mcp_world, monkeypatch):
    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE="single_tenant")
    mcp_world.mode = "single_tenant"
    from src.mcp_server import callctx

    callctx.reset()
    yield mcp_world
    callctx.reset()


def _call(world, bearer: str, tool: str):
    from src.mcp_server import callctx

    world.as_(bearer)
    return callctx.begin(tool)


def _new_archive(tmp_path):
    files = {
        "src/app.py": f"# {NEW_MARK}\ndef handler():\n    return 2\n",
        "src/util.py": "def helper():\n    return 2\n",
    }
    path = tmp_path / "next.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(f"shop/{rel}", text)
    data = path.read_bytes()
    from src.repos.upload import StoredArchive

    return StoredArchive(path, hashlib.sha256(data).hexdigest(), len(data))


def test_tokens_and_a_share_issued_before_the_update_read_the_new_version(world, tmp_path):
    from src.mcp_server.dev_profile import tools_grep, tools_repos
    from src.repos.upload_versions import apply_new_version

    by_name = world.issue("outsider", [SHARED], kind="pat", profile="dev", workspace=WS_B)[0]
    by_pattern = world.issue("outsider", ["github_bco-*"], kind="pat", profile="dev",
                             workspace=WS_B)[0]
    shared = holder_token(world, [make_share(world)], own=[WILD], who="owner")[0]

    for bearer in (by_name, by_pattern, shared):
        _call(world, bearer, "grep")
        assert "src/gone.py" in tools_grep.run(OLD_MARK, repo=SHARED)

    stored = _new_archive(tmp_path)
    out = apply_new_version(stored, "next.zip", SHARED, uploaded_by="owner@acme.example")
    assert "src/gone.py" in out.removed_paths and out.version["version"] == 2

    for bearer in (by_name, by_pattern, shared):
        _call(world, bearer, "grep")
        assert "src/app.py" in tools_grep.run(NEW_MARK, repo=SHARED)
        assert "src/gone.py" not in tools_grep.run(OLD_MARK, repo=SHARED)

    _call(world, shared, "repos")
    listed = tools_repos.run()
    assert any(ln.startswith(SHARED) and ln.endswith(" code shared") for ln in listed.splitlines())
    # A token still does not reach what it never reached.
    _call(world, by_name, "grep")
    assert FOREIGN not in tools_repos.run()


def test_the_idx_line_names_the_new_version(world, tmp_path, monkeypatch):
    from src.mcp_server.dev_profile.freshness import read_freshness
    from src.repos import index_state
    from src.repos.upload import upload_revision

    sha = upload_revision(SHARED)
    info = SimpleNamespace(last_indexed_sha=sha, indexed_branch="v2", up_to_date=None,
                           last_checked_at=None, last_indexed_at=datetime.now(UTC))
    monkeypatch.setattr(index_state, "read_index_states",
                        lambda slugs: {s: info for s in slugs if s == SHARED})
    entry = read_freshness([SHARED])[SHARED]
    assert (entry.branch, entry.state) == ("v2", "fresh")
    assert entry.entry().startswith(f"{SHARED} v2@{sha[:8]} ")
