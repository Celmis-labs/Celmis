"""Smoke tests: clone реальних OSS repos з GitHub без auth.

Покриває:
    - Multi-host detection (всі — GitHub)
    - Anonymous public clone (без credentials)
    - Branch fallback (якщо CLI default 'dev' не існує)
    - Per-language reference repos для подальшого Phase 2 (extractors)

Run:
    .venv/bin/pytest -m integration tests/integration/

Skip за замовчуванням бо потребує мережу + git binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.sync.clone import RepoSync
from src.sync.git_providers import GitProvider, parse_repo_url

# Reference OSS repos для кожної мови — використовуються:
#   1. Smoke test multi-host clone (Stage 1.7)
#   2. Integration test для extractors (Stage 3.1)
#
# Critеrii: public, manageable size, mainstream library, modern syntax.
OSS_REPOS = [
    pytest.param(
        "https://github.com/pallets/click",
        "click",  # expected name
        "python",  # primary language
        id="python_click",
    ),
    pytest.param(
        "https://github.com/spf13/cobra",
        "cobra",
        "go",
        id="go_cobra",
    ),
    pytest.param(
        "https://github.com/nikic/PHP-Parser",
        "PHP-Parser",
        "php",
        id="php_parser",
    ),
    pytest.param(
        "https://github.com/square/okhttp",
        "okhttp",
        "java",
        id="java_okhttp",
    ),
    pytest.param(
        "https://github.com/JamesNK/Newtonsoft.Json",
        "Newtonsoft.Json",
        "csharp",
        id="csharp_newtonsoftjson",
    ),
    pytest.param(
        "https://github.com/nlohmann/json",
        "json",
        "cpp",
        id="cpp_nlohmann_json",
    ),
]


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """Тимчасова workspace для smoke clone — не торкається ~/code-analysis."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Override settings.workspace_dir через env (pydantic-settings reads it)
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    # Force reload settings cache
    from src.config import get_settings
    get_settings.cache_clear()
    yield workspace
    get_settings.cache_clear()


# ─── Multi-host detection ───────────────────────────────────────────


@pytest.mark.parametrize("url,expected_name,_", OSS_REPOS)
def test_url_parses_to_github(url: str, expected_name: str, _: str) -> None:
    """Pure parsing — без мережі. Підтверджує що detection правильний."""
    repo = parse_repo_url(url)
    assert repo.provider == GitProvider.GITHUB
    assert repo.name == expected_name
    # Slug містить github_ prefix
    assert repo.slug.startswith("github_")
    assert expected_name.lower() in repo.slug.lower()


# ─── Real clone tests (require network + git) ──────────────────────


@pytest.mark.integration
@pytest.mark.parametrize("url,expected_name,language", OSS_REPOS)
def test_clone_oss_public_repo(
    url: str,
    expected_name: str,
    language: str,
    isolated_workspace: Path,
) -> None:
    """Анонімний clone public OSS repo — verify .git + commit_sha + content."""
    sync = RepoSync()

    # branch=None → default branch (main/master) — uniform across providers
    result = sync.clone_or_update(url, branch=None)

    # Basic asserts
    assert result.path.exists()
    assert (result.path / ".git").exists()
    assert result.commit_sha
    assert len(result.commit_sha) == 40  # SHA-1 hex
    assert result.changed is True  # fresh clone
    assert result.previous_sha is None
    assert result.provider == GitProvider.GITHUB

    # Slug у repo_path як очікується
    assert "github_" in result.path.name
    assert expected_name.lower() in result.path.name.lower()

    # Має існувати хоча б один файл коду — мінімальна sanity check
    has_files = any(result.path.rglob("*"))
    assert has_files, f"clone of {url} resulted in empty directory"


@pytest.mark.integration
def test_clone_with_default_branch_dev_falls_back(isolated_workspace: Path) -> None:
    """Default branch у CLI = 'dev', GitHub repo має 'main'.

    Очікуване behaviour: branch fallback retry + успішний clone default branch.
    """
    sync = RepoSync()
    # nlohmann/json — default 'develop', не 'dev'
    result = sync.clone_or_update("https://github.com/nlohmann/json", branch="dev")
    assert result.commit_sha
    assert (result.path / ".git").exists()


@pytest.mark.integration
def test_clone_with_browser_url_extracts_branch(isolated_workspace: Path) -> None:
    """Browser URL з /tree/branch/ — branch береться з URL."""
    sync = RepoSync()
    # Use valid branch для click repo
    result = sync.clone_or_update(
        "https://github.com/pallets/click/tree/main",
        branch="dev",  # CLI default — має ignored у favor of URL hint
    )
    assert result.commit_sha


@pytest.mark.integration
def test_clone_private_repo_without_auth_fails_clearly(
    isolated_workspace: Path,
) -> None:
    """Private repo без credentials — clear error, не висить.

    Використовуємо guaranteed-private fictitious repo path.
    """
    from src.sync.clone import CloneError

    sync = RepoSync()
    with pytest.raises(CloneError) as exc_info:
        sync.clone_or_update(
            "https://github.com/this-org-does-not-exist-9999/private-repo",
            branch=None,
        )
    # Error message має бути informative
    err_msg = str(exc_info.value).lower()
    assert "private" in err_msg or "not found" in err_msg
