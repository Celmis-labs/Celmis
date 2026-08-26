"""CLI smoke tests для group commands."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from src.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    import src.groups.manager as gm
    from src.config import get_settings
    get_settings.cache_clear()
    gm._default_manager = None  # reset singleton — settings cache change
    yield tmp_path
    get_settings.cache_clear()
    gm._default_manager = None


# ─── list / create / show / delete ──────────────────────────────────


class TestGroupCRUDCommands:
    def test_list_empty(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "list"])
        assert result.exit_code == 0
        assert "No groups defined" in result.stdout

    def test_create_then_list(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(
            app, ["group", "create", "acme-test", "--desc", "test group"],
        )
        assert result.exit_code == 0
        assert "created" in result.stdout

        list_result = runner.invoke(app, ["group", "list"])
        assert list_result.exit_code == 0
        assert "acme-test" in list_result.stdout
        assert "test group" in list_result.stdout

    def test_create_invalid_name(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "create", "bad name with spaces"])
        assert result.exit_code == 1
        assert "invalid" in result.stdout.lower()

    def test_create_duplicate(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "create", "g"])
        assert result.exit_code == 1
        assert "already exists" in result.stdout

    def test_show_not_found(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "show", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_delete_with_yes_flag(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "delete", "g", "--yes"])
        assert result.exit_code == 0
        assert "deleted" in result.stdout

    def test_delete_nonexistent(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "delete", "ghost", "--yes"])
        assert result.exit_code == 1


# ─── add / remove repos ─────────────────────────────────────────────


class TestGroupRepoCommands:
    def test_add_repo_to_group(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(
            app, ["group", "add", "g", "github:pallets/click"],
        )
        assert result.exit_code == 0
        assert "Added" in result.stdout

        # Verify через show
        show_result = runner.invoke(app, ["group", "show", "g"])
        assert "github:pallets/click" in show_result.stdout
        assert "github" in show_result.stdout  # provider info

    def test_add_to_nonexistent_group(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        result = runner.invoke(
            app, ["group", "add", "ghost", "github:foo/bar"],
        )
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_add_invalid_repo(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "add", "g", "invalid"])
        assert result.exit_code == 1
        assert "Invalid" in result.stdout

    def test_add_duplicate(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        runner.invoke(app, ["group", "add", "g", "github:foo/bar"])
        # Same repo via different URL form
        result = runner.invoke(
            app, ["group", "add", "g", "https://github.com/foo/bar"],
        )
        assert result.exit_code == 0
        assert "already" in result.stdout.lower()

    def test_remove_repo(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        runner.invoke(app, ["group", "add", "g", "github:foo/bar"])
        result = runner.invoke(app, ["group", "remove", "g", "github:foo/bar"])
        assert result.exit_code == 0
        assert "Removed" in result.stdout


# ─── index command ──────────────────────────────────────────────────


class TestGroupIndexCommand:
    def test_index_empty_group(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "index", "g"])
        assert result.exit_code == 1
        assert "empty" in result.stdout.lower()

    def test_index_nonexistent(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "index", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.stdout


class TestGroupGenerateCommand:
    def test_generate_empty_group(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "generate", "g"])
        assert result.exit_code == 1
        assert "empty" in result.stdout.lower()

    def test_generate_nonexistent(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "generate", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.stdout


class TestGroupAskCommand:
    def test_ask_nonexistent_group(self, runner: CliRunner, isolated_workspace) -> None:
        result = runner.invoke(app, ["group", "ask", "ghost", "what is X?"])
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_ask_empty_group(self, runner: CliRunner, isolated_workspace) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "ask", "g", "what is X?"])
        assert result.exit_code == 1
        assert "empty" in result.stdout.lower()

    def test_ask_no_indexed_repos(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        """Group has repos але жоден не indexed → fail з clear message."""
        runner.invoke(app, ["group", "create", "g"])
        runner.invoke(app, ["group", "add", "g", "github:foo/bar"])
        result = runner.invoke(app, ["group", "ask", "g", "what is X?"])
        assert result.exit_code == 1
        assert "No indexed repos" in result.stdout
        assert "analyzer group index" in result.stdout


class TestGroupCrossEdgesCommand:
    def test_cross_edges_nonexistent_group(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        result = runner.invoke(app, ["group", "cross-edges", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_cross_edges_no_materialization(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        runner.invoke(app, ["group", "create", "g"])
        result = runner.invoke(app, ["group", "cross-edges", "g"])
        assert result.exit_code == 1
        assert "No cross-repo edges" in result.stdout

    def test_cross_edges_with_data(
        self, runner: CliRunner, isolated_workspace,
    ) -> None:
        """Materialize edges → cross-edges command shows table."""
        import src.groups.manager as gm
        from src.groups.cross_repo import CrossRepoEdge
        from src.groups.indexer import GroupIndexer

        runner.invoke(app, ["group", "create", "g"])

        # Manually persist cross-repo edges
        gm._default_manager = None  # reset singleton щоб get fresh state
        from src.groups import get_group_manager
        group = get_group_manager().load("g")

        idx = GroupIndexer(group)
        idx._persist_cross_repo_edges([
            CrossRepoEdge(
                from_repo="repo-a",
                from_id="compose.yml::svc",
                to_repo="repo-b",
                to_id="Dockerfile::__module__",
                kind="REFERENCES_REPO",
                confidence="strong",
                rationale="",
            ),
        ])

        result = runner.invoke(app, ["group", "cross-edges", "g"])
        assert result.exit_code == 0
        # Rich може truncate "REFERENCES_REPO" → "REFERENCES_RE…" у narrow terminal,
        # тому перевіряємо лише prefix
        assert "REFERENCES_RE" in result.stdout
        assert "repo-a" in result.stdout
        assert "repo-b" in result.stdout
