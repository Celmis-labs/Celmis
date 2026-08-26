"""Integration test: run extractors на реальних OSS GitHub repos.

Перевіряє end-to-end:
    1. Clone OSS repo (через RepoSync)
    2. Build LanguageRegistry зі всіма enabled extractors
    3. Walk repo files, dispatch через registry
    4. Validate per-language parse success rate ≥85%
    5. Validate що symbols + edges витягуються (non-trivial counts)

Кожен test обмежує scope до перших N files per language щоб тримати runtime
розумним (~30-90s per repo). Production indexing — без обмеження.

Run: .venv/bin/pytest -m integration tests/integration/test_extractors_on_oss.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.indexing.graph.languages.factory import build_default_registry, walk_repo_files
from src.sync.clone import RepoSync

logger = logging.getLogger(__name__)


@dataclass
class _ExtractorStats:
    """Per-language статистика одного integration run."""

    language: str
    files_total: int
    files_parsed_ok: int
    files_parse_failed: int
    files_extract_exception: int
    symbols_total: int
    edges_total: int

    @property
    def success_rate(self) -> float:
        if self.files_total == 0:
            return 0.0
        return self.files_parsed_ok / self.files_total

    def __str__(self) -> str:
        return (
            f"{self.language}: {self.files_parsed_ok}/{self.files_total} files "
            f"({self.success_rate:.1%}), {self.symbols_total} symbols, "
            f"{self.edges_total} edges, {self.files_parse_failed} parse_errors, "
            f"{self.files_extract_exception} exceptions"
        )


# Per-language target language identifier (як зареєстровано у factory.py)
# + reference repo URL + max files cap (для CI runtime budget)
OSS_REPOS = [
    pytest.param(
        "https://github.com/pallets/click",
        "python",
        300,
        id="python_click",
    ),
    pytest.param(
        "https://github.com/spf13/cobra",
        "go",
        300,
        id="go_cobra",
    ),
    pytest.param(
        "https://github.com/nikic/PHP-Parser",
        "php",
        200,
        id="php_parser",
    ),
    pytest.param(
        "https://github.com/square/okhttp",
        "java",
        300,
        id="java_okhttp",
    ),
    pytest.param(
        "https://github.com/JamesNK/Newtonsoft.Json",
        "csharp",
        300,
        id="csharp_newtonsoftjson",
    ),
    pytest.param(
        "https://github.com/nlohmann/json",
        "cpp",
        100,
        id="cpp_nlohmann_json",
    ),
]


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    from src.config import get_settings
    get_settings.cache_clear()
    yield workspace
    get_settings.cache_clear()


def _run_extractor_on_repo(
    repo_path: Path,
    language: str,
    max_files: int,
) -> _ExtractorStats:
    """Walk repo + extract по language. Limited до max_files per run."""
    registry = build_default_registry(repo_root=repo_path, enabled={language})
    all_files = walk_repo_files(repo_path)

    # Filter тільки файли які match'ить registry — це і є "language files"
    matched_files = []
    for f in all_files:
        if registry.match(f) is not None:
            matched_files.append(f)
            if len(matched_files) >= max_files:
                break

    stats = _ExtractorStats(
        language=language,
        files_total=len(matched_files),
        files_parsed_ok=0,
        files_parse_failed=0,
        files_extract_exception=0,
        symbols_total=0,
        edges_total=0,
    )

    for f in matched_files:
        extractor = registry.match(f)
        if extractor is None:
            continue
        try:
            res = extractor.extract(f)
            if res.parse_errors:
                stats.files_parse_failed += 1
            else:
                stats.files_parsed_ok += 1
            stats.symbols_total += len(res.symbols)
            stats.edges_total += len(res.edges)
        except Exception as exc:  # noqa: BLE001
            stats.files_extract_exception += 1
            logger.warning("extract_exception lang=%s file=%s err=%s", language, f, exc)

    return stats


@pytest.mark.integration
@pytest.mark.parametrize("url,language,max_files", OSS_REPOS)
def test_extractor_on_oss_repo(
    url: str,
    language: str,
    max_files: int,
    isolated_workspace: Path,
) -> None:
    """End-to-end: clone real OSS repo + index через відповідний extractor."""
    sync = RepoSync()
    sync_result = sync.clone_or_update(url, branch=None)

    stats = _run_extractor_on_repo(sync_result.path, language, max_files)
    print(f"\n{stats}")

    # Acceptance: ≥85% parse success rate
    assert stats.files_total > 0, f"no {language} files discovered у {sync_result.path}"
    assert stats.files_extract_exception == 0, (
        f"extract exceptions у {stats.files_extract_exception} файлах "
        f"(не parse_errors — справжні Python exceptions у extractor)"
    )
    assert stats.success_rate >= 0.85, (
        f"{language} parse success rate {stats.success_rate:.1%} < 85% "
        f"(threshold). Stats: {stats}"
    )

    # Sanity: extractor дійсно витягнув щось
    assert stats.symbols_total > stats.files_total, (
        f"<1 symbol/file average — extractor можливо не працює. {stats}"
    )
    assert stats.edges_total > 0, f"no edges extracted. {stats}"


@pytest.mark.integration
def test_polyglot_repo_dispatches_correctly(isolated_workspace: Path) -> None:
    """Один repo з різними мовами → registry правильно dispatch'ить.

    Використовуємо click як test repo — він має .py файли + setup files.
    Перевіряємо що extractors не conflict'ять і всі мови рівнобіжно працюють.
    """
    sync = RepoSync()
    sync_result = sync.clone_or_update(
        "https://github.com/pallets/click", branch=None,
    )

    # Build registry зі ВСІМА extractors — щоб перевірити dispatch
    registry = build_default_registry(repo_root=sync_result.path)
    all_files = walk_repo_files(sync_result.path)

    by_extractor: dict[str, int] = {}
    for f in all_files[:500]:  # sample limit
        ext = registry.match(f)
        if ext is None:
            continue
        name = getattr(ext, "language", ext.__class__.__name__)
        by_extractor[name] = by_extractor.get(name, 0) + 1

    print(f"\nDispatched files: {by_extractor}")
    # Click — Python repo, тому переважно python
    assert "python" in by_extractor
    assert by_extractor["python"] >= 5  # min Python files
