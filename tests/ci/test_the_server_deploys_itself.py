"""The deploy moved onto the server, and four things had to move with it.

It used to run from a GitHub runner over ssh, which meant the repository's
secrets held a root key to the production box. That was the only way to deliver
code while the images were built there; now they come from a registry, so the
server fetches its own updates and the direction of trust reverses.

`pull && up -d` is not the whole job, and each of the four missing pieces was
learned from an outage — which is exactly why each would disappear silently in
a rewrite. These pin them:

  1. reclaim disk BEFORE anything writes. On a full disk the pull fails too, so
     a cleanup placed after it never runs.
  2. repair network aliases AFTER `up -d`. Compose recreates a network whose
     definition changed and RECONNECTS the containers it is not recreating —
     and a reconnect does not restore the service-name alias, so Postgres
     answers only to `celmis-postgres` and everything that connects to
     `postgres` fails DNS. This has taken the box down.
  3. check `alembic current` is at head. A migration that did not run is
     invisible until it fails elsewhere as something unrelated.
  4. stamp the build: `/api/capabilities` reads `api_version` from it, and the
     AGPL §13 footer links to the source AT THAT VERSION. An unstamped deploy
     offers the wrong source, which is the one thing that footer exists to get
     right.

These are structural checks on a shell script, which is a weak form of test —
so each asserts an executable line rather than the presence of a word, and the
ordering ones assert ORDER, which is the part that was wrong before.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/deploy-on-server.sh"


def _lines() -> list[str]:
    """Executable lines only — a comment naming a command is not the command."""
    return [
        line.strip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _index(needle: str) -> int:
    for i, line in enumerate(_lines()):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} is not in the script")


def test_the_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "not executable"


def test_it_parses_as_shell():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_it_stops_on_the_first_error():
    assert "set -euo pipefail" in _lines()


# ─── 1. disk, and the order that matters ─────────────────────────────


def test_disk_is_reclaimed_before_the_pull():
    assert _index("docker image prune") < _index("$COMPOSE pull"), (
        "a cleanup after the pull never runs on the disk that needs it"
    )


def test_it_refuses_to_start_without_headroom():
    body = "\n".join(_lines())

    assert "2097152" in body, "no free-space floor"
    assert _index("2097152") < _index("$COMPOSE pull")


@pytest.mark.parametrize("forbidden", ["volume prune", "system prune"])
def test_it_can_never_take_the_data_with_it(forbidden):
    """Postgres, Qdrant and the LiteLLM database live in volumes on that box."""
    assert forbidden not in "\n".join(_lines())


def test_it_does_not_run_the_prune_that_reclaims_nothing():
    """Measured on that server: `builder prune -af` frees 0B, because with the
    containerd image store the space it reports is image layers."""
    assert "builder prune" not in "\n".join(_lines())


# ─── 2. aliases, after the thing that breaks them ────────────────────


def test_aliases_are_repaired_after_up():
    assert _index("getent hosts") > _index("$COMPOSE up -d")


def test_a_failed_up_does_not_skip_the_repair():
    """`set -e` would stop on the symptom and never reach the cause."""
    body = "\n".join(_lines())

    assert "UP_FAILED" in body
    assert "|| UP_FAILED=1" in body


def test_the_stack_is_not_reported_up_while_dns_is_broken():
    lines = _lines()
    last_getent = max(i for i, line in enumerate(lines) if "getent hosts" in line)
    assert any("fail " in line for line in lines[last_getent:last_getent + 3]), (
        "a name that does not resolve has to end the deploy"
    )


# ─── 3 and 4. the migration and the stamp ────────────────────────────


def test_the_migration_is_verified_at_head():
    body = "\n".join(_lines())

    assert "alembic current" in body
    assert "(head)" in body


def test_the_build_is_stamped_from_the_digest_not_the_tag():
    """A tag can be moved; "which build is this" has to survive that."""
    body = "\n".join(_lines())

    assert "CELMIS_GIT_SHA" in body
    assert "RepoDigests" in body, "the stamp comes from a tag, which can move"


def test_the_stamp_happens_before_the_health_check():
    """The api reads it at start-up, so stamping after would report the
    previous deploy's version until the next restart."""
    assert _index("CELMIS_GIT_SHA") < _index("Health.Status")
