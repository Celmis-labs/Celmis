"""A workflow must not cancel the run that publishes what it needs.

`deploy.yml` had `concurrency: group: deploy` with `cancel-in-progress: true` —
one bucket keyed on nothing, so any run of it cancelled any other. Production
sat four commits behind for over an hour, and the visible symptom was a red run
that looked like it wanted a retry.

THE DEPLOY WORKFLOW IS GONE. It held a root key to the production server in the
secrets of what is now a public repository, and once images came from a
registry the server could fetch its own updates instead. Most of what this file
asserted went with it: a `workflow_run` trigger, a `conclusion == success`
guard, an `if` that refused pull requests. Those were facts about GitHub's
scheduling, and re-pointing them at `scripts/deploy-on-server.sh` would have
produced assertions about a race that cannot happen on a machine somebody runs
a script on.

What survives is the lesson, and it has a live subject: `release.yml` builds
and publishes three images, and a half-published release is worse than a slow
one. Its concurrency has to be the OPPOSITE of what the deploy's was — never
cancel — and that is worth pinning precisely because the reflex when adding a
concurrency block is to copy `cancel-in-progress: true` from the file next door.

The secrets check that used to live here moved with the deploy: the script
refuses to start without a `.env`, pinned in
tests/security/test_sandbox_deploy_path.py.
"""

from __future__ import annotations

import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows"


def _load(name: str) -> dict:
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_a_half_published_release_is_never_cancelled():
    """Three images, published one by one. Cancelling midway leaves a tag whose
    manifest resolves for some architectures and not others — which fails on
    the machine that pulls it, long after the release looked finished."""
    conc = _load("release.yml")["concurrency"]

    assert conc["cancel-in-progress"] is False


def test_the_release_group_is_keyed_on_the_ref():
    """A constant group name makes every run cancel every other — which is
    exactly how the deploy came to cancel itself."""
    group = _load("release.yml")["concurrency"]["group"]

    assert "${{" in group and "github.ref" in group


def test_ci_may_still_cancel_itself():
    """The opposite is right for CI: the answer to "is main green" is about the
    newest commit, and paying twice for one question is how minutes disappear.
    Same mechanism, opposite setting, and the difference is whether the run
    produces an ARTIFACT or an ANSWER."""
    conc = _load("ci.yml")["concurrency"]

    assert conc["cancel-in-progress"] is True


def test_ci_still_runs_on_pull_requests():
    """CI is the workflow that should see a pull request. Removing the deploy
    must not take that away."""
    assert "pull_request" in set(_load("ci.yml")["on"])


def test_no_workflow_holds_a_key_to_production():
    """The reason the deploy left. A public repository's secrets are one
    mistaken action update away from being somebody else's root shell."""
    for wf in WF.glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for secret in ("SSH_KEY", "SSH_HOST", "DEPLOY_ENV"):
            assert secret not in text, f"{wf.name} still wants {secret}"
