"""A one-line minified file must not flood the answer, whatever the line budget says."""

from __future__ import annotations

import os
import subprocess

from src.mcp_server.howto import engine, run_howto


def test_a_very_long_line_is_cut_and_marked():
    body = "x = 1\n" + "y" * 5000 + "\nz = 2"
    out = engine.clip_slice(body, max_chars=4000)
    assert "...[line truncated]" in out
    assert max(len(line) for line in out.split("\n")) < 450


def test_the_whole_slice_is_cut_at_the_character_budget():
    body = "\n".join(f"line_{i} = {i}" * 3 for i in range(500))
    out = engine.clip_slice(body, max_chars=600)
    assert len(out) <= 640 and out.endswith("...[slice truncated]")


def test_a_minified_file_is_bounded_in_the_answer(leaky):
    junk = "create_engine(" + "a" * 60000 + ")"
    (leaky.path / "src/db/min.py").write_text(junk, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for cmd in (["add", "-A"], ["commit", "-q", "-m", "min"]):
        subprocess.run(["git", "-C", str(leaky.path), *cmd], check=True, env=env, capture_output=True)
    out = run_howto("db", "acme/shop", budget_tokens=200)
    assert "a" * 500 not in out
    assert len(out) < 12000
