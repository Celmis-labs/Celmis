"""Production sat four commits behind for over an hour, and nothing said why.

`deploy.yml` had `concurrency: group: deploy` with `cancel-in-progress: true`
— ONE bucket for every run of the workflow, keyed on nothing. So any run
cancelled any other. A run triggered by a `pull_request` event cancelled the
push-to-main deploy and then failed itself four seconds later, because
pull_request events do not carry repository secrets: `ssh-keyscan -H ""` exits
non-zero and takes the step with it, and its stderr went to /dev/null.

The visible symptom was a red run that looked like it wanted a retry. The
actual symptom was that `push` to main no longer deployed at all.

Three separate things had to be true for that to happen, so three are fixed:
the group is keyed per event and ref, the job refuses to run on a pull request
at all, and a missing secret now names itself instead of dying inside
ssh-keyscan.

That last one is the same law as everywhere else in this codebase: a failure
that produces no message produces a wrong diagnosis. Four silent seconds read
as a network blip for an hour.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows"


def _load(name: str) -> dict:
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    # PyYAML reads a bare `on:` key as the boolean True.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_the_deploy_group_is_keyed_on_something():
    group = _load("deploy.yml")["concurrency"]["group"]

    assert "${{" in group, (
        "a constant group name makes every run cancel every other run"
    )
    assert "github.ref" in group


def test_the_group_separates_events():
    """A pull_request run and a push run must not share a bucket: the first
    cannot deploy and would cancel the second."""
    group = _load("deploy.yml")["concurrency"]["group"]

    assert "github.event_name" in group


def test_a_newer_push_to_the_same_branch_still_supersedes():
    """The cancelling behaviour itself is wanted — the newest commit is the
    one that should be live. Only the key was wrong."""
    conc = _load("deploy.yml")["concurrency"]

    assert conc["cancel-in-progress"] is True


def test_deploy_never_runs_on_a_pull_request():
    """`on:` says push and dispatch, but a workflow file is evaluated from the
    PR's own branch, so a branch that adds a pull_request trigger gets to run
    this job. The guard is the half the guarded thing cannot edit."""
    job = _load("deploy.yml")["jobs"]["deploy"]

    assert "pull_request" in job.get("if", "")


def test_deploy_is_not_triggered_by_pull_request():
    triggers = set(_load("deploy.yml")["on"])

    assert "pull_request" not in triggers
    # `push` is gone on purpose: the deploy now waits for the release that
    # builds its images (see test_the_deploy_waits_for_the_release...). What
    # matters here is that neither trigger is one secrets never reach.
    assert triggers <= {"workflow_run", "workflow_dispatch", "push"}
    assert triggers, "the deploy can no longer be triggered at all"


def test_a_missing_secret_names_itself_before_anything_uses_it():
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [s.get("name", "") for s in steps]
    check = next((i for i, n in enumerate(names) if "secret" in n.lower()), None)

    assert check is not None, "nothing checks the secrets"
    ssh = next(i for i, n in enumerate(names) if "SSH" in n)
    assert check < ssh, "the check runs after the step it exists to explain"


@pytest.mark.parametrize("secret", ["SSH_HOST", "SSH_USER", "SSH_KEY", "DEPLOY_ENV"])
def test_every_secret_the_deploy_needs_is_checked(secret):
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    check = next(s for s in steps if "secret" in s.get("name", "").lower())

    assert secret in check["run"]
    assert secret in (check.get("env") or {})


def test_ci_may_still_run_on_pull_requests():
    """CI is the workflow that SHOULD see a pull request. Fixing deploy must
    not take that away."""
    assert "pull_request" in set(_load("ci.yml")["on"])
