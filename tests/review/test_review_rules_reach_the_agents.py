"""Review rules reach the agents a review runs, and their findings come back
tied to the rule.

What is checked, each by behaviour:

  * composition — workspace active rules + repository active rules; a
    repository rule with the same title replaces the workspace one; pending
    and rejected rules never reach a review; another workspace's rules never;
  * targeting — a rule naming agents reaches only their prompts, an
    untargeted one every agent's, and the path glob gates it on the diff;
  * compatibility — a policy without review rules renders byte-for-byte as
    before, so `folder_rules` keep working untouched beside them;
  * citation — the `rule` field the agents are asked for is parsed, kept only
    when it names a rule in force, and takes that rule's severity;
  * `apply_filters_to_rules` — off, a cited finding passes the comment
    threshold and takes no slot of the inline cap; on (the default), the
    filters apply to it like to any other finding.
"""

from __future__ import annotations

import json

import pytest

from src.review.models import Finding, FindingSeverity, Hunk, PullRequest, ReviewBatch
from src.review.policy_rules import (
    REVIEW_RULES_PREAMBLE,
    glob_matches,
    render_policy_rules,
)
from src.review.rules_store import compose_effective
from tests.review.rules_db import rules_db


def _rule(id_: int, title: str, *, status: str = "active", repo: str | None = None,
          **kw) -> dict:
    return {"id": id_, "title": title, "instructions": f"instructions of {title}",
            "status": status, "repo_slug": repo, "severity": kw.pop("severity", "warning"),
            "agents": kw.pop("agents", []), "path_glob": kw.pop("path_glob", ""), **kw}


# ─── composition ─────────────────────────────────────────────────────


def test_a_repo_rule_replaces_the_workspace_rule_of_the_same_title():
    ws = [_rule(1, "Do not ignore exceptions"), _rule(2, "Use strict equality")]
    repo = [_rule(5, "do NOT ignore  exceptions", repo="r", severity="critical"),
            _rule(6, "Repo only", repo="r")]
    out = compose_effective(ws, repo)
    assert [r["id"] for r in out] == [5, 2, 6]
    assert out[0]["severity"] == "critical"


def test_only_active_rules_are_composed():
    ws = [_rule(1, "A", status="pending"), _rule(2, "B", status="rejected"), _rule(3, "C")]
    repo = [_rule(4, "C", repo="r", status="pending")]
    out = compose_effective(ws, repo)
    assert [r["id"] for r in out] == [3], "a pending repo rule must not replace an active one"


async def test_the_review_loads_its_workspace_and_repo_rules_only(tmp_path, monkeypatch):
    from src.review import rules_store

    async with rules_db(tmp_path, monkeypatch):
        await rules_store.create_rule("ws-a", repo_slug=None, title="WS rule",
                                      instructions="ws")
        await rules_store.create_rule("ws-a", repo_slug="acme_app", title="ws rule",
                                      instructions="repo override", severity="error")
        await rules_store.create_rule("ws-a", repo_slug="acme_other", title="Other repo",
                                      instructions="x")
        await rules_store.create_rule("ws-a", repo_slug=None, title="Waiting",
                                      instructions="x", status="pending")
        await rules_store.create_rule("ws-b", repo_slug=None, title="B secret",
                                      instructions="x")
        rules = rules_store.load_active_rules_sync("ws-a", "acme_app")
        assert [(r["title"], r["instructions"]) for r in rules] == [
            ("ws rule", "repo override")]
        assert rules_store.load_active_rules_sync("ws-b", "acme_app")[0]["title"] == "B secret"
        assert rules_store.load_active_rules_sync("", "acme_app") == []


