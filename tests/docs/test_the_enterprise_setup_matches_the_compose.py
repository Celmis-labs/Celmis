"""The enterprise setup notes must describe what the shipped compose delivers.

ee/README showed `CELMIS_LICENSE_FILE=/run/secrets/...` against an api service
with no such mount, and offered `OIDC_*` names compose never forwards; the
Keycloak dev compose listed every SSO variable except the licence SSO needs.
Each of those configures nothing and fails quietly into the community build.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _api() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["api"]


def _api_env_names() -> set[str]:
    env = _api()["environment"]
    return set(env) if isinstance(env, dict) else {e.split("=", 1)[0] for e in env}


def _mount_targets() -> list[PurePosixPath]:
    targets = []
    for v in _api().get("volumes", []):
        target = v["target"] if isinstance(v, dict) else v.split(":")[1]
        targets.append(PurePosixPath(target))
    return targets


def test_the_licence_file_example_is_inside_a_mount():
    text = (ROOT / "ee" / "README.md").read_text()
    paths = re.findall(r"^CELMIS_LICENSE_FILE=(\S+)", text, re.M)
    assert paths, "ee/README no longer shows a CELMIS_LICENSE_FILE example"
    for p in map(PurePosixPath, paths):
        assert any(p.is_relative_to(m) for m in _mount_targets()), (
            f"{p} is not under any volume the api service mounts"
        )


def test_every_licence_and_oidc_name_the_readme_sets_is_forwarded():
    text = (ROOT / "ee" / "README.md").read_text()
    names = set(re.findall(r"^(CELMIS_LICENSE_\w+|AUTH_OIDC_\w+)=", text, re.M))
    assert names <= _api_env_names(), names - _api_env_names()


def test_the_keycloak_recipe_names_the_licence():
    header = "".join(
        line for line in (ROOT / "deploy/keycloak/docker-compose.yml")
        .read_text().splitlines(keepends=True) if line.startswith("#")
    )
    env_lines = set(re.findall(r"^#\s+([A-Z][A-Z0-9_]*)=", header, re.M))
    assert "AUTH_OIDC_ISSUER" in env_lines
    assert "CELMIS_LICENSE_KEY" in env_lines, (
        "the SSO recipe omits the licence without which SSO is never mounted"
    )
    assert env_lines <= _api_env_names() | {"AUTH_OIDC_CLIENT_SECRET", "AUTH_OIDC_NAME"}
