"""Tests для ScipExternalRunner — graceful failure коли binaries missing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.indexing.scip.runner import ScipExternalRunner, ScipRunnerError


@pytest.fixture
def runner() -> ScipExternalRunner:
    return ScipExternalRunner()


# ─── Binary availability ───────────────────────────────────────────


class TestBinaryAvailability:
    def test_no_indexer_for_unknown_lang(self, runner: ScipExternalRunner) -> None:
        assert runner.is_indexer_available("brainfuck") is False

    def test_indexer_check_uses_which(
        self, runner: ScipExternalRunner,
    ) -> None:
        with patch("shutil.which") as which:
            which.return_value = "/usr/bin/scip-python"
            assert runner.is_indexer_available("python") is True
            which.assert_called_with("scip-python")

    def test_indexer_check_returns_false_when_missing(
        self, runner: ScipExternalRunner,
    ) -> None:
        with patch("shutil.which", return_value=None):
            assert runner.is_indexer_available("python") is False

    def test_scip_cli_check(self, runner: ScipExternalRunner) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/scip"):
            assert runner.is_scip_cli_available() is True
        with patch("shutil.which", return_value=None):
            assert runner.is_scip_cli_available() is False


# ─── Run failures ──────────────────────────────────────────────────


class TestRunFailures:
    def test_unknown_language_raises(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        with pytest.raises(ScipRunnerError, match="No SCIP indexer"):
            runner.run("brainfuck", tmp_path)

    def test_missing_binary_raises(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(ScipRunnerError, match="not found in PATH"),
        ):
            runner.run("python", tmp_path)

    def test_subprocess_nonzero_exit_raises(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        with patch("shutil.which", return_value="/fake/scip-python"):
            mock_result = MagicMock(returncode=1, stderr="error: missing pyright")
            with (
                patch("subprocess.run", return_value=mock_result),
                pytest.raises(ScipRunnerError, match="exited 1"),
            ):
                runner.run("python", tmp_path)

    def test_no_index_scip_output_raises(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        """Subprocess succeeded але index.scip not created → error."""
        with patch("shutil.which", return_value="/fake/scip-python"):
            mock_result = MagicMock(returncode=0, stderr="")
            with (
                patch("subprocess.run", return_value=mock_result),
                pytest.raises(ScipRunnerError, match="not found"),
            ):
                runner.run("python", tmp_path)

    def test_timeout_raises(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        import subprocess as sp
        with patch("shutil.which", return_value="/fake/scip-python"), patch(
            "subprocess.run",
            side_effect=sp.TimeoutExpired("scip-python", 600),
        ), pytest.raises(ScipRunnerError, match="timeout"):
            runner.run("python", tmp_path)


# ─── try_run (graceful) ────────────────────────────────────────────


class TestTryRun:
    def test_returns_none_on_missing_binary(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        with patch("shutil.which", return_value=None):
            result = runner.try_run("python", tmp_path)
            assert result is None

    def test_returns_none_for_unknown_language(
        self, runner: ScipExternalRunner, tmp_path: Path,
    ) -> None:
        result = runner.try_run("brainfuck", tmp_path)
        assert result is None
