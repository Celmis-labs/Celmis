"""The job that runs the test suite has to be given the history it reads.

WHAT HAPPENED. Two tests in `tests/ci/test_the_repository_says_where_it_came_from.py`
read git — one recomputes PROVENANCE.md's commit counts at the tag the record
pins, the other proves no commit ever weakened that record and its guard
together. `actions/checkout` fetches ONE commit and NO tags unless told
otherwise. So in CI the first failed with "the record pins v0.1.23, which is
not a tag in this repository" — an accusation against a file that was
correct — and the second walked a log holding a single commit and passed on
nothing at all. Measured: on a `--depth 1` clone it reported PASSED; with the
clone's blindness declared it reports FAILED, which is what it always should
have said there.

Two states this repository keeps separating: wrong, and not checked. A shallow
clone is the second, and the workflow is where it is fixed.

Read from the YAML, not grepped. `fetch-depth: 0` appears in this docstring
and in ci.yml's own comment; a grep for the string would pass on the prose
explaining its absence, which is the mistake this file's neighbours were
written after making.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _runs_pytest(job: dict) -> bool:
    for step in job.get("steps", []):
        script = str(step.get("run", ""))
        if "pytest" in script.split("#")[0]:
            return True
    return False


def test_the_job_that_runs_the_suite_checks_out_the_whole_repository():
    jobs = {name: job for name, job in _jobs().items() if _runs_pytest(job)}
    assert jobs, "no job in ci.yml runs pytest — this guard is aimed at nothing"

    for name, job in jobs.items():
        checkouts = [s for s in job["steps"]
                     if str(s.get("uses", "")).startswith("actions/checkout")]
        assert checkouts, f"job {name!r} runs the suite without checking out"
        for step in checkouts:
            depth = (step.get("with") or {}).get("fetch-depth")
            assert depth == 0, (
                f"job {name!r} checks out with fetch-depth={depth!r}. The "
                f"default is one commit and no tags, and the suite holds two "
                f"tests that read history: one then blames PROVENANCE.md for "
                f"the clone's shortcoming, the other passes on an empty log. "
                f"0 means everything."
            )
