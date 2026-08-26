"""SCIP indexer subprocess wrapper.

Runs external SCIP indexer binaries (`scip-python`, `scip-typescript`, etc.)
as a subprocess + collects index.scip output, converts to JSON via
`scip convert`.

Graceful failure if the binaries are not installed — lets the pipeline
continue with tree-sitter only enrichment.

Required tooling per language (as of May 2026):
    Python:     npm install -g @sourcegraph/scip-python
    TypeScript: npm install -g @sourcegraph/scip-typescript
    Java:       coursier launch com.sourcegraph:scip-java...
    Go:         go install github.com/sourcegraph/scip-go/cmd/scip-go@latest
    SCIP CLI:   go install github.com/sourcegraph/scip/cmd/scip@latest
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.indexing.scip.reader import ScipIndex, ScipReader

logger = logging.getLogger(__name__)


class ScipRunnerError(RuntimeError):
    """Raised when the scip indexer subprocess fails OR the binary is missing."""


@dataclass
class ScipRunResult:
    """Outcome of single SCIP indexer subprocess run."""

    binary: str
    repo_path: Path
    output_path: Path  # path to the index.scip file
    index: ScipIndex | None  # None if conversion failed


class ScipExternalRunner:
    """Run SCIP indexer subprocess + convert binary output to JSON.

    Sequence:
        1. Verify the indexer binary is available (shutil.which)
        2. Run the indexer in repo_path → emits index.scip (binary protobuf)
        3. Run `scip convert --from binary --to json` → JSON file
        4. Parse JSON → ScipIndex

    If any step fails with a missing binary — raise ScipRunnerError, which the
    caller can catch gracefully.
    """

    INDEXER_COMMANDS: dict[str, list[str]] = {
        # Map language → CLI invocation. Every indexer emits index.scip
        # into the CWD by default.
        "python": ["scip-python", "index"],
        "typescript": ["scip-typescript", "index"],
        "go": ["scip-go"],
        "java": ["scip-java", "index"],
    }

    def __init__(
        self,
        scip_cli: str = "scip",
        timeout_seconds: int = 600,
    ) -> None:
        self.scip_cli = scip_cli
        self.timeout = timeout_seconds

    def is_indexer_available(self, language: str) -> bool:
        """Check whether the binary is available in PATH."""
        cmd = self.INDEXER_COMMANDS.get(language)
        if not cmd:
            return False
        return shutil.which(cmd[0]) is not None

    def is_scip_cli_available(self) -> bool:
        """The `scip` CLI tool is required for convert."""
        return shutil.which(self.scip_cli) is not None

    def run(
        self,
        language: str,
        repo_path: Path,
        *,
        output_dir: Path | None = None,
    ) -> ScipRunResult:
        """Execute the SCIP indexer + convert to JSON.

        Args:
            language: 'python' | 'typescript' | 'go' | 'java' | ...
            repo_path: cloned repo root (CWD for the indexer subprocess)
            output_dir: where to store the outputs. Default — repo_path.

        Returns:
            ScipRunResult with a parsed ScipIndex (None if conversion failed
            but the indexer succeeded — there is a binary index.scip but the
            JSON conversion failed).

        Raises:
            ScipRunnerError if the indexer binary is missing or the subprocess
            returns a non-zero exit code.
        """
        cmd = self.INDEXER_COMMANDS.get(language)
        if not cmd:
            raise ScipRunnerError(f"No SCIP indexer configured for '{language}'")

        if not self.is_indexer_available(language):
            raise ScipRunnerError(
                f"SCIP indexer '{cmd[0]}' not found in PATH. "
                f"Install: see src/indexing/scip/runner.py docstring."
            )

        out_dir = output_dir or repo_path
        out_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: run indexer (writes index.scip into CWD)
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScipRunnerError(
                f"SCIP indexer '{cmd[0]}' timeout after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise ScipRunnerError(f"Failed to spawn '{cmd[0]}': {exc}") from exc

        if result.returncode != 0:
            raise ScipRunnerError(
                f"SCIP indexer '{cmd[0]}' exited {result.returncode}: "
                f"{result.stderr[:500]}"
            )

        scip_binary = repo_path / "index.scip"
        if not scip_binary.exists():
            raise ScipRunnerError(
                f"SCIP indexer succeeded but output index.scip not found at {scip_binary}"
            )

        # Step 2: convert to JSON via `scip convert`
        json_path = out_dir / f"index_{language}.json"
        index: ScipIndex | None = None

        if self.is_scip_cli_available():
            try:
                conv = subprocess.run(
                    [self.scip_cli, "convert", "--from", str(scip_binary), "--to", "json"],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                if conv.returncode == 0 and conv.stdout:
                    json_path.write_bytes(conv.stdout)
                    index = ScipReader().read_bytes(conv.stdout)
                else:
                    logger.warning(
                        "scip_convert_failed rc=%d stderr=%s",
                        conv.returncode, conv.stderr[:200].decode(errors="replace"),
                    )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("scip_convert_error %s", exc)
        else:
            logger.info(
                "scip_cli_unavailable — skipping JSON conversion. "
                "Install: go install github.com/sourcegraph/scip/cmd/scip@latest"
            )

        return ScipRunResult(
            binary=cmd[0],
            repo_path=repo_path,
            output_path=scip_binary,
            index=index,
        )

    def try_run(
        self,
        language: str,
        repo_path: Path,
        *,
        output_dir: Path | None = None,
    ) -> ScipRunResult | None:
        """Same as run() but returns None instead of raising on a missing binary.

        Useful when the pipeline wants to gracefully degrade to tree-sitter only.
        """
        try:
            return self.run(language, repo_path, output_dir=output_dir)
        except ScipRunnerError as exc:
            logger.info("scip_run_skipped lang=%s reason=%s", language, exc)
            return None
