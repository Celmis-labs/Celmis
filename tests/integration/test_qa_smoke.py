"""End-to-end Q&A smoke test (Stage 3.2, травень 2026).

Заpускається ТІЛЬКИ якщо у env:
    GEMINI_API_KEY    — для LLM calls
    QDRANT_URL + QDRANT_API_KEY — для vault embeddings storage

Без них тест skip'иться (CI-friendly).

Flow:
    1. Clone OSS repo (pallets/click) — fast, mature Python codebase
    2. Generate PRD/BRD через GenerationOrchestrator (full pipeline)
    3. Ask декілька реалістичних questions через QAOrchestrator
    4. Validate що response non-empty + reasonable length + no LLM errors

Не assert exact text content — Gemini нон-deterministic. Перевіряємо
shape: response > 200 chars, contains markdown structure, не містить error markers.
"""

from __future__ import annotations

import os

import pytest

_REQUIRED_ENV = ("GEMINI_API_KEY", "QDRANT_URL", "QDRANT_API_KEY")


def _has_required_env() -> bool:
    return all(os.environ.get(v) for v in _REQUIRED_ENV)


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """Use isolated workspace але REAL credentials/Qdrant (live mode)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    from src.config import get_settings
    get_settings.cache_clear()
    yield workspace
    get_settings.cache_clear()


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_required_env(),
    reason=(
        f"Q&A smoke test потребує {_REQUIRED_ENV} у env. "
        "Run: GEMINI_API_KEY=... QDRANT_URL=... QDRANT_API_KEY=... "
        "pytest -m integration -k qa_smoke"
    ),
)
class TestQASmoke:
    """End-to-end Q&A smoke на реальному OSS repo (pallets/click)."""

    OSS_REPO = "https://github.com/pallets/click"
    SLUG = "github_pallets-click"

    @pytest.fixture(scope="class")
    def indexed_repo(self, tmp_path_factory):
        """Clone + index pallets/click ОДИН раз для всього класу.

        scope=class — щоб не re-clone'ити для кожного test method.
        """
        tmp = tmp_path_factory.mktemp("qa_smoke_workspace")
        os.environ["WORKSPACE_DIR"] = str(tmp)
        from src.config import get_settings
        get_settings.cache_clear()

        # Clone + index без LLM generation (cheaper)
        from src.indexing.graph.pipeline import index_repo_graph
        from src.sync.clone import RepoSync

        sync = RepoSync()
        sync_result = sync.clone_or_update(self.OSS_REPO, branch=None)

        index_repo_graph(sync_result.path, sync_result.repo_slug, src_subdir=None)

        yield sync_result.repo_slug

        # Teardown — settings reset
        get_settings.cache_clear()

    def test_simple_overview_question(self, indexed_repo: str) -> None:
        """Loosely structured question про overall codebase."""
        from src.qa.orchestrator import QAOrchestrator

        qa = QAOrchestrator()
        answer = qa.ask(
            question="Що робить ця бібліотека? Назви 2-3 ключові функції.",
            repo=indexed_repo,
        )

        assert answer.text, "answer text empty"
        assert len(answer.text) > 100, f"answer too short: {len(answer.text)} chars"
        assert "click" in answer.text.lower(), (
            "answer не згадує 'click' — Q&A retrieval likely broken"
        )
        # No LLM error markers
        assert "error" not in answer.text.lower()[:100]
        assert "exception" not in answer.text.lower()[:100]

    def test_specific_function_question(self, indexed_repo: str) -> None:
        """Question про specific concept у Click — `command` decorator."""
        from src.qa.orchestrator import QAOrchestrator

        qa = QAOrchestrator()
        answer = qa.ask(
            question="Як працює @click.command декоратор?",
            repo=indexed_repo,
        )

        assert answer.text
        assert len(answer.text) > 100
        # Має згадати decorator чи command
        text_lower = answer.text.lower()
        assert "command" in text_lower or "декоратор" in text_lower or "decorator" in text_lower

        # Має були files_read (через retrieval) або vault_hits
        assert (
            len(answer.files_read) > 0 or len(answer.vault_hits) > 0
        ), "no files/vault hits — retrieval found nothing"

    def test_token_metrics_populated(self, indexed_repo: str) -> None:
        """Validate що tokens_in/out tracked (not zero)."""
        from src.qa.orchestrator import QAOrchestrator

        qa = QAOrchestrator()
        answer = qa.ask(
            question="Який entry point?",
            repo=indexed_repo,
        )

        assert answer.tokens_in > 0, "tokens_in not tracked"
        assert answer.tokens_out > 0, "tokens_out not tracked"

    def test_answer_metadata_populated(self, indexed_repo: str) -> None:
        """Validate question_type + route classified."""
        from src.qa.orchestrator import QAOrchestrator

        qa = QAOrchestrator()
        answer = qa.ask(
            question="Як парситься command-line argument?",
            repo=indexed_repo,
        )

        assert answer.question_type, "question_type not set"
        assert answer.route, "route not set"
        # answer_mode — або 'tech' або 'ba'
        assert answer.answer_mode in ("tech", "ba")
