"""celmis 0.1.0 and 0.2.0 were uploaded by hand, and PyPI says so.

`provenance=None` for both artefacts: nobody holding those files can tell which
commit built them, or that they came from this repository at all rather than
from whoever else held the API token. The images had the same gap — the OCI
`revision` label says which commit an image CLAIMS, and a label is text anybody
can write.

Two fixes, one shape. A publish workflow with Trusted Publishing, so upload
rights belong to a workflow in a repository for the length of a run rather than
to a token in a secret; and a signed build attestation on each image, checkable
with `gh attestation verify` by somebody who has only the image.

THE TAG PREFIX IS THE PART THAT NEEDED MEASURING. `release.yml` triggers on the
glob `v*`, and the obvious name for a verifier tag — `verifier-0.2.1` — matches
it, because "verifier" starts with a v. That tag would have built and published
three container images. The test below is that check, kept.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish-verifier.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc[True] if True in doc else doc["on"]


def _tag_patterns(path: Path) -> list[str]:
    return _triggers(_doc(path))["push"]["tags"]


def test_the_publish_workflow_exists() -> None:
    assert PUBLISH.is_file(), (
        "there is no publish workflow, so every release is somebody's laptop "
        "and a token"
    )


def test_a_verifier_tag_cannot_start_an_image_build() -> None:
    """The measurement that changed the prefix.

    `v*` is a glob over the whole tag, so anything beginning with the letter v
    matches — `verifier-0.2.1` included.
    """
    publish_patterns = _tag_patterns(PUBLISH)
    release_patterns = _tag_patterns(RELEASE)

    for tag in ("pypi-0.2.1", "pypi-1.0.0"):
        assert any(fnmatch.fnmatch(tag, p) for p in publish_patterns), (
            f"{tag} does not trigger the publish workflow: {publish_patterns}"
        )
        assert not any(fnmatch.fnmatch(tag, p) for p in release_patterns), (
            f"{tag} ALSO triggers the image release ({release_patterns}), "
            f"which would build three containers for a package release"
        )


def test_the_obvious_wrong_name_is_still_wrong() -> None:
    """Kept as the reason, so nobody renames the prefix back."""
    assert fnmatch.fnmatch("verifier-0.2.1", "v*"), (
        "if this ever stops being true the comment in publish-verifier.yml is "
        "stale, but the prefix is still fine"
    )
    assert not any(
        fnmatch.fnmatch("verifier-0.2.1", p) for p in _tag_patterns(PUBLISH)
    )


def test_upload_uses_a_trusted_publisher_and_no_token() -> None:
    jobs = _doc(PUBLISH)["jobs"]
    publish = jobs["publish"]
    assert publish["permissions"].get("id-token") == "write", (
        "without id-token: write there is no OIDC identity and the action "
        "falls back to looking for a token"
    )
    assert publish.get("environment") == "pypi", (
        "the trusted publisher is registered against an environment; without "
        "it the claim will not match"
    )
    steps = publish["steps"]
    assert any("pypa/gh-action-pypi-publish" in str(s.get("uses", "")) for s in steps)
    body = PUBLISH.read_text(encoding="utf-8")
    for token in ("PYPI_TOKEN", "TWINE_PASSWORD", "password:"):
        assert token not in body, f"the workflow still references {token}"


def test_the_platform_cannot_leave_under_the_verifier_name() -> None:
    """The root distribution is `celmis-platform` for this reason; this is the
    second lock, because the first only helps if the accident is at the root."""
    steps = _doc(PUBLISH)["jobs"]["build"]["steps"]
    guard = [s for s in steps if "dist/" in (s.get("name") or "")]
    assert guard, "nothing checks what ended up in dist/ before it is uploaded"
    body = " ".join(s.get("run", "") for s in steps)
    assert "src/" in body, "the guard does not look for platform source in the wheel"


def test_the_tag_and_the_package_version_must_agree() -> None:
    """A PyPI version cannot be replaced, so publishing the wrong one is
    permanent."""
    body = " ".join(
        s.get("run", "") for s in _doc(PUBLISH)["jobs"]["build"]["steps"]
    )
    assert "pyproject.toml" in body and "::error::" in body


# ─── the images ──────────────────────────────────────────────────────


def test_images_carry_a_build_attestation() -> None:
    doc = _doc(RELEASE)
    perms = doc["permissions"]
    assert perms.get("id-token") == "write"
    assert perms.get("attestations") == "write"

    steps = doc["jobs"]["images"]["steps"]
    attest = [s for s in steps if "attest-build-provenance" in str(s.get("uses", ""))]
    assert attest, "no image is attested, so its `revision` label is the only claim"
    assert attest[0]["with"]["push-to-registry"] is True, (
        "the attestation is not pushed, so somebody holding the image cannot "
        "find it"
    )


def test_the_attestation_names_a_digest_not_a_tag() -> None:
    """A tag moves; `:latest` moved four times this week."""
    steps = _doc(RELEASE)["jobs"]["images"]["steps"]
    attest = next(s for s in steps if "attest-build-provenance" in str(s.get("uses", "")))
    subject = str(attest["with"]["subject-digest"])
    assert "digest" in subject, f"the attestation subject is {subject!r}"
    build = next(s for s in steps if "build-push-action" in str(s.get("uses", "")))
    assert build.get("id") == "build", (
        "the build step has no id, so steps.build.outputs.digest is empty and "
        "the attestation would name nothing"
    )


@pytest.mark.parametrize("key", ["provenance"])
def test_buildkits_own_provenance_stays_off(key: str) -> None:
    """A different mechanism, deliberately not used.

    It rewrites the manifest into an index; the comment where it is set says
    why that is unwanted, and the attestation above is the separate thing that
    answers "built from what".
    """
    steps = _doc(RELEASE)["jobs"]["images"]["steps"]
    build = next(s for s in steps if "build-push-action" in str(s.get("uses", "")))
    assert build["with"][key] is False
