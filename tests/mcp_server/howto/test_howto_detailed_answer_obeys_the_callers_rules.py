"""The detailed answer (dependencies, related tests) filters paths like the scan does."""

from __future__ import annotations

import os
import subprocess

from src.access import RepoAccessDecision
from src.access.resolver import _RuleView
from src.mcp_server.howto import engine, run_howto


def _add(repo, files: dict[str, str]) -> None:
    for rel, body in files.items():
        fp = repo.path / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for cmd in (["add", "-A"], ["commit", "-q", "-m", "more"]):
        subprocess.run(["git", "-C", str(repo.path), *cmd], check=True, env=env, capture_output=True)


def test_dependencies_and_tests_in_a_hidden_directory_are_not_listed(leaky, monkeypatch):
    _add(leaky, {
        "hidden/payments/requirements.txt": "sqlalchemy==2.0.40\n",
        "hidden/payments/tests/test_db_hidden.py": "def test_x():\n    pass\n",
        "requirements.txt": "sqlalchemy==2.0.40\n",
    })
    rule = _RuleView("code", (), ("hidden/**",), ())
    dec = RepoAccessDecision(repo_slug=leaky.slug, visibility="code", rules=(rule,), open_default=False)
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {leaky.slug: dec})
    out = run_howto("db", "acme/shop", detail="detailed", budget_tokens=6000)
    assert "hidden/" not in out
    assert "requirements.txt:1" in out
