"""The exec tool tells the model where its command will run.

MEASURED ON A REAL SESSION. The agent burned two of its seven turns on
environment discovery:

    mcp__exec__run  "cd /workspace/data/agent_workspaces/478d671b…/repo &&
                     python -m pytest tests/ -q"
      → [exit 1] /bin/bash: line 1: cd: …/repo: No such file or directory

    mcp__exec__run  "python -m pytest tests/ -q"
      → [exit 1] /usr/local/bin/python: No module named pytest

    mcp__exec__run  "pip install pytest -q && python -m pytest tests/ -q"
      → [exit 0] 3 passed in 0.01s

Both failures were predictable and both were the tool's fault, not the
model's. Its Read and Edit tools work in absolute paths under
`/workspace/data/agent_workspaces/<id>/repo`; the sandbox unpacks the tree at
`WORK_ROOT/job-<hex>` and runs with that as cwd. Nothing said so. And the
image ships an interpreter, not the project's dev dependencies.

Those two dead turns are also what got written into the pushed commit as two
`[FAIL]` pytest lines — see test_a_probe_is_not_a_failing_test.py. Cheaper to
not generate them.
"""

from __future__ import annotations


def description() -> str:
    """The tool description as the MODEL receives it — the concatenated
    value, not the source text.

    The first version of this helper regexed the source, and failed on a
    phrase that happens to be split across two adjacent string literals:
    `"…can answer \'No "` `"module named pytest\'…"`. The model reads one
    sentence; the file contains two fragments. A test that reads the file is
    testing the formatting.
    """
    import ast
    import inspect

    from src.agent import exec_tool

    tree = ast.parse(inspect.getsource(exec_tool))
    best = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "tool":
            continue
        for arg in node.args:
            try:
                value = ast.literal_eval(arg)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, str) and len(value) > len(best):
                best = value
    assert best, "could not find the run tool's description"
    return best


def test_the_model_is_told_it_is_already_in_the_repo():
    body = description()

    assert "ALREADY IN THE REPOSITORY ROOT" in body
    assert "Do not cd anywhere first" in body


def test_the_model_is_warned_off_its_own_absolute_paths():
    """The exact mistake: the path its file tools use does not exist here."""
    body = description()

    assert "/workspace/" in body
    assert "do NOT exist here" in body.replace("DO NOT", "do NOT")


def test_the_missing_test_runner_is_stated_up_front():
    body = description()

    assert "No module named pytest" in body


def test_the_fix_is_given_as_one_call_not_two():
    """Discovering it over two round trips is what cost the turn."""
    body = description()

    assert "pip install pytest -q && python -m pytest" in body


def test_the_existing_guidance_survived():
    """The description is the whole interface; adding to it must not have
    dropped what was already load-bearing."""
    body = description()

    for phrase in ("isolated sandbox container", "DISCARDED",
                   "GIT WORKS, WITH NO HISTORY", "outbound internet"):
        assert phrase in body, f"lost: {phrase}"
