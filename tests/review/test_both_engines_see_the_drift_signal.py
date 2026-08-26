"""The Claude Code engine reviewed without the cross-repo drift block.

`_build_context` runs before the engine is chosen. It calls the drift
detector, puts the result on `context.cross_repo_drift`, records the facts on
the orchestrator, and those facts reach `drift_json` on the run row. All of
that happened for a Claude Code review. The engine was simply never handed the
block.

So a workspace on that engine reviewed a change to a shared constant with no
way to know the constant was shared — while the run record reported a drift
check, because one had run. That is worse than not running it: the number says
the search happened and the reviewer never saw the answer.

Cross-repo drift is the one signal nothing else in the pipeline provides — a
deterministic grep across the siblings in a repository's group, no model call
— and it is the reason `POST /api/repos/groups` was written at all.
"""

from __future__ import annotations

import inspect


def test_the_engine_accepts_the_drift_block():
    from src.review.claude_engine import run_claude_review

    params = inspect.signature(run_claude_review).parameters
    assert "cross_repo_drift" in params, (
        "the Claude Code engine cannot be given a drift signal at all"
    )


def test_the_orchestrator_hands_it_over():
    """Asserted on the call in the source, because reaching this branch for
    real needs a provider, a diff and a subscription token. The property is
    narrow and structural: whatever the orchestrator passes, it must include
    the drift the context is already carrying."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/review/orchestrator.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "run_claude_review"
    ]
    assert calls, "the Claude Code engine is no longer called"
    for call in calls:
        names = {kw.arg for kw in call.keywords}
        assert "cross_repo_drift" in names, (
            f"run_claude_review at line {call.lineno} is called without the "
            f"drift block; it is already on the context by then"
        )


def test_it_is_not_recomputed():
    """The detector greps every sibling repository in the group. Calling it a
    second time inside the engine branch would double that for no new answer —
    `_build_context` has already run it and stored the facts."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/review/orchestrator.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_build_cross_repo_drift"
    ]
    assert len(calls) == 1, (
        f"the drift detector is invoked {len(calls)} times; it greps every "
        f"sibling repository and one review needs one answer"
    )


def test_the_block_reaches_the_prompt():
    """End of the chain inside the engine: the string has to land in the
    context the model is given, not merely be accepted as an argument."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/review/claude_engine.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    uses = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "cross_repo_drift"
        and isinstance(n.ctx, ast.Load)
    ]
    assert len(uses) >= 3, (
        "cross_repo_drift is accepted but barely used — it has to reach the "
        f"prompt, not just the signature (found {len(uses)} reads)"
    )


def test_the_agent_pipeline_still_gets_it():
    """The engine that already worked must keep working."""
    from pathlib import Path

    base = (Path(__file__).resolve().parents[2]
            / "src/review/agents/base.py").read_text(encoding="utf-8")
    assert "cross_repo_drift=context.cross_repo_drift" in base
