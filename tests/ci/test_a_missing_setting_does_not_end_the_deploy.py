"""A deploy stopped because a hardening setting was absent from .env.

`deploy-on-server.sh` runs under `set -euo pipefail`. It read the sandbox
subnet with

    SANDBOX_SUBNET="$(grep -E '^SANDBOX_NET_SUBNET=' .env | cut ... | tr ...)"

and grep exits 1 when the line is simply not there. `pipefail` carries that
to the pipeline, and a failing substitution in an assignment ends the script
— three lines below a comment promising that "a deploy must not stop over a
hardening rule", and one line above the default written for exactly this
case, which was therefore never reached.

Where it stopped matters: after `docker compose up -d`. New containers were
already running, CELMIS_GIT_SHA was never stamped, and the health check that
decides whether the deploy worked never ran.

This test runs the real line out of the real script rather than a copy of
it, so reintroducing the bug fails here instead of on somebody's install.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-on-server.sh"


def _subnet_lines() -> str:
    """The assignment and its default, lifted verbatim from the script."""
    text = SCRIPT.read_text()
    match = re.search(
        r'^(SANDBOX_SUBNET="\$\(.*?\)"\n: "\$\{SANDBOX_SUBNET:=[^\n]*\n)',
        text, re.M | re.S,
    )
    assert match, (
        "could not find the SANDBOX_SUBNET assignment followed by its default "
        "in deploy-on-server.sh — this test is no longer reading the code it "
        "is about"
    )
    return match.group(1)


@pytest.mark.parametrize(
    ("env_body", "expected"),
    [
        ("FOO=bar\n", "172.28.90.0/24"),          # the setting is absent
        ("SANDBOX_NET_SUBNET=10.9.0.0/24\n", "10.9.0.0/24"),
        ("", "172.28.90.0/24"),                    # an empty .env
        ("SANDBOX_NET_SUBNET=\n", "172.28.90.0/24"),   # present but blank
    ],
    ids=["absent", "set", "empty-file", "blank-value"],
)
def test_the_deploy_survives_and_picks_the_right_subnet(
    tmp_path: Path, env_body: str, expected: str,
) -> None:
    (tmp_path / ".env").write_text(env_body)
    script = "set -euo pipefail\n" + _subnet_lines() + 'echo "$SANDBOX_SUBNET"\n'
    done = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, (
        f".env {env_body!r} ended the deploy with exit {done.returncode}: "
        f"{done.stderr.strip()!r}. Everything after this line — the version "
        f"stamp and the health check — never runs."
    )
    assert done.stdout.strip() == expected


def test_the_script_still_parses() -> None:
    done = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
