"""``bin/celmis-auth-header``: the header JSON, and nothing else, on stdout."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.plugin import contract as C

HELPER = C.PLUGIN / "bin" / "celmis-auth-header"
#: A PATH with no keychain tools, so the test never reads the developer's own.
_BARE = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}


def _run(env: dict[str, str], tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name in ("security", "secret-tool"):  # shadow the real ones
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    full = {**_BARE, "PATH": f"{stubs}:{_BARE['PATH']}", **env}
    return subprocess.run(["/bin/sh", str(HELPER)], capture_output=True, text=True,
                          env=full, timeout=10)


def test_the_token_from_the_environment_becomes_an_authorization_header(tmp_path) -> None:
    done = _run({"CELMIS_TOKEN": "abc.DEF-123_xyz~+/="}, tmp_path)
    assert done.returncode == 0
    assert json.loads(done.stdout) == {"Authorization": "Bearer abc.DEF-123_xyz~+/="}
    assert done.stderr == ""


def test_no_token_anywhere_fails_with_a_message_that_has_no_secret(tmp_path) -> None:
    done = _run({}, tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert "CELMIS_TOKEN" in done.stderr


@pytest.mark.parametrize("token", ['a"b', "a b", "a\\b", "a$(id)", "a;b", "a\nb"])
def test_a_token_that_would_need_escaping_is_refused_not_emitted(token, tmp_path) -> None:
    done = _run({"CELMIS_TOKEN": token}, tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert token not in done.stderr


def test_the_keychain_is_preferred_over_the_environment(tmp_path) -> None:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("security", "secret-tool"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\necho from-keychain\n")
        stub.chmod(0o755)
    env = {**_BARE, "PATH": f"{stubs}:{_BARE['PATH']}", "CELMIS_TOKEN": "from-env"}
    done = subprocess.run(["/bin/sh", str(HELPER)], capture_output=True, text=True, env=env)
    assert json.loads(done.stdout) == {"Authorization": "Bearer from-keychain"}
    assert os.access(HELPER, os.X_OK)
