"""End-to-end CLI tests — Phase 11.

Використовуємо Typer's CliRunner. Gemini API mock'ається — тести не палять quota.
Real graph + real disk опційно (skip якщо repo не indexed).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli import app
from src.llm.client import LLMResult
from src.qa.orchestrator import QAAnswer

runner = CliRunner()

# Real-repo e2e тести опційні: slug локального клону береться з
# CELMIS_REAL_REPO, інакше вони скіпаються.
REAL_REPO = os.environ.get("CELMIS_REAL_REPO", "acme-frontend")


# ─── basic commands (no Gemini) ─────────────────────────────────────


def test_help_works():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Code Analysis System" in result.stdout


def test_subcommand_help():
    for cmd in ("init", "ask", "generate", "chat", "index", "graph-stats"):
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed"


# ─── ask command (Gemini mocked) ────────────────────────────────────


def test_ask_command_with_mocked_gemini():
    """analyzer ask повертає markdown з footer metadata."""
    fake_answer = QAAnswer(
        question="test",
        question_type="technical",
        route="A",
        text="# Test answer\n\nMocked synthesis.",
        vault_hits=[],
        files_read=["src/foo.ts", "src/bar.ts"],
        tokens_in=1000,
        tokens_out=500,
    )

    with patch("src.qa.orchestrator.QAOrchestrator") as mock_qa_cls:
        mock_qa = MagicMock()
        mock_qa.ask = MagicMock(return_value=fake_answer)
        mock_qa_cls.return_value = mock_qa

        result = runner.invoke(app, [
            "ask",
            "Як реалізована функція `useEstimate`?",
            "--repo", "acme-frontend",
        ])

    assert result.exit_code == 0
    # Footer метадані виводяться
    assert "type=technical" in result.stdout
    assert "route=A" in result.stdout
    assert "files=2" in result.stdout


def test_ask_raw_mode():
    """--raw виводить через as_markdown() (з vault context)."""
    fake_answer = QAAnswer(
        question="x", question_type="overview", route="C",
        text="content", vault_hits=[], files_read=[],
        tokens_in=0, tokens_out=0,
    )

    with patch("src.qa.orchestrator.QAOrchestrator") as mock_qa_cls:
        mock_qa = MagicMock()
        mock_qa.ask = MagicMock(return_value=fake_answer)
        mock_qa_cls.return_value = mock_qa

        result = runner.invoke(app, ["ask", "питання", "--repo", "x", "--raw"])

    assert result.exit_code == 0
    # У raw — as_markdown footer secition
    assert "Type:" in result.stdout or "type=" in result.stdout


# ─── index command (real graph якщо repo cloned) ────────────────────


@pytest.mark.skipif(
    not Path(f"~/code-analysis/repos/{REAL_REPO}").expanduser().exists(),
    reason=f"{REAL_REPO} not cloned",
)
def test_index_command_real_repo():
    """analyzer index на real repo — non-zero counts."""
    result = runner.invoke(app, ["index", REAL_REPO])
    assert result.exit_code == 0, f"stdout={result.stdout!r}"
    assert "Done in" in result.stdout or "✅" in result.stdout
    assert "symbols:" in result.stdout
    assert "edges:" in result.stdout


@pytest.mark.skipif(
    not Path(f"~/code-analysis/data/{REAL_REPO}/graph.fdblite").expanduser().exists(),
    reason=f"graph not built — run `analyzer index {REAL_REPO}` first",
)
def test_graph_stats_command():
    """analyzer graph-stats читає graph і виводить stats."""
    result = runner.invoke(app, ["graph-stats", REAL_REPO])
    assert result.exit_code == 0
    assert "Graph stats" in result.stdout
    assert "Symbols by kind" in result.stdout
    assert "Edges by kind" in result.stdout


def test_index_command_repo_not_cloned(tmp_path, monkeypatch):
    """analyzer index на не клонованому repo → exit 1 з підказкою."""
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)

    result = runner.invoke(app, ["index", "nonexistent-repo"])
    assert result.exit_code == 1
    assert "Repo not cloned" in result.stdout or "❌" in result.stdout


def test_graph_stats_no_graph(tmp_path, monkeypatch):
    """analyzer graph-stats на repo без graph → exit 1."""
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)

    result = runner.invoke(app, ["graph-stats", "nonexistent"])
    assert result.exit_code == 1


# ─── DoD: повний ask на real graph + mocked Gemini ─────────────────


@pytest.mark.skipif(
    not Path(f"~/code-analysis/data/{REAL_REPO}/graph.fdblite").expanduser().exists(),
    reason="graph not built",
)
def test_dod_ask_useEstimate_returns_chain():
    """DoD: ask 'Як реалізована `useEstimate`?' → у answer згадані ВСІ 3 ключові файли.

    Це повний e2e: real router → real Tier 2 (FalkorDBLite) → real Tier 3 (disk read)
    → mocked Gemini.generate (щоб не палити quota і мати детермінований response).
    """
    # LLMResult, не GenerationResult: synthesis тепер іде через
    # build_llm_client (`QAOrchestrator._generate`), а нативний
    # GeminiClient.generate видалено разом із останнім викликом.
    fake = LLMResult(
        text="# Mock answer\n\nSee files in metadata.",
        input_tokens=2000, output_tokens=300,
        finish_reason="STOP", model="gemini-3.1-pro-preview",
        cost_usd=None, cost_source="unknown", provider="google",
    )

    # Mock тільки synthesis — pipeline retrieval запускаємо real
    from src.qa.orchestrator import QAOrchestrator

    real_qa = QAOrchestrator()
    real_qa.vault_ret = MagicMock()
    real_qa.vault_ret.search = MagicMock(return_value=[])

    with (
        patch.object(real_qa, "_generate", return_value=fake),
        patch("src.qa.orchestrator.QAOrchestrator", return_value=real_qa),
    ):
        result = runner.invoke(app, [
            "ask",
            "Як реалізована функція `useEstimate`?",
            "--repo", REAL_REPO,
            "--raw",
        ])

    assert result.exit_code == 0, f"failed: {result.stdout!r}"
    # У raw mode виводиться повний as_markdown з files-read footer
    out = result.stdout
    expected_files = [
        "useEstimate.js",
        "OrdersController.ts",
        "PricingApi.js",
    ]
    found = [f for f in expected_files if f in out]
    assert len(found) >= 2, f"Only {found} of {expected_files} found in: {out[:1500]}"


# ─── review command — the verdict carries its own health ────────────
#
# The measured failure this section pins: a benchmark run in which every
# provider call died of ConnectError still printed "Verdict: COMMENT,
# findings: 0" and exited 0 — "could not check" dressed as "checked, clean".
# The exit codes below are documented in `analyzer review --help`: 2 for a
# FAILED run, 3 for a PARTIAL run whose only answers are deterministic,
# 0 for anything a human can actually use as a review.


def _review_run_result(agents_run: list[str], agents_failed: list[str]):
    """A real ReviewBatch (real `run_status`, real `compute_verdict`) wrapped
    the way the orchestrator returns it — the CLI's health logic is exercised
    against the shipped status arithmetic, not a stub of it."""
    from src.review.models import PullRequest, ReviewBatch
    from src.review.orchestrator import ReviewRunResult

    pr = PullRequest(
        provider="github", repo="acme/api", number=42, title="t",
        description="", author="dev", base_ref="main", base_sha="a" * 7,
        head_ref="fix", head_sha="b" * 7, state="open",
    )
    batch = ReviewBatch(pull_request=pr)
    batch.agents_run = list(agents_run)
    batch.agents_failed = list(agents_failed)
    batch.verdict = batch.compute_verdict()
    batch.mark_complete()
    return ReviewRunResult(batch=batch, posted=False, provider_response={})


def _invoke_review(run_result):
    with (
        patch("src.review.orchestrator.ReviewOrchestrator") as orch_cls,
        patch("src.review.providers.get_provider_for") as get_provider,
    ):
        orch_cls.return_value.review.return_value = run_result
        get_provider.return_value = MagicMock()
        # A wide console so Rich cannot wrap the banner mid-assertion.
        return runner.invoke(
            app, ["review", "github:acme/api#42"], env={"COLUMNS": "200"},
        )


def test_review_exits_2_when_every_agent_died():
    """All dispatched agents failed → run_status FAILED (and the verdict the
    batch computes for it is SKIPPED-because-agents-dead). Exit 0 here is the
    exact lie the benchmark run printed."""
    result = _invoke_review(_review_run_result(
        agents_run=[],
        agents_failed=["defect", "contract", "security", "cve"],
    ))

    assert result.exit_code == 2, result.stdout
    assert "FAILED — no agent produced a verdict" in result.stdout


def test_review_partial_with_an_llm_answer_keeps_exit_0_but_says_so():
    """One agent short is still a usable review — the product owner's line is
    non-zero only "коли жоден LLM-агент не відпрацював". The banner, not the
    exit code, is what carries the hole."""
    result = _invoke_review(_review_run_result(
        agents_run=["defect", "contract", "cve"],
        agents_failed=["security"],
    ))

    assert result.exit_code == 0, result.stdout
    assert "PARTIAL — 1 of 4" in result.stdout
    assert "security" in result.stdout


def test_review_exits_3_when_only_deterministic_checks_answered():
    """The ConnectError shape itself: every LLM agent dead, the deterministic
    stages (cve, breaking_change) still answered, so the run is PARTIAL — but
    no model read the diff, and exit 0 would dress that up as a review."""
    result = _invoke_review(_review_run_result(
        agents_run=["cve", "breaking_change"],
        agents_failed=["defect", "contract", "security"],
    ))

    assert result.exit_code == 3, result.stdout
    assert "PARTIAL — 3 of 5" in result.stdout
    assert "No LLM agent produced a result" in result.stdout


def test_review_complete_run_exits_0_with_no_health_banner():
    result = _invoke_review(_review_run_result(
        agents_run=["defect", "contract", "security", "cve"],
        agents_failed=[],
    ))

    assert result.exit_code == 0, result.stdout
    assert "PARTIAL" not in result.stdout
    assert "FAILED" not in result.stdout


def test_review_help_documents_the_exit_codes():
    """The codes are an interface: CI scripts branch on them, so the help is
    where they must be discoverable — 1 was already taken by "CLI error"."""
    result = runner.invoke(app, ["review", "--help"])

    assert result.exit_code == 0
    assert "Exit codes" in result.stdout
