"""`ci.yml` said the rules-of-hooks rule was blocking. It was blocking nowhere.

The comment read: that rule "is now enforced as a TEST —
tests/web/test_rules_of_hooks.py, in the python job above — so it is blocking
there rather than advisory here". Two facts made it false. The tests carry
`skipif(not _eslint_available())`, and the python job never runs
`pnpm install`, so `web/node_modules` is absent and every one of them reports
SKIPPED — in green. Meanwhile this step ended in `|| echo "::warning::"`.

A gate everybody believes in and nothing enforces is worse than an admitted
absence: it is the reason nobody looked for eight months while 42 findings
accumulated, 24 of them in the same react-hooks family whose breach took every
authenticated page down.

These tests read the workflow with a YAML parser and the gate with `ast`.
Searching either file for "eslint" would pass on the version this replaces —
the false comment says the word four times.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
GATE = ROOT / "scripts" / "eslint_gate.py"


def _steps(job: str) -> list[dict]:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return (doc["jobs"][job].get("steps") or [])


def test_the_lint_step_can_fail_the_build() -> None:
    """`|| echo` turns any command into a success. That was the whole gate."""
    runs = [s.get("run", "") for s in _steps("web")]
    lint = [r for r in runs if "eslint" in r]
    assert lint, "no step runs eslint at all any more"
    for command in lint:
        assert "|| echo" not in command and "|| true" not in command, (
            f"the lint step swallows its own exit status: {command.strip()!r}"
        )


def test_the_gate_is_what_the_step_runs() -> None:
    runs = " ".join(s.get("run", "") for s in _steps("web"))
    assert "eslint_gate.py" in runs, (
        "the web job no longer calls the gate, so nothing enforces the count"
    )


def test_rules_of_hooks_is_fatal_regardless_of_the_baseline() -> None:
    """The two rules are different in kind and must stay that way.

    A trend is the right instrument for `no-explicit-any`. It is the wrong one
    for the rule that took the site down: one violation is a regression, not a
    worsening average.
    """
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    fatal: tuple[str, ...] = ()
    baseline: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "FATAL_RULES":
                fatal = tuple(
                    e.value for e in ast.walk(node.value)
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
            elif name == "BASELINE" and isinstance(node.value, ast.Constant):
                baseline = node.value.value

    assert "react-hooks/rules-of-hooks" in fatal, (
        f"the outage rule is not in FATAL_RULES: {fatal}"
    )
    assert isinstance(baseline, int) and baseline >= 0, (
        "the ratchet has no written-down baseline, so it cannot hold anything"
    )


def test_the_baseline_is_a_number_in_the_file_not_a_computed_one() -> None:
    """A threshold derived from the current tree ratchets nothing.

    `--baseline $(count findings)` is always satisfied. The number has to be a
    literal so that lowering it is an edit somebody makes and a reviewer sees.
    """
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    assignment = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "BASELINE"
    )
    assert isinstance(assignment.value, ast.Constant), (
        "BASELINE is computed rather than written down"
    )


def test_the_story_test_needs_no_node_and_says_so() -> None:
    """One decorator was the difference between a check and a skip.

    `test_the_switcher_calls_its_hooks_before_the_session_guard` reads
    app-shell.tsx as text. It carried `skipif(not _eslint_available())` anyway,
    so in the python job — which installs no node_modules — the only check of
    this family that could have run did not.
    """
    source = (ROOT / "tests" / "web" / "test_rules_of_hooks.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    target = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_the_switcher_calls_its_hooks_before_the_session_guard"
    )
    decorators = [ast.unparse(d) for d in target.decorator_list]
    assert not any("skipif" in d for d in decorators), (
        f"the text-only check is skipped again: {decorators}"
    )


@pytest.mark.parametrize("name", ["run_eslint", "main"])
def test_the_gate_reads_json_rather_than_an_exit_status(name: str) -> None:
    """eslint exits non-zero for warnings too, so the status cannot count.

    A gate built on `eslint; echo $?` cannot tell 42 known findings from 43,
    which is the only question it exists to answer.
    """
    tree = ast.parse(GATE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    source = ast.unparse(fn)
    if name == "run_eslint":
        assert "--format" in source and "json" in source
    else:
        assert "severity" in source or "messages" in source
