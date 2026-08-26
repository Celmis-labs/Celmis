"""Tests для agent base + Verifier (LLM call mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.review.agents.base import (
    AgentContext,
    LLMReviewAgent,
)
from src.review.agents.verifier import VerifierAgent
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
)


def _make_pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1,
        title="Add feature", description="Description",
        author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[
            Hunk(
                file_path="src/foo.py", old_file_path="src/foo.py",
                old_start=1, old_count=2, new_start=1, new_count=4,
                content="@@ -1,2 +1,4 @@\n line\n+added1\n+added2\n",
            ),
        ],
    )


# ─── _extract_json_array ────────────────────────────────────────────


class TestJsonExtraction:
    def test_bare_array(self) -> None:
        text = '[{"file": "a.py", "line": 1}]'
        assert LLMReviewAgent._extract_json_array(text) == text

    def test_fenced_json_block(self) -> None:
        text = '```json\n[{"file": "a.py"}]\n```'
        result = LLMReviewAgent._extract_json_array(text)
        assert result is not None
        assert '"file": "a.py"' in result

    def test_fenced_no_lang(self) -> None:
        text = '```\n[{"file": "a.py"}]\n```'
        result = LLMReviewAgent._extract_json_array(text)
        assert result is not None

    def test_no_array(self) -> None:
        assert LLMReviewAgent._extract_json_array("plain text") is None

    def test_empty(self) -> None:
        assert LLMReviewAgent._extract_json_array("") is None


# ─── _parse_findings ────────────────────────────────────────────────


class _DummyAgent(LLMReviewAgent):
    name = "dummy"
    severity_default = FindingSeverity.WARNING
    system_prompt = "test"
    user_prompt_template = "{diff}"
    model = "test-model"


class TestParseFindings:
    def test_valid_json_array(self) -> None:
        agent = _DummyAgent()
        text = """[
            {"reasoning": "line 5 reads x before it is assigned",
             "file": "src/foo.py", "line": 5, "severity": "error",
             "title": "Bug", "body": "Details", "rule_id": "r.x", "confidence": 0.9}
        ]"""
        ctx = AgentContext(pull_request=_make_pr())
        findings = agent._parse_findings(text, ctx)
        assert len(findings) == 1
        f = findings[0]
        assert f.file_path == "src/foo.py"
        assert f.line == 5
        assert f.severity == FindingSeverity.ERROR
        assert f.agent == "dummy"  # provenance applied

    def test_invalid_severity_falls_back_to_default(self) -> None:
        agent = _DummyAgent()
        text = '[{"reasoning": "r", "file": "src/foo.py", "line": 1, "severity": "blocker"}]'
        ctx = AgentContext(pull_request=_make_pr())
        findings = agent._parse_findings(text, ctx)
        assert findings[0].severity == FindingSeverity.WARNING

    def test_negative_line_skipped(self) -> None:
        agent = _DummyAgent()
        text = '[{"reasoning": "r", "file": "src/foo.py", "line": -1}]'
        ctx = AgentContext(pull_request=_make_pr())
        findings = agent._parse_findings(text, ctx)
        assert findings == []

    def test_missing_file_skipped(self) -> None:
        agent = _DummyAgent()
        text = '[{"line": 5, "body": "no file"}]'
        ctx = AgentContext(pull_request=_make_pr())
        findings = agent._parse_findings(text, ctx)
        assert findings == []

    def test_confidence_clamped(self) -> None:
        agent = _DummyAgent()
        text = '[{"reasoning": "r", "file": "src/foo.py", "line": 1, "confidence": 5.0}]'
        ctx = AgentContext(pull_request=_make_pr())
        findings = agent._parse_findings(text, ctx)
        assert findings[0].confidence == 1.0

    def test_garbage_text_is_reported_not_swallowed(self) -> None:
        """This test used to assert `== []` for both, which is how the silent
        auto-approve survived: a reply we could not read looked identical to a
        reply that found nothing, and "found nothing" is an APPROVE. The
        prompt says *if nothing is found — `[]`*, so an absent array is a
        protocol breach and has to reach `AgentRunResult.error`."""
        import pytest

        from src.review.agents.base import AgentReplyUnreadable

        agent = _DummyAgent()
        ctx = AgentContext(pull_request=_make_pr())
        with pytest.raises(AgentReplyUnreadable):
            agent._parse_findings("not json", ctx)
        with pytest.raises(AgentReplyUnreadable):
            agent._parse_findings("", ctx)
        # The one that genuinely means "clean" still does.
        assert agent._parse_findings("[]", ctx) == []


# ─── Agent run з mocked LLM ─────────────────────────────────────────


class TestAgentRun:
    def test_successful_review(self) -> None:
        mock_response = MagicMock()
        mock_response.text = (
            '[{"reasoning": "line 2 drops the return value", '
            '"file": "src/foo.py", "line": 2, "severity": "warning", '
            '"title": "Test", "body": "issue", "rule_id": "r.x"}]'
        )
        mock_response.input_tokens = 100
        mock_response.output_tokens = 50

        # The fallback builds a gateway client now; the old direct path is
        # gone, so the mock has to follow it.
        with patch("src.llm.client.build_llm_client") as mock_get:
            client = MagicMock()
            client.generate.return_value = mock_response
            mock_get.return_value = client

            agent = _DummyAgent()
            result = agent.review(AgentContext(pull_request=_make_pr()))

        assert result.error is None
        assert len(result.findings) == 1
        assert result.tokens_in == 100
        assert result.tokens_out == 50
        assert result.findings[0].agent == "dummy"

    def test_llm_exception_caught(self) -> None:
        # The fallback builds a gateway client now; the old direct path is
        # gone, so the mock has to follow it.
        with patch("src.llm.client.build_llm_client") as mock_get:
            client = MagicMock()
            client.generate.side_effect = RuntimeError("API down")
            mock_get.return_value = client

            agent = _DummyAgent()
            result = agent.review(AgentContext(pull_request=_make_pr()))

        assert result.error is not None
        assert "API down" in result.error
        assert result.findings == []

    def test_empty_response_no_findings(self) -> None:
        mock_response = MagicMock()
        mock_response.text = "[]"
        mock_response.input_tokens = 50
        mock_response.output_tokens = 5

        # The fallback builds a gateway client now; the old direct path is
        # gone, so the mock has to follow it.
        with patch("src.llm.client.build_llm_client") as mock_get:
            client = MagicMock()
            client.generate.return_value = mock_response
            mock_get.return_value = client

            agent = _DummyAgent()
            result = agent.review(AgentContext(pull_request=_make_pr()))

        assert result.findings == []
        assert result.error is None


# ─── Verifier dedup ─────────────────────────────────────────────────


class TestVerifierDedup:
    def test_unique_findings_kept(self) -> None:
        verifier = VerifierAgent(confidence_threshold=0.0)
        findings = [
            Finding(file_path="a.py", line=1, rule_id="r1", confidence=0.8),
            Finding(file_path="a.py", line=2, rule_id="r2", confidence=0.8),
            Finding(file_path="b.py", line=1, rule_id="r1", confidence=0.8),
        ]
        ctx = AgentContext(pull_request=_make_pr())
        result = verifier.verify(findings, ctx)
        assert len(result.kept) == 3
        assert result.dropped_dedup == 0

    def test_duplicate_findings_merged(self) -> None:
        verifier = VerifierAgent(confidence_threshold=0.0)
        findings = [
            Finding(
                file_path="a.py", line=1, rule_id="r.x",
                agent="architect", severity=FindingSeverity.WARNING,
                body="from architect", confidence=0.8,
            ),
            Finding(
                file_path="a.py", line=1, rule_id="r.x",
                agent="security", severity=FindingSeverity.ERROR,
                body="from security", confidence=0.8,
            ),
        ]
        ctx = AgentContext(pull_request=_make_pr())
        result = verifier.verify(findings, ctx)
        assert len(result.kept) == 1
        assert result.dropped_dedup == 1
        # Most-severe wins
        assert result.kept[0].severity == FindingSeverity.ERROR
        # Both agents listed
        assert "architect" in result.kept[0].agent
        assert "security" in result.kept[0].agent

    def test_low_confidence_filtered(self) -> None:
        verifier = VerifierAgent(confidence_threshold=0.5)
        findings = [
            Finding(file_path="a.py", line=1, rule_id="r1", confidence=0.9),
            Finding(file_path="a.py", line=2, rule_id="r2", confidence=0.3),
            Finding(file_path="a.py", line=3, rule_id="r3", confidence=0.7),
        ]
        ctx = AgentContext(pull_request=_make_pr())
        result = verifier.verify(findings, ctx)
        assert len(result.kept) == 2
        assert result.dropped_low_confidence == 1

    def test_severity_sort_critical_first(self) -> None:
        verifier = VerifierAgent(confidence_threshold=0.0)
        findings = [
            Finding(file_path="a.py", line=1, rule_id="r1",
                    severity=FindingSeverity.INFO, confidence=0.9),
            Finding(file_path="a.py", line=2, rule_id="r2",
                    severity=FindingSeverity.CRITICAL, confidence=0.9),
            Finding(file_path="a.py", line=3, rule_id="r3",
                    severity=FindingSeverity.WARNING, confidence=0.9),
        ]
        ctx = AgentContext(pull_request=_make_pr())
        result = verifier.verify(findings, ctx)
        assert result.kept[0].severity == FindingSeverity.CRITICAL
        assert result.kept[-1].severity == FindingSeverity.INFO

    def test_small_batch_skips_llm_verify(self) -> None:
        """< 3 findings — LLM verifier skipped."""
        verifier = VerifierAgent(confidence_threshold=0.0)
        findings = [
            Finding(file_path="a.py", line=1, rule_id="r", confidence=0.9),
            Finding(file_path="a.py", line=2, rule_id="r2", confidence=0.9),
        ]
        ctx = AgentContext(pull_request=_make_pr())
        # No LLM mock — must not be called
        result = verifier.verify(findings, ctx)
        assert len(result.kept) == 2
        assert result.dropped_llm_filter == 0


# ─── _parse_keep_indices ────────────────────────────────────────────


class TestVerifierParseKeep:
    def test_valid_keep_array(self) -> None:
        text = '{"keep": [0, 2, 3], "reasons": {"1": "FP"}}'
        result = VerifierAgent._parse_keep_indices(text, total=5)
        assert result == [0, 2, 3]

    def test_fallback_bare_array(self) -> None:
        text = "Keeping [0, 1] for review."
        result = VerifierAgent._parse_keep_indices(text, total=3)
        assert 0 in result
        assert 1 in result

    def test_garbage_fails_open(self) -> None:
        """The parser now says None for "could not read this", and the caller
        keeps every finding on None. The observable behaviour is what it always
        was — an unreadable verifier must never silently delete a real finding.

        It returned list(range(total)) before, which made an unreadable reply
        indistinguishable from `{"keep": []}` — the verifier saying none of
        these findings are real. That verdict was therefore impossible to act
        on: the agent whose whole job is removing false positives could not
        remove all of them.
        """
        assert VerifierAgent._parse_keep_indices("nothing parseable", total=3) is None
        # …and the distinction the None exists for:
        assert VerifierAgent._parse_keep_indices('{"keep": []}', total=3) == []
