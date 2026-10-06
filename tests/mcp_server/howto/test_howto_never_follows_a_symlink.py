"""A committed symlink must not turn the secret-file policy or the repo boundary into a suggestion."""

from __future__ import annotations

import os
import secrets
import subprocess

from src.mcp_server.howto import engine, run_howto


def _commit_link(repo, rel: str, target: str) -> None:
    link = repo.path / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for cmd in (["add", "-A"], ["commit", "-q", "-m", "link"]):
        subprocess.run(["git", "-C", str(repo.path), *cmd], check=True, env=env, capture_output=True)


def _outside_file(tmp_path) -> tuple[str, str]:
    canary = secrets.token_urlsafe(18)
    f = tmp_path / "elsewhere.py"
    f.write_text(f'conn = connect(host="db", password="{canary}", port=5432)\n# create_engine\n', encoding="utf-8")
    return str(f), canary


def test_a_symlink_to_the_env_file_is_not_read_through_howto(leaky):
    _commit_link(leaky, "src/db/linked_env.py", "../../.env")
    out = run_howto("db", "acme/shop", detail="detailed", budget_tokens=6000)
    assert "linked_env" not in out
    assert leaky.canaries["env_db"] not in out and leaky.canaries["env_jwt"] not in out


def test_a_symlink_to_a_file_outside_the_clone_is_not_read_through_howto(leaky, tmp_path):
    target, canary = _outside_file(tmp_path)
    _commit_link(leaky, "src/db/linked_out.py", target)
    out = run_howto("db", "acme/shop", detail="detailed", budget_tokens=6000)
    assert "linked_out" not in out and canary not in out


def test_tracked_files_leaves_symlinks_out(leaky, tmp_path):
    target, _ = _outside_file(tmp_path)
    _commit_link(leaky, "src/db/linked_out.py", target)
    _commit_link(leaky, "src/db/linked_env.py", "../../.env")
    files = engine.tracked_files(leaky.path)
    assert "src/db/linked_out.py" not in files and "src/db/linked_env.py" not in files
    assert "src/db/session.py" in files


def test_reading_a_symlink_directly_returns_nothing(leaky, tmp_path):
    target, _ = _outside_file(tmp_path)
    _commit_link(leaky, "src/db/linked_out.py", target)
    _commit_link(leaky, "src/db/linked_env.py", "../../.env")
    assert engine._read(leaky.path, "src/db/linked_out.py") is None
    assert engine._read(leaky.path, "src/db/linked_env.py") is None
    assert engine._read(leaky.path, "src/db/session.py")


def test_the_indexing_walker_skips_symlinks(leaky, tmp_path):
    from src.indexing.graph.languages.factory import walk_repo_files

    target, _ = _outside_file(tmp_path)
    _commit_link(leaky, "src/db/linked_out.py", target)
    _commit_link(leaky, "src/db/linked_env.py", "../../.env")
    names = {p.name for p in walk_repo_files(leaky.path)}
    assert "linked_out.py" not in names and "linked_env.py" not in names
    assert "session.py" in names


def test_the_trace_does_not_follow_a_link_either(leaky, tmp_path):
    from src.mcp_server.howto import tracer

    target, canary = _outside_file(tmp_path)
    _commit_link(leaky, "deploy/compose.yml", target)
    names = {"DATABASE_URL": tracer.NameInfo("DATABASE_URL", "db")}
    tracer.trace_sources(names, leaky.path, ["deploy/compose.yml"])
    assert canary not in repr(names)
    assert not names["DATABASE_URL"].sources


def test_the_repo_context_does_not_parse_a_real_env_file(leaky):
    from src.indexing.graph.configs import build_repo_context

    ctx = build_repo_context(leaky.path)
    assert "DATABASE_URL" in ctx.env_keys  # from .env.example only
    assert "JWT_SECRET" in ctx.env_keys
