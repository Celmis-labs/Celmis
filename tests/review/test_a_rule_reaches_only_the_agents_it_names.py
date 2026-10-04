"""Structured repo rules and the per-repo output settings, at review time.

The API half is tests/api/test_a_repo_customises_its_review.py; this is what
the orchestrator and the agents do with what was stored.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.review.agents.base import AgentContext, custom_rules_for
from src.review.models import Finding, FindingSeverity, PullRequest, ReviewBatch
from src.review.orchestrator import ReviewOrchestrator
from src.review.policy_rules import render_policy_rules


def _pr(files: list[str]):
    """All the rule renderer reads of a pull request is its changed files."""
    return SimpleNamespace(changed_files=files)


def _real_pr() -> PullRequest:
    return PullRequest(provider="github", repo="acme/api", number=1, title="t",
                       description="", author="a", base_ref="main", base_sha="",
                       head_ref="f", head_sha="", state="open", url="")


def test_a_legacy_rule_renders_exactly_as_before():
    """Byte-for-byte the shape `_build_custom_rules` always produced, so a
    policy that uses none of the new fields reviews as it did."""
    policy = {"prompt_template": "Be strict.",
              "folder_rules": [{"pattern": "src/*.py", "prompt": "No prints."}]}
    text = ReviewOrchestrator.__new__(ReviewOrchestrator)._build_custom_rules(
        _pr(["src/a.py"]), policy)
    assert text == (
        "**Repo-level rules (from admin panel):**\nBe strict.\n\n"
        "**Folder rule — `src/*.py` (matches: src/a.py):**\nNo prints."
    )


def test_a_targeted_rule_is_kept_out_of_the_shared_block():
    policy = {"folder_rules": [
        {"pattern": "api/**", "prompt": "AUTH", "agents": ["security"],
         "title": "Auth", "severity_hint": "error"},
        {"pattern": "**", "prompt": "SHARED"},
    ]}
    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    pr = _pr(["api/users.py"])
    shared = orch._build_custom_rules(pr, policy)
    per_agent = orch._build_agent_custom_rules(pr, policy)
    assert "SHARED" in shared and "AUTH" not in shared
    assert set(per_agent) == {"security"}
    assert "AUTH" in per_agent["security"] and "severity `error`" in per_agent["security"]

    ctx = AgentContext(pull_request=pr, custom_rules=shared, agent_custom_rules=per_agent)
    assert "AUTH" in custom_rules_for(ctx, "security")
    assert "AUTH" not in custom_rules_for(ctx, "defect")
    assert "SHARED" in custom_rules_for(ctx, "defect")


def test_a_targeted_rule_still_needs_a_matching_file():
    policy = {"folder_rules": [
        {"pattern": "api/**", "prompt": "AUTH", "agents": ["security"]},
    ]}
    assert render_policy_rules(policy, ["web/app.tsx"]).per_agent == {}
    assert "AUTH" in render_policy_rules(policy, None, match_files=False).per_agent["security"]


def test_the_single_reviewer_engine_gets_every_rule_once():
    policy = {"folder_rules": [
        {"pattern": "**", "prompt": "BOTH", "agents": ["security", "defect"]},
    ]}
    rendered = render_policy_rules(policy, ["x.py"])
    assert rendered.targeted.count("BOTH") == 1
    assert rendered.shared == ""


def _finding(n: int) -> Finding:
    return Finding(agent="defect", file_path=f"f{n}.py", line=n,
                   severity=FindingSeverity.WARNING, title=f"t{n}", body="b")


def test_the_repo_inline_cap_wins_over_the_install_default():
    batch = ReviewBatch(pull_request=_real_pr())
    batch.findings = [_finding(n) for n in range(10)]
    assert len(batch.inline_findings(20)) == 10
    batch.max_inline_comments = 3
    assert len(batch.inline_findings(20)) == 3
    assert batch.inline_cap(20) == 3


def test_the_policy_loader_reads_the_output_settings():
    """`_load_policy` is where the orchestrator learns them; the switches
    read NULL as ON."""
    import inspect

    src = inspect.getsource(ReviewOrchestrator._load_policy)
    for key in ("summary_enabled", "summary_instructions", "started_comment_enabled",
                "review_language", "max_inline_comments"):
        assert f'"{key}"' in src, key


def test_a_repo_language_beats_the_workspace_one(monkeypatch):
    from src.review.agents import base

    monkeypatch.setattr(
        "src.api.routers.llm._load_workspace_config",
        lambda workspace_id="default": {"review_language": "de"},
    )
    assert "German" in base._review_language_instruction("ws")
    assert "Ukrainian" in base._review_language_instruction("ws", "uk")
    assert base._review_language_instruction("ws", "en") == ""
