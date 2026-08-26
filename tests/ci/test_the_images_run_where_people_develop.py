"""An image that only runs on the server is half an image.

`release.yml` pinned `platforms: linux/amd64` with the comment "the only
architecture we deploy to" — which meant the one box in front of whoever wrote
it. Two facts made that wrong in both directions:

  * the documentation sells arm64. `docs/HETZNER.md` recommends a CAX21 (ARM)
    and explains at length why ARM is the right 2026 choice; `docs/ORACLE_CICD.md`
    picks an Ampere A1. A user following either gets
    "no matching manifest for linux/arm64/v8";
  * development happens on Apple Silicon. `docker compose up` on an M-series
    Mac pulling an amd64-only image runs it under emulation if it runs at all.

The `Dockerfile` already carries per-architecture SHA pins precisely so arm64
works — the build was the only thing refusing to use them.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github/workflows/release.yml"


def _release() -> dict:
    doc = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps() -> list[dict]:
    return _release()["jobs"]["images"]["steps"]


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_both_architectures_are_built(arch):
    build = next(s for s in _steps() if "build-push-action" in str(s.get("uses", "")))

    assert arch in build["with"]["platforms"], build["with"]["platforms"]


def test_the_release_fails_if_an_architecture_is_missing():
    """A tag that pulls on the server and not on the developer's laptop is a
    tag that looks finished and is not."""
    body = "\n".join(s.get("run", "") for s in _steps())

    assert "imagetools inspect" in body, (
        "a `docker pull` resolves to the runner's own architecture and would "
        "say nothing about the other one"
    )
    for arch in ("amd64", "arm64"):
        assert arch in body, arch
    assert "exit 1" in body, "a missing architecture does not fail the release"


def test_the_dockerfile_still_pins_per_architecture():
    """The SHA pins are what make arm64 reproducible; a build that stopped
    using them would quietly lose that."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert re.search(r"(arm64|aarch64)", text), (
        "the per-architecture pins are gone — check before relaxing platforms"
    )


def test_compose_names_no_architecture():
    """The tag a deployment names must resolve on whatever it runs on. A
    `-amd64` suffix in compose would put the architecture in the address."""
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for name in ("api", "web", "sandbox"):
        image = doc["services"][name]["image"]
        assert "amd64" not in image and "arm64" not in image, image


# ─── the package has to know where it came from ──────────────────────


def test_the_image_names_its_source_repository():
    """Without `org.opencontainers.image.source` a published package is an
    orphan: GHCR links a package to a repository through that label and only
    through it. No label means no Source link on the package page, and under an
    organisation no inherited repository permissions — access granted to each
    package by hand, forever.
    """
    build = next(s for s in _steps() if "build-push-action" in str(s.get("uses", "")))
    labels = build["with"].get("labels", "")

    assert "org.opencontainers.image.source" in labels


def test_the_source_label_is_not_a_written_out_name():
    """A fork must label its own images. Spelling the URL out means every fork
    publishes packages pointing at somebody else's repository, and the line has
    to be edited after every move — which is how the compose file ended up
    naming one personal account three times.
    """
    build = next(s for s in _steps() if "build-push-action" in str(s.get("uses", "")))
    labels = build["with"]["labels"]

    assert "github.repository" in labels, labels
    assert "github.com/Celmis" not in labels, "the repository is hard-coded"


def test_the_image_records_the_licence_it_ships_under():
    """AGPL travels with the artifact, not only with the source tree."""
    build = next(s for s in _steps() if "build-push-action" in str(s.get("uses", "")))

    assert "AGPL-3.0" in build["with"]["labels"]