def test_the_orchestrator_puts_the_rules_on_the_policy(monkeypatch):
    from src.review.orchestrator import ReviewOrchestrator

    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    seen = {}
    monkeypatch.setattr(orch, "_load_policy", lambda slug: None, raising=False)
    monkeypatch.setattr(orch, "_load_workspace_defaults", lambda ws: None, raising=False)

    def _rules(ws, slug):
        seen["args"] = (ws, slug)
        return [_rule(1, "A")]

    monkeypatch.setattr(orch, "_load_review_rules", _rules, raising=False)
    policy = orch._resolved_policy("acme_app", "ws-a")
    assert seen["args"] == ("ws-a", "acme_app")
    assert policy["review_rules"][0]["title"] == "A"
    # A repository with no policy and no rules still reads as "no policy".
    monkeypatch.setattr(orch, "_load_review_rules", lambda ws, slug: [], raising=False)
    assert orch._resolved_policy("acme_app", "ws-a") is None


# ─── targeting and globs ─────────────────────────────────────────────


def test_a_targeted_rule_reaches_only_its_agents():
    policy = {"review_rules": [
        _rule(1, "Everyone"), _rule(2, "Security only", agents=["security"]),
    ]}
    out = render_policy_rules(policy, ["src/a.py"])
    assert "Everyone" in out.shared and "Security only" not in out.shared
    assert set(out.per_agent) == {"security"}
    assert "Security only" in out.per_agent["security"]
    assert "Security only" in out.targeted and "Everyone" not in out.targeted
    assert out.shared.startswith(REVIEW_RULES_PREAMBLE)
    assert out.per_agent["security"].startswith(REVIEW_RULES_PREAMBLE)


def test_the_glob_gates_a_rule_on_the_diff():
    policy = {"review_rules": [_rule(1, "Python", path_glob="*.py"),
                               _rule(2, "Vue", path_glob="*.vue")]}
    out = render_policy_rules(policy, ["pkg/mod/a.py"])
    assert "Python" in out.shared and "Vue" not in out.shared
    assert render_policy_rules(policy, ["x.go"]).shared == ""
    every = render_policy_rules(policy, None, match_files=False).everything
    assert "Python" in every and "Vue" in every


@pytest.mark.parametrize("path, glob, hit", [
    ("src/a.ts", "*.{ts,tsx}", True),
    ("src/a.tsx", "src/**/*.{ts,tsx}", True),
    ("setup.py", "**/*.py", True),
    ("a.go", "*.py, *.go", True),
    ("a.go", "*.py", False),
    ("anything", "", True),
])
def test_globs(path, glob, hit):
    assert glob_matches(path, glob) is hit


# ─── compatibility ───────────────────────────────────────────────────


def test_a_policy_without_review_rules_renders_as_before():
    """The legacy rules, byte for byte: no review-rules text appears."""
    policy = {
        "prompt_template": "Be strict.",
        "folder_rules": [
            {"pattern": "src/*", "prompt": "Old rule"},
            {"pattern": "src/*", "prompt": "Targeted", "title": "T",
             "severity_hint": "error", "agents": ["security"]},
        ],
    }
    out = render_policy_rules(policy, ["src/a.py"])
    assert out.shared == (
        "**Repo-level rules (from admin panel):**\nBe strict.\n\n"
        "**Folder rule — `src/*` (matches: src/a.py):**\nOld rule")
    assert out.per_agent == {"security": (
        "**Rule — T** (`src/*` (matches: src/a.py)):\nTargeted\n"
        "Report a violation of this rule with severity `error`.")}
    assert REVIEW_RULES_PREAMBLE not in out.everything
    # With review rules added the folder rules are still there, first.
    both = render_policy_rules({**policy, "review_rules": [_rule(1, "New")]}, ["src/a.py"])
    assert both.shared.startswith(out.shared)
    assert both.shared.endswith("instructions of New")


# ─── citation ────────────────────────────────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open", hunks=[Hunk(file_path="src/foo.py", old_file_path="src/foo.py",
                                  old_start=1, old_count=1, new_start=1, new_count=2,
                                  content="@@ -1 +1,2 @@\n x\n+y\n")])


