"""`last_full_rebuild_at` names a rebuild, and an incremental pass is not one.

`repo_index_state` carries two different times: `last_indexed_at` ("we walked
this repository at T") and `last_full_rebuild_at` ("the graph on disk was
built from scratch at T"). The second is the one an operator reads when the
question is "could this graph be missing a symbol a diff never touched?", and
an incremental pass — which only applies `git diff prior..HEAD` — cannot
answer yes to it.

Nothing pinned the distinction. Replacing `full_rebuild=result.get("mode") ==
"full"` in src/sync/incremental.py with a bare `True` left the whole suite
green while every five-minute incremental pass stamped a rebuild that never
happened, which is the same shape of defect as everything else in this wave:
a surface claiming more than the run did. This file drives the real
`run_index` over a real git checkout and asks the row.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.config import Settings
from src.db.models import RepoIndexState
from src.repos.index_state import read_index_state
from src.sync.incremental import run_index

SLUG = "github_Acme-Dev-todo-app"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=test@celmis.local", "-c", "user.name=Celmis Test",
         "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = sa.create_engine(f"sqlite:///{db}")
    RepoIndexState.__table__.create(engine)
    engine.dispose()

    settings = Settings(
        gemini_api_key="test-key",
        workspace_dir=tmp_path / "ws",
        vault_dir=tmp_path / "vault",
    )
    settings.ensure_directories()
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    monkeypatch.setattr("src.groups.manager.get_settings", lambda: settings)

    repo = settings.repo_path(SLUG)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    sha = _commit(repo, "a.py", "def a(): ...\n")

    def _fake_index_repo_graph(*, repo_path, repo_slug, src_subdir=None):
        return types.SimpleNamespace(files_processed=9, symbols=42, edges=7)

    monkeypatch.setattr(
        "src.indexing.graph.pipeline.index_repo_graph", _fake_index_repo_graph,
    )
    return types.SimpleNamespace(settings=settings, repo=repo, sha=sha)


def test_an_incremental_pass_leaves_the_rebuild_time_where_the_rebuild_put_it(
    world, monkeypatch,
):
    """The pass walked a diff, not the repository. `last_indexed_at` moves
    because we did look; `last_full_rebuild_at` must not, because the graph on
    disk is still the one the rebuild wrote."""
    run_index(SLUG, force_full=True)
    after_rebuild = read_index_state(SLUG)
    assert after_rebuild.last_full_rebuild_at == after_rebuild.last_indexed_at

    monkeypatch.setattr(
        "src.sync.incremental._run_incremental",
        lambda repo_slug, repo_path, prior_sha, head_sha: {
            "mode": "incremental", "files_touched": 1,
        },
    )
    new_sha = _commit(world.repo, "b.py", "def b(): ...\n")

    result = run_index(SLUG)

    assert result["mode"] == "incremental"
    after = read_index_state(SLUG)
    assert after.last_indexed_sha == new_sha
    assert after.last_indexed_at > after_rebuild.last_indexed_at, (
        "the pass did not record that it ran at all"
    )
    assert after.last_full_rebuild_at == after_rebuild.last_full_rebuild_at, (
        "an incremental pass claimed the graph had been rebuilt from scratch"
    )


def test_a_forced_rebuild_does_move_the_rebuild_time(world):
    """The other half: the distinction is only worth anything if a real
    rebuild still says so."""
    run_index(SLUG, force_full=True)
    first = read_index_state(SLUG)

    _commit(world.repo, "b.py", "def b(): ...\n")
    run_index(SLUG, force_full=True)
    second = read_index_state(SLUG)

    assert second.last_full_rebuild_at > first.last_full_rebuild_at
    assert second.last_full_rebuild_at == second.last_indexed_at
