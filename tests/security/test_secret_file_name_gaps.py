"""File names that hold credentials but were once missed by the deny list."""

from __future__ import annotations

import subprocess

import pytest

from src.security.secret_files import GIT_PATHSPEC_EXCLUDES, classify

DENY = [
    ".envrc", ".env-prod", ".env_staging", "app.env.local",
    ".pgpass", ".my.cnf", ".dockercfg", ".s3cfg", "client_secret_123.json",
    "private/keys.pem.txt", "svc/user.keytab", "ci/.ENV", "ci/ID_RSA",
    "deploy/gcp-credentials.json", "deploy/prod_credentials_2026.json",
]


@pytest.mark.parametrize("rel", DENY)
def test_a_credential_file_the_list_once_missed_is_denied(rel):
    assert classify(rel) == "deny"


def test_the_git_pathspecs_ignore_case(tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "keep.py").write_text("x = 1\n")
    (tmp_path / ".ENV").write_text("A=1\n")
    (tmp_path / "ID_RSA").write_text("k\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A", "-f"], check=True)
    out = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--", ".", *GIT_PATHSPEC_EXCLUDES],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert out == ["keep.py"]


def test_a_service_account_key_under_any_name_is_denied_by_its_content():
    head = b'{"type": "service_account", "private_key_id": "k1"}'
    assert classify("deploy/ci-key.json", head) == "deny"
    assert classify("deploy/ci-key.json", b'{"type": "module"}') == "ok"