def test_the_agent_parser_reads_the_rule_field():
    from src.review.agents.base import AgentContext, LLMReviewAgent

    class _Agent(LLMReviewAgent):
        name = "defect"
        severity_default = FindingSeverity.WARNING
        system_prompt = "s"
        user_prompt_template = "{diff}"
        model = "m"

    reply = json.dumps([{
        "reasoning": "x is swallowed on line 2", "file": "src/foo.py", "line": 2,
        "severity": "warning", "title": "swallowed", "body": "b",
        "rule_id": "defect.swallow", "rule": "Do not ignore exceptions",
        "confidence": 0.9,
    }])
    out = _Agent()._parse_findings(reply, AgentContext(pull_request=_pr()))
    assert out[0].rule == "Do not ignore exceptions"
    assert out[0].rule_id == "defect.swallow"


def test_a_citation_counts_only_for_a_rule_in_force():
    from src.review.orchestrator import _cite_review_rules

    policy = {"review_rules": [_rule(1, "Do not ignore exceptions", severity="critical")]}
    cited = Finding(file_path="a", line=1, severity=FindingSeverity.INFO,
                    rule="`do not ignore exceptions.`")
    stray = Finding(file_path="a", line=2, rule="quality.todo")
    out = _cite_review_rules([cited, stray], policy)
    assert out[0].rule == "Do not ignore exceptions"
    assert out[0].severity == FindingSeverity.CRITICAL, "the rule's severity wins"
    assert out[1].rule == ""
    # No rules in force: nothing is a citation.
    lone = Finding(file_path="a", line=3, rule="Do not ignore exceptions")
    assert _cite_review_rules([lone], None)[0].rule == ""


def test_a_merged_duplicate_keeps_the_citation():
    from src.review.agents.verifier import _merge_same_line

    a = Finding(file_path="a", line=1, agent="defect", rule_id="x")
    b = Finding(file_path="a", line=1, agent="security", rule_id="x", rule="R")
    assert _merge_same_line([a, b]).rule == "R"


# ─── apply_filters_to_rules ──────────────────────────────────────────


def _batch(bypass: bool) -> ReviewBatch:
    batch = ReviewBatch(pull_request=_pr())
    batch.comment_min_severity = "error"
    batch.max_inline_comments = 1
    batch.rules_bypass_filters = bypass
    batch.findings = [
        Finding(file_path="a", line=1, severity=FindingSeverity.CRITICAL, title="c"),
        Finding(file_path="a", line=2, severity=FindingSeverity.ERROR, title="e"),
        Finding(file_path="a", line=3, severity=FindingSeverity.INFO, title="r",
                rule="Rule"),
    ]
    return batch


def test_with_filters_on_a_cited_finding_is_filtered_like_any_other():
    batch = _batch(bypass=False)
    assert [f.title for f in batch.inline_findings(20)] == ["c"]
    assert batch.below_threshold_count == 1


def test_with_filters_off_a_cited_finding_passes_threshold_and_cap():
    batch = _batch(bypass=True)
    assert [f.title for f in batch.inline_findings(20)] == ["c", "r"]
    assert batch.below_threshold_count == 0


@pytest.mark.parametrize("setting, bypass", [(None, False), (True, False), (False, True)])
def test_the_orchestrator_reads_apply_filters_to_rules(monkeypatch, setting, bypass):
    """Asked of the real review path: the batch handed to the provider."""
    from src.review.orchestrator import ReviewOrchestrator

    policy = {"enabled": False, "target_branches": None,
              "apply_filters_to_rules": setting}
    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    monkeypatch.setattr(orch, "_resolved_policy", lambda slug, ws: policy, raising=False)

    class _Provider:
        def fetch_pull_request(self, repo, number):
            return _pr()

    result = orch._review_impl("github", "o/r", 1, dry_run=True, post_comments=False,
                               provider=_Provider(), user_id="u", workspace_id="w",
                               t0=0.0)
    assert result.batch.rules_bypass_filters is bypass
