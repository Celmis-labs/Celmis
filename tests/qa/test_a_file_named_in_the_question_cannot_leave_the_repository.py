"""A file path the router lifts out of a question is joined to the clone only after a check.

THE DEFECT. `QAOrchestrator._resolve_file_paths` took `decision.files` (what the
question mentioned: `../../etc/passwd`, an absolute path, `.env`) and asked
`(repo_path / f).is_file()`. A hit was returned as a path to read, so the
existence of any file on the server was the answer to a guess, and the path went
on to the reader. The reader has its own guard, but a second door that checks
nothing is how the first one gets forgotten.

THE CONTRACT. A mention resolves to a file of the repository or to nothing: a
`..` or absolute path, a link that leaves the clone and a secret file read like a
file that is not there (they are not even looked up in the graph); an ordinary
relative path still resolves, and so does a bare file name, through the graph.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import Settings
from src.qa.orchestrator import QAOrchestrator


@pytest.fixture()
def world(tmp_path: Path):
    settings = Settings(workspace_dir=tmp_path / "ws")
    repo = settings.repos_dir / "acme-app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    (repo / ".env").write_text("K=not-a-real-key\n")
    outside = settings.repos_dir / "outside.txt"
    outside.write_text("beyond the clone\n")
    try:
        (repo / "src" / "link.txt").symlink_to(outside)
    except OSError:  # pragma: no cover
        pytest.skip("symlinks unavailable")
    queries: list[dict] = []

    class Store:
        def query(self, _cypher, params=None):
            queries.append(params)
            tail = (params or {}).get("tail", "")
            return [{"file": "src/app.py"}] if tail == "/app.py" else []

    orchestrator = object.__new__(QAOrchestrator)
    orchestrator.graph_ret = SimpleNamespace(_store_for=lambda _path: Store())
    return orchestrator, repo, queries


def test_a_file_of_the_repo_resolves_by_path_and_by_bare_name(world):
    orchestrator, repo, _queries = world
    assert orchestrator._resolve_file_paths(repo, ["src/app.py"]) == ["src/app.py"]
    assert orchestrator._resolve_file_paths(repo, ["app.py"]) == ["src/app.py"]
    assert orchestrator._resolve_file_paths(repo, ["src/nothing.py"]) == []


@pytest.mark.parametrize("mention", [
    "../outside.txt",
    "src/../../outside.txt",
    "../../../../../../../../etc/hosts",
    "/etc/hosts",
    "src/link.txt",          # a link out of the clone
    ".env",                  # a secret file that exists
    "src/../.env",
])
def test_a_mention_that_leaves_the_repo_or_names_a_secret_resolves_to_nothing(world, mention):
    orchestrator, repo, queries = world
    assert orchestrator._resolve_file_paths(repo, [mention]) == [], mention
    assert queries == [], "and it is not even looked up in the graph"
