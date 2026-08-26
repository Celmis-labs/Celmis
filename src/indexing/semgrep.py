"""Semgrep SAST runner.

Invokes the semgrep CLI as a subprocess, stores the SARIF, parses findings.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class SemgrepFinding:
    """A single security finding."""

    rule_id: str
    severity: str  # ERROR | WARNING | INFO
    file: str  # relative to repo
    line: int
    end_line: int | None
    message: str
    category: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "end_line": self.end_line,
            "message": self.message,
            "category": self.category,
        }


@dataclass
class SemgrepReport:
    repo: str
    findings: list[SemgrepFinding] = field(default_factory=list)
    sarif_path: Path | None = None
    scan_duration_seconds: float = 0.0

    def filter_by_path(self, path_prefix: str) -> list[SemgrepFinding]:
        prefix = path_prefix.rstrip("/") + "/"
        return [
            f for f in self.findings
            if f.file == path_prefix or f.file.startswith(prefix)
        ]

    def count_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts


class SemgrepRunner:
    """Runs semgrep and parses the result."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def scan(self, repo_path: Path, output_dir: Path) -> SemgrepReport:
        """Scan the repo with every ruleset. Returns a SemgrepReport."""
        output_dir.mkdir(parents=True, exist_ok=True)
        sarif_path = output_dir / "findings.sarif"

        cmd: list[str] = [
            "semgrep",
            "--sarif",
            "--output",
            str(sarif_path),
            "--metrics=off",
            "--disable-version-check",
            "--quiet",
        ]
        for ruleset in self.settings.semgrep_rulesets:
            cmd.extend(["--config", ruleset])
        cmd.append(str(repo_path))

        logger.info("semgrep_scan repo=%s rules=%d", repo_path.name, len(self.settings.semgrep_rulesets))

        import time

        start = time.perf_counter()
        try:
            # Semgrep returns non-zero when there are findings — not an error
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.settings.semgrep_timeout,
                check=False,
            )
            if proc.returncode not in (0, 1):
                logger.error("semgrep_error rc=%d stderr=%s", proc.returncode, proc.stderr[:500])
        except subprocess.TimeoutExpired:
            logger.error("semgrep_timeout after %ds", self.settings.semgrep_timeout)
            raise
        duration = time.perf_counter() - start

        findings = self._parse_sarif(sarif_path, repo_path)
        report = SemgrepReport(
            repo=repo_path.name,
            findings=findings,
            sarif_path=sarif_path,
            scan_duration_seconds=duration,
        )
        logger.info(
            "semgrep_done repo=%s findings=%d duration=%.1fs",
            repo_path.name,
            len(findings),
            duration,
        )
        return report

    @staticmethod
    def _parse_sarif(sarif_path: Path, repo_path: Path) -> list[SemgrepFinding]:
        if not sarif_path.exists():
            return []
        data = json.loads(sarif_path.read_text(encoding="utf-8"))
        findings: list[SemgrepFinding] = []
        for run in data.get("runs", []):
            rules_index: dict[str, dict] = {}
            for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
                rules_index[rule.get("id", "")] = rule
            for result in run.get("results", []):
                rule_id = result.get("ruleId", "")
                severity = (result.get("level") or "WARNING").upper()
                message = (result.get("message") or {}).get("text", "")
                locations = result.get("locations", [])
                if not locations:
                    continue
                phys = locations[0].get("physicalLocation", {})
                artifact = phys.get("artifactLocation", {}).get("uri", "")
                region = phys.get("region", {})
                line = region.get("startLine", 0) or 0
                end_line = region.get("endLine")
                category = ""
                rule_def = rules_index.get(rule_id, {})
                props = rule_def.get("properties", {})
                if isinstance(props, dict):
                    tags = props.get("tags", [])
                    if isinstance(tags, list) and tags:
                        category = str(tags[0])
                findings.append(
                    SemgrepFinding(
                        rule_id=rule_id,
                        severity=severity,
                        file=_relative(artifact, repo_path),
                        line=line,
                        end_line=end_line,
                        message=message,
                        category=category,
                    )
                )
        return findings


def _relative(uri: str, repo_path: Path) -> str:
    """'file:///abs/path' or 'abs/path' → relative path."""
    if uri.startswith("file://"):
        uri = uri[len("file://") :]
    p = Path(uri)
    try:
        return str(p.relative_to(repo_path))
    except ValueError:
        return str(p)
