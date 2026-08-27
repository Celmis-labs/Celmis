"""The production server was the worst available place to build, and it built.

Every push to main rebuilt three images over ssh on the box that runs them,
while a rented runner was billed for the ~17.5 minutes it spent watching. That
is what exhausted the account's included Actions minutes and stopped deployment
altogether — for over an hour the only symptom was a red run that looked like
it wanted a retry.

Measured on that box, all three assumptions behind the arrangement were wrong:

  * `api` alone took 485 seconds and 4.2GB of a filesystem with 4.2GB free;
  * an UNCHANGED rebuild cost the same 485 seconds — with the containerd image
    store there is no build cache to reuse;
  * `docker builder prune -af` reclaims 0B there, so the command that made the
    build slow (dropping the cache between images) was a no-op run three times
    a deploy.

Images are now built once per tag by release.yml and pulled. Measured on the
same box: 6 seconds, and the disk does not move.

The bash check below exists because writing this cost me an outage of my own:
the remote script is a single-quoted argument to ssh, and an apostrophe in a
COMMENT — "the account's minutes" — closed the quote and broke the whole step.
YAML validates such a file happily.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows"
ROOT = WF.parents[1]


def _load(name: str) -> dict:
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _runs(doc: dict) -> list[tuple[str, str]]:
    out = []
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("run"):
                out.append((step.get("name", "?"), step["run"]))
    return out


# ─── the deploy no longer builds ─────────────────────────────────────


def _executable(doc: dict) -> str:
    """Run blocks with the comments stripped.

    The first version of this searched the raw text, so the assertion was
    satisfied by a `#` line naming the command and the prohibition was dodged
    by one in a comment. Proved by mutation: delete the pull, mention it in a
    comment, suite green.
    """
    lines = []
    for _name, run in _runs(doc):
        for line in run.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)
    return "\n".join(lines)


def test_the_deploy_pulls_rather_than_builds():
    """The workflow this read is gone; the guarantee is not. What deploys must
    fetch images, never build them — on that box a build was measured at 485
    seconds and 4.2GB of a filesystem with 4.2GB free."""
    body = (ROOT / "scripts/deploy-on-server.sh").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )

    assert "compose pull" in body.lower(), "the deploy neither builds nor pulls"
    # Case-insensitive: deploy.yml spells compose both as `$COMPOSE` and as
    # `docker compose`, and a guard that only knew the first would have missed
    # a rebuild written in the second.
    assert "compose build" not in body.lower(), "the server is building again"


# `test_the_deploy_waits_for_the_release_that_makes_its_images` and
# `test_the_deploy_refuses_to_ship_a_failed_release` were deleted with the
# workflow, not with a guarantee. Both were about GitHub's own scheduling — a
# `workflow_run` trigger and a `conclusion == success` guard — and neither has
# a counterpart on a server that pulls when somebody asks it to. Re-pointing
# them at the script would have produced two assertions about a race that
# cannot happen there.
#
# What survives is the reason they existed: a deploy must not fetch images that
# are not published yet. On the server that is the pull failing loudly, which
# `set -euo pipefail` guarantees and `test_it_stops_on_the_first_error` pins in
# tests/ci/test_the_server_deploys_itself.py.


def test_the_tag_reaches_the_server_through_the_file_compose_reads():
    """Compose interpolates `CELMIS_TAG` from `.env` itself, so the tag needs no
    plumbing of its own and has no second place to disagree with the deploy."""
    body = (ROOT / "scripts/deploy-on-server.sh").read_text(encoding="utf-8")

    assert "CELMIS_TAG" in body
    assert ".env" in body


def test_compose_pins_images_and_builds_nothing():
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for name in ("api", "web", "sandbox"):
        svc = doc["services"][name]
        assert "build" not in svc, f"{name} still builds where it runs"
        assert "CELMIS_TAG" in svc["image"], f"{name} is not pinned to a tag"
        # THE OWNER IS NOT OURS TO HARD-CODE. This asserted the literal prefix
        # `ghcr.io/`, which also froze the account name into the file: a fork
        # publishes under its own owner, and `release.yml` already computes it
        # from `github.repository_owner`. A compose file that names one account
        # points at nothing the moment the project moves.
        assert "CELMIS_REGISTRY" in svc["image"], (
            f"{name} hard-codes a registry owner"
        )


def test_the_registry_default_is_written_down():
    """A variable with no documented default is a variable nobody sets."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "CELMIS_REGISTRY" in example
    assert "CELMIS_TAG" in example


def test_developers_can_still_build_locally():
    """Taking the build out of the product must not take it from the people
    working on it."""
    doc = yaml.safe_load((ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8"))

    assert set(doc["services"]) == {"api", "web", "sandbox"}
    for svc in doc["services"].values():
        assert "build" in svc


# ─── the release workflow ────────────────────────────────────────────


def test_release_builds_all_three_images():
    matrix = _load("release.yml")["jobs"]["images"]["strategy"]["matrix"]["include"]

    assert {m["name"] for m in matrix} == {"api", "web", "sandbox"}


def test_release_only_asks_for_the_permission_it_uses():
    """Least privilege, derived from what the workflow does — not a constant.

    This asserted `contents: read` with the note "a build job has no business
    writing code", which was exactly right while the workflow only built
    images. It now also publishes the GitHub Release for the tag, and creating
    a Release is `contents: write` in GitHub's scope model.

    So the assertion follows the work instead of naming a value: write is
    permitted only while something here actually publishes. Delete the publish
    job and leave the permission, and this fails — which is the property the
    original was protecting.

    The residual, stated rather than hidden: `contents: write` also permits
    pushing commits, and nothing in this workflow does. GitHub has no narrower
    scope for "may create a release", so the guard is this test plus the fact
    that every `run:` block here is in the file under review.
    """
    doc = _load("release.yml")
    perms = doc["permissions"]
    assert perms["packages"] == "write"

    publishes = any(
        "gh release" in (step.get("run") or "")
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
    )
    if publishes:
        assert perms["contents"] == "write", (
            "the workflow creates a Release and cannot with contents: read"
        )
    else:
        assert perms["contents"] == "read", (
            "nothing here writes to the repository; do not ask for write"
        )


def test_release_verifies_what_it_published():
    """A tag that exists but cannot be pulled is worse than no tag: the failure
    lands on the server, after the release looked finished."""
    body = _executable(_load("release.yml"))

    assert "imagetools inspect" in body, (
        "nothing checks that the manifest can actually be resolved"
    )
    # Both architectures, because the documentation sells arm64 hosts: the
    # Hetzner guide recommends a CAX21 and the Oracle one an Ampere A1.
    for arch in ("amd64", "arm64"):
        assert arch in body, arch


def test_a_half_published_release_is_not_cancelled():
    conc = _load("release.yml")["concurrency"]

    assert conc["cancel-in-progress"] is False


# ─── the trap that cost an outage ────────────────────────────────────


@pytest.mark.parametrize("wf", ["release.yml", "ci.yml"])
def test_every_run_block_is_valid_shell(wf):
    """An apostrophe inside a comment closed the single-quoted ssh argument and
    broke the step. YAML parsed it happily; only bash notices."""
    for name, run in _runs(_load(wf)):
        body = re.sub(r"\$\{\{.*?\}\}", "X", run)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(body)
            path = f.name
        proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert proc.returncode == 0, f"{wf} / {name}:\n{proc.stderr}"
