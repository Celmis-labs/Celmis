"""Tests for src.review.structural — ast-grep-driven rule pack."""

from __future__ import annotations

from src.review.agents.base import AgentContext
from src.review.models import FindingSeverity, Hunk, PullRequest
from src.review.structural import StructuralAgent, list_rules


def _pr(file_path: str, hunk_content: str, *, new_start: int = 1,
        new_count: int | None = None) -> PullRequest:
    if new_count is None:
        # Approximate: count lines that are kept on new side
        new_count = sum(
            1 for line in hunk_content.splitlines()
            if line and not line.startswith("@@") and not line.startswith("-")
        )
    h = Hunk(
        file_path=file_path, old_file_path=file_path,
        old_start=new_start, old_count=new_count,
        new_start=new_start, new_count=new_count,
        content=hunk_content,
    )
    return PullRequest(
        provider="github", repo="x/y", number=1, title="t", description="",
        author="me", base_ref="main", base_sha="x", head_ref="b", head_sha="y",
        state="open", hunks=[h],
    )


def _findings(pr: PullRequest):
    return StructuralAgent().review(AgentContext(pull_request=pr)).findings


def test_rule_pack_non_empty():
    rules = list_rules()
    assert len(rules) >= 8
    # Every rule has required fields
    for r in rules:
        assert r.id and r.title and r.body and r.language
        assert isinstance(r.rule, dict) and r.rule


def test_python_mutable_default_arg():
    diff = """@@ -1,1 +1,2 @@
 x = 1
+def f(items=[]):
+    items.append(1)
"""
    findings = _findings(_pr("src/x.py", diff))
    rule_ids = [f.rule_id for f in findings]
    assert "structural.py.mutable-default-arg" in rule_ids
    f = next(f for f in findings if f.rule_id == "structural.py.mutable-default-arg")
    assert f.severity == FindingSeverity.ERROR
    assert f.confidence == 1.0
    assert f.agent == "structural"


def test_python_empty_except_fires():
    diff = """@@ -1,1 +1,5 @@
 x = 1
+try:
+    do()
+except Exception:
+    pass
"""
    findings = _findings(_pr("src/x.py", diff))
    assert any(f.rule_id == "structural.py.empty-except" for f in findings)


def test_python_empty_except_skipped_with_logging():
    """Should NOT fire if except has a call (e.g. logger.exception)."""
    diff = """@@ -1,1 +1,5 @@
 x = 1
+try:
+    do()
+except Exception:
+    logger.exception('failed')
"""
    findings = _findings(_pr("src/x.py", diff))
    assert not any(f.rule_id == "structural.py.empty-except" for f in findings)


def test_python_bare_except():
    diff = """@@ -1,1 +1,5 @@
 x = 1
+try:
+    do()
+except:
+    return
"""
    findings = _findings(_pr("src/x.py", diff))
    assert any(f.rule_id == "structural.py.bare-except" for f in findings)


def test_typescript_console_log():
    diff = """@@ -1,1 +1,3 @@
 export const x = 1;
+console.log('debug');
"""
    findings = _findings(_pr("web/init.ts", diff))
    assert any(f.rule_id == "structural.js.console-log" for f in findings)


def test_typescript_loose_equality():
    diff = """@@ -1,1 +1,3 @@
 const x = 1;
+if (x == null) return;
"""
    findings = _findings(_pr("web/init.ts", diff))
    assert any(f.rule_id == "structural.js.loose-equality" for f in findings)


def test_typescript_await_in_foreach():
    diff = """@@ -1,1 +1,5 @@
 const items = [];
+items.forEach(async (x) => {
+    await fetch(x);
+});
"""
    findings = _findings(_pr("web/init.ts", diff))
    assert any(f.rule_id == "structural.js.await-in-foreach" for f in findings)
    f = next(f for f in findings if f.rule_id == "structural.js.await-in-foreach")
    assert f.severity == FindingSeverity.ERROR


def test_unsupported_language_skipped():
    """File with no rules-mapped language returns no findings, no error."""
    diff = """@@ -1,1 +1,2 @@
 some text
+more text
"""
    pr = _pr("README.md", diff)
    res = StructuralAgent().review(AgentContext(pull_request=pr))
    assert res.error is None
    assert res.findings == []


def test_only_changed_lines_flagged():
    """Issues in context (unchanged) lines should NOT be flagged."""
    diff = """@@ -1,3 +1,4 @@
 def f(x=[]):
     pass
+x = 1
"""
    findings = _findings(_pr("src/x.py", diff))
    # Mutable default arg is on context line 1 (` ` prefix), not added.
    # Should be skipped because we filter to PR-added lines.
    assert not any(f.rule_id == "structural.py.mutable-default-arg" for f in findings)


def test_orchestrator_includes_structural_by_default():
    from src.review.orchestrator import ReviewOrchestrator
    orch = ReviewOrchestrator()
    names = [a.name for a in orch.agents]
    assert "structural" in names
