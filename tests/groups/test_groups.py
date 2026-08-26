"""Tests для RepoGroup + GroupManager."""

from __future__ import annotations

import pytest

from src.groups.manager import (
    GroupManager,
    GroupNotFoundError,
    GroupValidationError,
)
from src.groups.models import RepoGroup
from src.sync.git_providers import GitProvider

# ─── RepoGroup model ────────────────────────────────────────────────


class TestRepoGroupModel:
    def test_create_with_name(self) -> None:
        g = RepoGroup(name="acme-platform", description="Test")
        assert g.name == "acme-platform"
        assert g.description == "Test"
        assert g.repos == []
        assert g.created_at != ""
        assert g.updated_at != ""

    def test_add_repo_unique(self) -> None:
        g = RepoGroup(name="g")
        assert g.add_repo("github:foo/bar") is True
        assert g.add_repo("https://github.com/foo/bar") is False  # duplicate
        assert len(g.repos) == 1

    def test_add_repo_different_providers(self) -> None:
        """Same path on різних providers → НЕ duplicate."""
        g = RepoGroup(name="g")
        g.add_repo("github:foo/bar")
        g.add_repo("bitbucket:foo/bar")
        assert len(g.repos) == 2

    def test_remove_repo(self) -> None:
        g = RepoGroup(name="g")
        g.add_repo("github:foo/bar")
        g.add_repo("github:baz/qux")
        # Remove via different URL form
        assert g.remove_repo("https://github.com/foo/bar") is True
        assert len(g.repos) == 1
        assert g.repos[0] == "github:baz/qux"

    def test_remove_nonexistent_returns_false(self) -> None:
        g = RepoGroup(name="g")
        g.add_repo("github:foo/bar")
        assert g.remove_repo("github:other/repo") is False

    def test_invalid_repo_raises(self) -> None:
        g = RepoGroup(name="g")
        with pytest.raises(ValueError):
            g.add_repo("just-one-segment")

    def test_parsed_repos(self) -> None:
        g = RepoGroup(name="g")
        g.add_repo("github:pallets/click")
        g.add_repo("https://gitlab.com/group/repo")
        parsed = g.parsed_repos()
        assert len(parsed) == 2
        providers = {p.provider for p in parsed}
        assert providers == {GitProvider.GITHUB, GitProvider.GITLAB}

    def test_serialize_round_trip(self) -> None:
        g = RepoGroup(
            name="g",
            description="test",
            repos=["github:foo/bar"],
        )
        d = g.to_dict()
        g2 = RepoGroup.from_dict(d)
        assert g2.name == g.name
        assert g2.description == g.description
        assert g2.repos == g.repos


# ─── GroupManager ────────────────────────────────────────────────────


@pytest.fixture
def manager(tmp_path, monkeypatch) -> GroupManager:
    """Isolated workspace per test."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from src.config import get_settings
    get_settings.cache_clear()
    yield GroupManager()
    get_settings.cache_clear()


class TestGroupManagerCRUD:
    def test_create_and_load(self, manager: GroupManager) -> None:
        g = manager.create("acme", description="Test")
        loaded = manager.load("acme")
        assert loaded.name == g.name
        assert loaded.description == g.description

    def test_create_duplicate_raises(self, manager: GroupManager) -> None:
        manager.create("acme")
        with pytest.raises(GroupValidationError, match="already exists"):
            manager.create("acme")

    def test_load_nonexistent_raises(self, manager: GroupManager) -> None:
        with pytest.raises(GroupNotFoundError):
            manager.load("ghost")

    def test_delete(self, manager: GroupManager) -> None:
        manager.create("g")
        assert manager.delete("g") is True
        assert manager.delete("g") is False  # already gone
        with pytest.raises(GroupNotFoundError):
            manager.load("g")

    def test_list(self, manager: GroupManager) -> None:
        assert manager.list() == []
        manager.create("alpha")
        manager.create("beta")
        manager.create("gamma")
        # Sorted alphabetically
        assert manager.list() == ["alpha", "beta", "gamma"]

    def test_invalid_name_raises(self, manager: GroupManager) -> None:
        with pytest.raises(GroupValidationError, match="invalid group name"):
            manager.create("has spaces")
        with pytest.raises(GroupValidationError):
            manager.create("../escape")
        with pytest.raises(GroupValidationError):
            manager.create("")

    def test_add_remove_repo_persists(self, manager: GroupManager) -> None:
        manager.create("g")
        assert manager.add_repo("g", "github:foo/bar") is True
        assert manager.add_repo("g", "github:foo/bar") is False  # duplicate

        # Reload — repo має зберегтися
        g2 = manager.load("g")
        assert g2.repos == ["github:foo/bar"]

        # Remove
        assert manager.remove_repo("g", "github:foo/bar") is True
        g3 = manager.load("g")
        assert g3.repos == []

    def test_save_load_round_trip_preserves_data(self, manager: GroupManager) -> None:
        manager.create("g", description="My cross-repo group")
        manager.add_repo("g", "github:pallets/click")
        manager.add_repo("g", "bitbucket:acme/frontend")
        manager.add_repo("g", "https://gitlab.com/group/sub/repo")

        loaded = manager.load("g")
        assert loaded.description == "My cross-repo group"
        assert len(loaded.repos) == 3
        # Parse all and check providers
        parsed = loaded.parsed_repos()
        providers = {p.provider for p in parsed}
        assert providers == {
            GitProvider.GITHUB,
            GitProvider.BITBUCKET,
            GitProvider.GITLAB,
        }


class TestGroupManagerFileFormat:
    def test_yaml_file_human_readable(self, manager: GroupManager, tmp_path) -> None:
        """Зберігаємо у YAML що easy-to-edit by hand."""
        manager.create("g", description="My group")
        manager.add_repo("g", "github:foo/bar")

        yaml_file = tmp_path / "groups" / "g.yaml"
        content = yaml_file.read_text()
        assert "name: g" in content
        assert "description: My group" in content
        assert "github:foo/bar" in content
