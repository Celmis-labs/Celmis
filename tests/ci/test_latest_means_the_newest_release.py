""":latest moved to whatever tag was built last, including an old one.

`release.yml` pushed two tags on every build:

    tags: |
      ${{ steps.img.outputs.repo }}:${{ steps.img.outputs.tag }}
      ${{ steps.img.outputs.repo }}:latest

The second line has no condition on it. Rebuild v0.1.0 — for any reason, a
moved tag, a rerun, a cache investigation — and `:latest` silently becomes the
oldest code in the registry while every log line stays green. The only way to
notice is to pull the image and look at what came back.

That is not a cosmetic mislabel here. `.env.example` ships `CELMIS_TAG=latest`
and docker-compose.yml falls back to `latest` when it is unset, so `:latest`
is precisely what a first-time installation pulls. It happened: a stray
`git tag -f v0.1.0` in a push script triggered a rebuild, and for a few
minutes a fresh `docker compose up` would have installed the root commit —
without eight releases of fixes, including an MCP endpoint unreachable on
every install and a sandbox on the wrong port.

Same family as the tag/literal guard next door: a value asserts one thing and
means another. That one tied the tag to the version literal. This ties
`:latest` to being the newest release, which is the only thing the word can
honestly mean.

The decision lives in a script rather than in a YAML expression, because a
YAML expression cannot be run in a test and this is the second CI defect in a
day that a test would have caught before a rebuild did.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "should_move_latest.py"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

sys.path.insert(0, str(ROOT / "scripts"))


def decide(tag: str, tags: list[str]) -> bool:
    from should_move_latest import should_move
    return should_move(tag, tags)


RELEASES = ["v0.1.0", "v0.1.1", "v0.1.7", "v0.1.8"]


# ─── the decision ────────────────────────────────────────────────────

def test_the_newest_release_moves_latest():
    assert decide("v0.1.8", RELEASES) is True


def test_rebuilding_an_older_tag_does_not():
    """The incident, as a test."""
    assert decide("v0.1.0", RELEASES) is False
    assert decide("v0.1.7", RELEASES) is False


def test_the_tag_being_built_moves_latest_when_it_is_the_newest():
    assert decide("v0.1.9", RELEASES + ["v0.1.9"]) is True


def test_the_first_tag_ever_moves_latest():
    """A repository with exactly one release. The list holds that one tag."""
    assert decide("v0.1.0", ["v0.1.0"]) is True


def test_a_list_that_does_not_contain_the_tag_moves_nothing():
    """Fail closed, and this is not hypothetical.

    The tag being built always exists on the remote: a tag push runs the
    workflow after the push, a manual dispatch names an existing tag. So a
    list without it means the lookup failed — a wrong remote, no network, a
    changed ls-remote format. Treating that as "no releases exist, therefore I
    am the newest" would move :latest on exactly the reruns this guards.

    The first version of the script did that, and a local simulation of the
    workflow step caught it reporting `true` for v0.1.0.
    """
    assert decide("v0.1.9", RELEASES) is False
    assert decide("v0.1.0", []) is False


def test_versions_compare_numerically_not_lexically():
    """`v0.10.0` is newer than `v0.9.0`; sorted as text it is not.

    The classic way a version check passes every test written on
    single-digit versions and fails the first time a minor reaches ten.
    """
    assert decide("v0.10.0", ["v0.9.0", "v0.10.0"]) is True
    assert decide("v0.9.0", ["v0.9.0", "v0.10.0"]) is False
    assert decide("v1.0.0", ["v0.99.99", "v1.0.0"]) is True


def test_a_prerelease_never_moves_latest():
    """`latest` means what a first-time install should get, and that is not an rc."""
    for tag in ("v0.2.0-rc1", "v0.2.0-beta.1", "v0.2.0+build7"):
        assert decide(tag, RELEASES) is False, tag


def test_a_prerelease_in_the_list_does_not_hold_back_a_release():
    assert decide("v0.1.9", RELEASES + ["v0.1.9", "v0.2.0-rc1"]) is True


def test_something_that_is_not_a_version_moves_nothing():
    for tag in ("latest", "nightly", "v1", "v1.2", "1.2.3", "", "v1.2.3.4"):
        assert decide(tag, RELEASES) is False, tag


def test_junk_in_the_tag_list_is_ignored_not_fatal():
    assert decide("v0.1.9", ["v0.1.8", "v0.1.9", "not-a-tag", "", "release-2024"]) is True


# ─── the script as the workflow calls it ─────────────────────────────

def _run(tag: str, tags: list[str]) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(SCRIPT), tag],
                       input="\n".join(tags), capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def test_the_cli_prints_true_or_false_and_exits_zero():
    """Exit code stays 0 either way: a `false` is an answer, not a failure.

    A non-zero exit would abort the step under `set -e` and take the whole
    release with it — the build must still publish its version tag.
    """
    code, out = _run("v0.1.8", RELEASES)
    assert (code, out) == (0, "true")   # v0.1.8 is in RELEASES and is the top
    code, out = _run("v0.1.0", RELEASES)
    assert (code, out) == (0, "false")


def test_the_cli_handles_an_empty_tag_list():
    """Nothing on stdin means the lookup produced nothing. Exit 0, answer no."""
    code, out = _run("v0.1.0", [])
    assert code == 0 and out == "false"


# ─── the workflow ────────────────────────────────────────────────────

def _build_step() -> dict:
    doc = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["images"]["steps"]
    hits = [s for s in steps if "build-push-action" in (s.get("uses") or "")]
    assert len(hits) == 1, "expected exactly one build-push step"
    return hits[0]


def test_the_build_does_not_hardcode_latest_in_its_tag_list():
    """Parsed YAML, not a grep: the comment above this block says `latest` too,
    and a grep would find it there and call a fixed workflow broken."""
    tags = _build_step()["with"]["tags"]
    lines = [ln.strip() for ln in str(tags).splitlines() if ln.strip()]
    offenders = [ln for ln in lines if ln.endswith(":latest")]
    assert not offenders, (
        f"release.yml still pushes {offenders} unconditionally — rebuilding an "
        f"old tag will repoint the image a fresh install pulls"
    )


def test_the_workflow_asks_the_script():
    text = RELEASE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    runs = "\n".join(s.get("run", "") for s in doc["jobs"]["images"]["steps"])
    assert "should_move_latest.py" in runs, (
        "nothing in the workflow consults the decision script"
    )


def test_the_script_is_executable_and_shipped():
    assert SCRIPT.exists(), "the workflow calls a script that is not in the repo"
