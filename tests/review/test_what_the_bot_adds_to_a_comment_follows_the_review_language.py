"""The few words the bot adds around a model's sentences are in the review language.

THE DEFECT. A Ukrainian review still ended some comments in English: the
sentence a merged comment gets ("Also at lines 88, 120."), the title of a
comment two agents wrote together, the note under a performance finding whose
severity was lowered, and the labels of a compliance verdict. Each was a literal
inside the code that builds it, so the message catalogue (English and
Ukrainian, kept complete) never saw it.

Also pinned here, because it is the same change: a cross-agent merge keeps the
two most severe "why" sentences and not all of them, and the missing-test gate
reads a Ukrainian sentence too (it only read English, so the moment the model
obeyed the language setting its complaints about tests got through).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.review import messages
from src.review.agents.base import AgentContext, reads_as_a_coverage_claim
from src.review.agents.performance import DOWNGRADE_NOTE, PerformanceAgent
from src.review.agents.verifier import prefilter
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest
from src.review.settings import AgentLLMSettings

# ─── a merged comment ────────────────────────────────────────────────


def _f(file, line, *, rule="defect.r", title="Same defect everywhere", body="b",
       severity=FindingSeverity.WARNING, agent="defect", reasoning="",
       confidence=0.8) -> Finding:
    return Finding(
        file_path=file, line=line, rule_id=rule, title=title, body=body,
        severity=severity, agent=agent, reasoning=reasoning, confidence=confidence,
    )


def test_a_merge_inside_one_file_says_where_else_in_the_review_language():
    findings = [_f("a.py", 10), _f("a.py", 88), _f("a.py", 120)]
    english = prefilter(findings).kept[0].body
    ukrainian = prefilter(findings, language="uk").kept[0].body
    assert english.endswith("Also at lines 88, 120.")
    assert ukrainian.endswith("Також у рядках 88, 120.")
    one = prefilter(findings[:2], language="uk").kept[0].body
    assert one.endswith("Також у рядку 88.")


def test_a_merge_across_files_names_every_place_in_the_review_language():
    findings = [_f("a.py", 10), _f("b.py", 20)]
    text = prefilter(findings, language="uk").kept[0].body
    assert text.endswith("Той самий дефект (2): a.py:10, b.py:20.")
    assert "locations" not in text


def test_a_language_without_a_catalogue_reads_the_english_sentence():
    findings = [_f("a.py", 10), _f("a.py", 88)]
    assert prefilter(findings, language="de").kept[0].body.endswith("Also at line 88.")


def test_two_agents_on_one_line_with_no_title_get_a_title_in_the_language():
    findings = [
        _f("a.py", 5, rule="sec.x", title="", agent="security"),
        _f("a.py", 5, rule="sec.x", title="", agent="defect"),
    ]
    assert prefilter(findings, language="uk").kept[0].title == messages.t(
        "merge.several_agents", "uk")
    assert prefilter(findings).kept[0].title == "Multiple agents flagged this line"


def test_a_cross_agent_merge_keeps_the_two_worst_whys_and_no_more():
    title = "Reflected XSS via request referer in the inline script tag"
    findings = [
        _f("a.py", 5, rule="quality.x", title=title, agent="quality",
           severity=FindingSeverity.INFO, reasoning="the info why"),
        _f("a.py", 5, rule="sec.x", title=title, agent="security",
           severity=FindingSeverity.CRITICAL, reasoning="the critical why"),
        _f("a.py", 5, rule="arch.x", title=title, agent="architect",
           severity=FindingSeverity.ERROR, reasoning="the error why"),
    ]
    [kept] = prefilter(findings).kept
    assert kept.reasoning == "the critical why. the error why."
    assert "info why" not in kept.reasoning


# ─── a lowered severity ──────────────────────────────────────────────

_DIFF = (
    "@@ -10,3 +10,6 @@ def totals(orders):\n"
    "     out = []\n"
    "+    for order in orders:\n"
    "+        user = db.query(User).get(order.user_id)\n"
    "+        out.append((user.name, order.total))\n"
    "     return out\n"
)


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/shop", number=12, title="Show buyer names",
        description="Adds the buyer name to every row.", author="a", base_ref="main",
        base_sha="x", head_ref="feat", head_sha="y", state="open",
        hunks=[Hunk(file_path="app/report.py", old_file_path="app/report.py",
                    old_start=10, old_count=3, new_start=10, new_count=6, content=_DIFF)],
    )


_ITEM = {
    "reasoning": "Line 12 runs a query per order inside the loop on line 11.",
    "file": "app/report.py", "line": 12, "severity": "error",
    "title": "N+1 query", "body": "Load the users with one `IN` query.",
    "rule_id": "perf.n-plus-one", "confidence": 0.85,
}


def _perf(language: str | None):
    client = MagicMock()
    client.generate.return_value = SimpleNamespace(
        text=json.dumps([_ITEM]), input_tokens=1, output_tokens=1, cost_usd=0.0,
        cost_source="litellm_estimate", model="m")
    context = AgentContext(
        pull_request=_pr(), llm_client=client, review_language=language,
        workspace_id="ws-lang",
        agent_llm={"performance": AgentLLMSettings(
            model="m", max_output_tokens=1000, reasoning=None)})
    [finding] = PerformanceAgent().review(context).findings
    return finding


def test_the_note_under_a_lowered_finding_is_in_the_review_language():
    english, ukrainian = _perf("en"), _perf("uk")
    assert english.severity is ukrainian.severity is FindingSeverity.WARNING
    assert english.body.endswith(DOWNGRADE_NOTE)
    assert ukrainian.body.endswith(messages.t("perf.downgrade", "uk"))
    assert "Severity lowered" not in ukrainian.body


def test_the_input_size_field_stays_english_for_the_pattern_that_reads_it():
    prompt = PerformanceAgent.system_prompt
    assert "write it in English" in " ".join(prompt.split())


# ─── a compliance verdict ────────────────────────────────────────────


def _compliance(monkeypatch, language: str | None, *, blocking: bool):
    from src.review import compliance

    spec = compliance.ComplianceCheckSpec(
        id="no-secrets", name="No secrets", scope="workspace", glob_pattern="**",
        rule="No hardcoded secrets", severity="error", blocking=blocking)
    seen: list[dict] = []

    def generate(**kw):
        seen.append(kw)
        return SimpleNamespace(
            text='{"passes": false, "reason": "line 3 holds a literal key"}',
            input_tokens=1, output_tokens=1)

    context = AgentContext(
        pull_request=_pr(), llm_client=SimpleNamespace(generate=generate),
        review_language=language, workspace_id="ws-lang",
        agent_llm={"compliance": AgentLLMSettings(
            model="m", max_output_tokens=256, reasoning=None)})
    monkeypatch.setattr(compliance, "load_active_checks", lambda repo: [spec])
    [finding] = compliance.run_compliance(context).findings
    return finding, seen


def test_a_compliance_finding_is_labelled_in_the_review_language(monkeypatch):
    finding, seen = _compliance(monkeypatch, "uk", blocking=True)
    assert finding.body == (
        "**Правило** (блокує): No hardcoded secrets\n\n"
        "**Причина:** line 3 holds a literal key")
    assert "Ukrainian" in seen[0]["system_instruction"]


def test_a_compliance_finding_in_english_is_short(monkeypatch):
    finding, seen = _compliance(monkeypatch, "en", blocking=False)
    assert finding.body == (
        "**Rule**: No hardcoded secrets\n\n**Reason:** line 3 holds a literal key")
    assert "OUTPUT LANGUAGE" not in seen[0]["system_instruction"]


def test_the_compliance_title_does_not_move_with_the_language(monkeypatch):
    """It is part of the finding's fingerprint: the same finding in a review
    that changed language must not become a new one."""
    uk = _compliance(monkeypatch, "uk", blocking=True)[0].title
    en = _compliance(monkeypatch, "en", blocking=True)[0].title
    assert uk == en == "Compliance: No secrets"


# ─── the missing-test gate reads Ukrainian ───────────────────────────


@pytest.mark.parametrize("sentence", [
    "Немає тестів для нової гілки помилки",
    "Без тестів на цей шлях код ламається непомітно",
    "Бракує покриття тестами для обробки відмови",
    "Гілка помилки не протестована",
    "Цей шлях ніколи не тестується",
    "Метод не покритий тестами",
    "Тест не перевіряє нове значення",
    "Клієнт замоканий у всіх тестах",
])
def test_a_missing_test_complaint_in_ukrainian_is_not_a_finding(sentence):
    assert reads_as_a_coverage_claim(sentence)


@pytest.mark.parametrize("sentence", [
    "Немає перевірки на null перед розіменуванням у рядку 42",
    "Без тестового режиму сервер падає на старті",
    "Тестовий режим вмикає заглушку платежів у проді",
    "Змінна x не ініціалізована до читання в рядку 5",
    "У функції немає обробки помилки мережі",
    "Метод не повертає значення в гілці else",
    "Метод замоканий для виклику зовнішнього API лише тут",
])
def test_a_real_defect_in_ukrainian_survives_the_words_it_happens_to_use(sentence):
    assert reads_as_a_coverage_claim(sentence) is None
