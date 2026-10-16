"""The veto's evidence includes the BODIES of functions called next to the line.

A typical false positive: "variable used after the catch" where the catch
calls a helper (a closure in a variable) that ends the process with `exit()`. The window
around the line never contains the helper, so a reader of the evidence could
not tell that execution does not continue; now it can. Also: the prompt shows
the evidence before the claim and asks for a written premise check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from src.review.agents import verifier_evidence as ve
from src.review.agents.base import AgentContext
from src.review.agents.verifier import _VERIFIER_SYSTEM, VerifierAgent
from src.review.models import Finding, Hunk, PullRequest

CONTROLLER = """<?php
class Api {
    function run($file) {
        $bailOut = function ($message) {
            echo json_encode(['msg' => $message]);
            exit();
        };

        try {
            $result = Account::open($file);
            $result = json_decode($result, true);
        } catch (Throwable $exc) {
            $bailOut($exc->getMessage());
        }
        return $result['id'];
    }
}
"""

ACCOUNT = """<?php
class Account {
    public static function open($file)
    {
        if (!$file) {
            return '{}';
        }
        return strtoupper($file);
    }
}
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "app" / "Api.php").write_text(CONTROLLER)
    (root / "app" / "Account.php").write_text(ACCOUNT)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    return root, _git(root, "rev-parse", "HEAD")


def _pr(sha: str) -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="", author="a",
        base_ref="main", base_sha="b", head_ref="f", head_sha=sha, state="open",
        hunks=[Hunk(file_path="app/Api.php", old_file_path="app/Api.php", old_start=11,
                    old_count=1, new_start=12, new_count=1,
                    content="@@ -11 +12 @@\n+            $bailOut($exc->getMessage());\n")])


def _finding() -> Finding:
    return Finding(file_path="app/Api.php", line=14, title="Undefined variable $result after the catch",
                   body="After the catch execution continues and $result is unset.",
                   rule_id="defect.x", agent="defect")


def _settings(root: Path):
    return SimpleNamespace(repo_path=lambda slug: root)


def test_candidates_are_nearest_first_and_skip_keywords_definitions_and_comments():
    lines = [
        "// helper($x) is not a call",
        "function declared($a) {",
        "    if (isset($a)) { far($a); }",
        "    $near($a);",
        "    return obj->method($a);",
    ]
    assert ve.callee_candidates(lines, 4) == ["$near", "far", "method"]


def test_body_ends_where_braces_balance_or_indentation_returns():
    lines = CONTROLLER.splitlines()
    at = ve.find_definition_line(lines, "$bailOut")
    assert at == 4
    body = ve.body_from(lines, at)
    assert "exit();" in body and "try {" not in body
    py = ["def f(x):", "    return x", "", "def g():", "    pass"]
    assert ve.body_from(py, 1).splitlines() == ["1: def f(x):", "2:     return x", "3: "]
    assert "[body continues]" in ve.body_from(["function a() {"] + ["x;"] * 50, 1)


def test_the_closure_that_exits_is_in_the_evidence_and_the_prompt(tmp_path):
    root, sha = _repo(tmp_path)
    pr = _pr(sha)
    bundle = ve.gather_evidence([_finding()], pr, app_settings=_settings(root))
    callees = "\n".join(bundle.items[0].callees)
    assert "`$bailOut` is defined at app/Api.php:4" in callees and "exit();" in callees
    # a library or built-in call has no definition in the repository: skipped silently
    assert "json_decode" not in callees and "getMessage" not in callees
    # a method with exactly one definition in the repository is shown from there
    assert "app/Account.php:" in callees and "strtoupper" in callees

    prompt = VerifierAgent._build_verification_prompt([_finding()], AgentContext(pull_request=pr), bundle)
    assert "Bodies of functions called near the line" in prompt
    assert prompt.index("Bodies of functions") < prompt.index("**Claim to check")


def test_a_name_defined_twice_is_not_guessed(tmp_path):
    root, sha = _repo(tmp_path)
    (root / "app" / "Other.php").write_text("<?php\nclass Other {\n  function open($f) { return 1; }\n}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "y")
    sha = _git(root, "rev-parse", "HEAD")
    f = Finding(file_path="app/Api.php", line=9, title="t", body="", rule_id="d.x", agent="defect")
    b = ve.gather_evidence([f], _pr(sha), app_settings=_settings(root))
    assert not any("`open`" in c for c in b.items[0].callees)


def test_no_clone_means_no_callees_and_no_error():
    f = _finding()
    b = ve.gather_evidence([f], _pr("deadbeef"), app_settings=SimpleNamespace(
        repo_path=lambda slug: Path("/nonexistent")))
    assert b.items[0].callees == []


def test_the_system_prompt_asks_for_a_premise_check_and_still_keeps_by_default():
    for needle in ("PREMISE", "ends the process", '"checks"', "KEEP is the default",
                   "A dropped index with no"):
        assert needle in _VERIFIER_SYSTEM
    assert "refuted" in _VERIFIER_SYSTEM and "unclear" in _VERIFIER_SYSTEM


def test_a_reply_with_checks_first_still_parses_keep_and_reasons():
    reply = ('{"checks": {"0": "premise: p | read: 3 | verdict: refuted"}, "keep": [], '
             '"reasons": {"0": "line 6 exits"}}')
    assert VerifierAgent._parse_keep_indices(reply, total=1) == []
    assert VerifierAgent._parse_reasons(reply) == {0: "line 6 exits"}
