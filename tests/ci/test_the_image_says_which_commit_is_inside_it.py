"""A build that checks out a tag must not label itself with the branch.

The release workflow checks out `inputs.tag || github.ref`.  On a
workflow_dispatch those differ: the checkout takes the tag, while
`github.sha` stays the head of the branch the run was started from.
v0.1.11 shipped the tag's tree carrying main's commit in
`org.opencontainers.image.revision`, and `scripts/deploy-on-server.sh`
reads that label back as CELMIS_GIT_SHA — so production displayed
`0.1.11+f2a2b39` while running `c2a5e48`, and the AGPL §13 footer offered
source at a tree that was not the one inside the image.

Keyed on the property: a job that can check out something other than the
dispatching ref has to take its revision from the checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
LABEL = "org.opencontainers.image.revision"


def _jobs(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}).get("jobs") or {}


def _checks_out_a_chosen_ref(job: dict) -> bool:
    """True when this job's checkout ref is not simply the dispatching one."""
    for step in job.get("steps") or []:
        uses = str(step.get("uses") or "")
        if not uses.startswith("actions/checkout"):
            continue
        ref = str((step.get("with") or {}).get("ref") or "")
        # `inputs.` is the only way this repo picks a different ref; anything
        # naming it can resolve to a commit github.sha does not name.
        if "inputs." in ref:
            return True
    return False


def _revision_values(job: dict) -> list[tuple[str, str]]:
    out = []
    for step in job.get("steps") or []:
        labels = str((step.get("with") or {}).get("labels") or "")
        for line in labels.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == LABEL:
                out.append((step.get("name") or step.get("uses") or "?", value.strip()))
    return out


@pytest.mark.parametrize(
    "workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name
)
def test_revision_names_the_tree_that_was_built(workflow: Path) -> None:
    for job_name, job in _jobs(workflow).items():
        if not _checks_out_a_chosen_ref(job):
            continue
        for step_name, value in _revision_values(job):
            where = f"{workflow.name} :: {job_name} :: {step_name}"
            assert "github.sha" not in value, (
                f"{where}: {LABEL} is {value!r}. This job checks out a ref of "
                f"its own, so github.sha can name a different commit than the "
                f"one built. Take the sha from the checkout instead."
            )
            assert value, f"{where}: {LABEL} is empty"


def test_the_scanner_sees_the_v0_1_11_mistake(tmp_path: Path) -> None:
    """Passing has to mean 'looked and found nothing', not 'found nothing to look at'."""
    bad = tmp_path / "w.yml"
    bad.write_text(
        "jobs:\n"
        "  images:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ inputs.tag || github.ref }}\n"
        "      - uses: docker/build-push-action@v6\n"
        "        with:\n"
        "          labels: |\n"
        "            org.opencontainers.image.revision=${{ github.sha }}\n"
    )
    with pytest.raises(AssertionError, match="github.sha"):
        test_revision_names_the_tree_that_was_built(bad)

    good = tmp_path / "ok.yml"
    good.write_text(
        "jobs:\n"
        "  images:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ inputs.tag || github.ref }}\n"
        "      - uses: docker/build-push-action@v6\n"
        "        with:\n"
        "          labels: |\n"
        "            org.opencontainers.image.revision=${{ steps.src.outputs.sha }}\n"
    )
    test_revision_names_the_tree_that_was_built(good)
