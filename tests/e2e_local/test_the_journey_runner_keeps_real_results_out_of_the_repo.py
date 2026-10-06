"""The journey runner's own safety rails: where results may go and what counts as a secret.

The full journey (``scripts/dev_mcp_journey.py``) takes minutes and is run by hand;
these tests keep its guard rails honest without starting a server.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import dev_mcp_e2e as e2e
from scripts import dev_mcp_journey as journey

ROOT = Path(__file__).resolve().parents[2]


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=env)
    return path


def test_results_for_real_services_cannot_be_written_inside_this_repository() -> None:
    with pytest.raises(SystemExit):
        journey.main(["--real", "svc", "--out", str(ROOT / "docs" / "journey-out")])


def test_a_password_literal_in_a_tracked_file_is_armed_but_a_placeholder_is_not(tmp_path: Path) -> None:
    secret = "Zk9" + "qW2mRt7Lx"  # assembled: no literal secret in this file
    repo = _git_repo(tmp_path / "svc", {
        "app/db.py": f'password = "{secret}"\nother_password = "changeme"\napi_key = "your-key-here"\n',
        "README.md": "no secrets here\n",
    })
    found = journey.literal_secrets_in(repo)
    assert [s.value for s in found] == [secret]
    assert "app/db.py" in found[0].name and secret not in found[0].name


def test_a_local_env_value_committed_as_a_default_is_public_but_a_secret_named_one_is_not(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "svc", {"app/config.py": 'HOME_PAGE = "https://app.example.com/start"\n'
                                        'SESSION_SECRET = "abcdef123456"\n'})
    plain_value = e2e.Secret("HOME_PAGE_URL", "https://app.example.com/start")
    secret_named = e2e.Secret("SESSION_SECRET", "abcdef123456")
    dsn_with_password = e2e.Secret("DATABASE_URL", "postgresql://app:pw1234@db/app")
    assert journey.public_value(plain_value, [repo]) is True
    assert journey.public_value(secret_named, [repo]) is False
    assert journey.public_value(dsn_with_password, [repo]) is False
    assert journey.public_value(e2e.Secret("OTHER_URL", "https://not-committed.example.com"), [repo]) is False


def test_a_blocked_repository_answer_is_compared_without_the_callers_spelling() -> None:
    a = journey.plain("idx: x\nnot found: github_acme-shop\nsimilar: a, b", "github_acme-shop")
    b = journey.plain("idx: x\nnot found: github_nobody-nothing", "github_nobody-nothing")
    assert a == b
