"""There is a CI workflow, and it runs the suite.

Until today this repository had one workflow — deploy.yml — with zero
mentions of pytest. 2908 tests ran on one laptop and nowhere else, which
means they ran until the day that laptop was busy, and a suite nobody is
forced to run is a suite that quietly rots.

These are cheap structural checks. They cannot tell you the build is green;
they can tell you the build exists, runs the tests, and has not silently had
its teeth pulled — which is how CI usually dies: not deleted, just made
advisory one step at a time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert CI.exists(), "there is no CI workflow"
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def test_it_runs_on_pull_requests_and_on_main(workflow):
    """A workflow that only runs on main tells you about a mistake after it
    has been merged."""
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" in triggers
    assert "main" in triggers["push"]["branches"]


def test_the_suite_actually_runs(workflow):
    runs = " ".join(s.get("run", "") for s in _steps(workflow, "python"))
    assert "pytest" in runs, "the CI job does not run the tests"


def test_the_test_step_is_not_advisory(workflow):
    """`continue-on-error` on the test step is how a suite stops mattering
    without anybody deciding that it should."""
    for step in _steps(workflow, "python"):
        if "pytest" in step.get("run", ""):
            assert not step.get("continue-on-error"), (
                "the tests cannot fail the build, so they are decoration"
            )
            return
    pytest.fail("no pytest step")


def test_the_web_build_is_checked_too(workflow):
    """26k lines of TypeScript. A backend-only CI is half a CI."""
    runs = " ".join(s.get("run", "") for s in _steps(workflow, "web"))
    assert "tsc --noEmit" in runs
    assert "npm run build" in runs


def test_the_lint_ratchet_exists_and_has_a_baseline():
    """Lint is enforced as "must not get worse" rather than "must be clean",
    because the repository carries findings that predate CI and a build that
    is red on day one is a build everybody learns to ignore."""
    assert (ROOT / "scripts" / "lint_ratchet.py").exists()
    baseline = ROOT / ".ruff-baseline"
    assert baseline.exists(), "the ratchet has nothing to ratchet against"
    assert baseline.read_text().strip().isdigit()


def test_the_linter_was_told_which_framework_it_is_reading():
    """B008 — a call in a default argument — is a real bug in ordinary Python
    and the required idiom in FastAPI. It was a third of the findings, and an
    unconfigured linter whose report is a third noise is a report nobody
    reads."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "extend-immutable-calls" in pyproject
    assert "Depends" in pyproject


def test_deploy_is_not_where_tests_live(workflow):
    """Correctness and deployment are different questions, and a deploy that
    waits ten minutes for a suite is a deploy people learn to skip."""
    deploy = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"))
    runs = " ".join(
        s.get("run", "") for s in deploy["jobs"]["deploy"]["steps"])
    assert "pytest" not in runs
