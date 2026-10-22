"""The sentences a program wrote into a finding are said in the review language.

THE DEFECT. A structural rule, a dependency advisory, a breaking-change notice
and a compliance title are templates, and each came out in English under an
otherwise Ukrainian comment. The language check could not fix them and, worse,
could have: a finding a program wrote that is not marked "proven" (a breaking
change, a compliance finding) was sent to the model to be rewritten, title
included, and the title is what the issue fingerprint is made of — the same
finding would have been a new one on the next push.

THE RULE. The finder keeps writing English and the stored finding keeps it,
fingerprint included. The finding also records its template
(`Finding.template`); the comment is written from `finding_text.shown()`, which
swaps in the translation. The language check leaves such a finding alone.
"""

from __future__ import annotations

import dataclasses
import re
from types import SimpleNamespace

import pytest

import src.review.agents  # noqa: F401 — settles an import cycle between the agents and structural
from src.review import finding_text, language
from src.review.agents.verifier import _merge_cluster, _merge_same_line
from src.review.finding_text import shown
from src.review.models import Finding, FindingSeverity, TemplateText
from src.review.providers.base import (
    _format_finding_body,
    _format_unanchored,
    _top_findings_lines,
    finding_fingerprint,
)
from src.review.structural import _RULES, StructuralAgent
from tests.review.test_a_pr_cannot_smuggle_a_known_cve import (
    LOCKFILE_ADD_DIFF,
    LODASH_ADVISORY,
    PACKAGE_JSON_HUNK,
    _hunk,
    _missing_runner,
    _pr,
    _query_with,
    _run,
)
from tests.review.test_the_pr_says_what_it_does import _pr as github_pr
from tests.review.test_undefined_class_constant import _run as undefined_constant

CYRILLIC = re.compile(r"[А-Яа-яІіЇїЄєҐґ]")


class _Match:
    def text(self) -> str:
        return "except:\n    pass"


def _structural(rule) -> Finding:
    return StructuralAgent()._match_to_finding(rule, "app/a.py", 7, _Match())


def _cve() -> Finding:
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)],
             raw_diff=LOCKFILE_ADD_DIFF, skipped=["package-lock.json"])
    result = _run(pr, runner=_missing_runner, osv_query=_query_with(
        {("npm", "lodash", "4.17.15"): [LODASH_ADVISORY]}))
    return result.findings[0]


def _breaking(monkeypatch) -> Finding:
    from src.review import breaking_change

    monkeypatch.setattr(breaking_change, "_find_consumers_across_repos", lambda *_a: {
        "apply_refund": [{"repo_slug": "billing", "file": "api.py", "line": 4,
                          "caller": "charge"}] * 2})
    pr = SimpleNamespace(
        raw_diff="+def apply_refund(order, amount):\n", local_slug="shop", repo="o/shop",
        number=3, title="t", url="", changed_files=["app/refunds.py"])
    context = SimpleNamespace(pull_request=pr, workspace_id="w")
    return breaking_change.run_breaking_change(context).findings[0]


# ─── every template has its translation ─────────────────────────────


@pytest.mark.parametrize("rule", _RULES, ids=lambda r: r.id)
def test_every_structural_rule_is_said_in_ukrainian(rule) -> None:
    finding = _structural(rule)
    uk = shown(finding, "uk")
    assert CYRILLIC.search(uk.title) and CYRILLIC.search(uk.body)
    assert uk.title != finding.title and rule.body not in uk.body
    # The matched line is the author's code, and it stays under the sentence.
    assert uk.body.endswith("```\nexcept: ⏎     pass\n```")
    assert uk.suggestion == finding.suggestion, "a snippet is not translated"


def test_a_translation_is_not_missing_for_a_rule_added_later() -> None:
    assert {f"structural.{r.id}" for r in _RULES} <= set(finding_text.UK)


def test_english_and_unknown_languages_show_the_finders_own_text() -> None:
    finding = _structural(_RULES[0])
    assert shown(finding, None) is finding
    assert shown(finding, "en") is finding
    assert shown(finding, "de") is finding, "no German catalogue: the English stands"


def test_a_finding_without_a_template_is_shown_as_it_is() -> None:
    plain = Finding(file_path="a.py", line=1, title="Off by one", body="Loop bound.")
    assert shown(plain, "uk") is plain


def test_a_template_with_no_entry_or_with_broken_arguments_shows_the_english() -> None:
    orphan = Finding(file_path="a.py", line=1, title="T", body="B",
                     template=TemplateText(key="no.such.key", title="T", body="B"))
    assert shown(orphan, "uk") is orphan
    broken = Finding(file_path="a.py", line=1, title="T", body="B",
                     template=TemplateText(key="structural.undefined-class-constant",
                                           title="T", body="B", args={}))
    assert shown(broken, "uk") is broken


# ─── the other finders ──────────────────────────────────────────────


@pytest.mark.parametrize("snippet, near", [
    ("<?php\n$m->status = Model::STATUS_PENDNG;\n", "STATUS_PENDING"),
    ("<?php\n$m->status = Model::ZZZ_NOTHING_LIKE_IT;\n", ""),
])
def test_an_undefined_class_constant_is_said_in_ukrainian(tmp_path, snippet, near) -> None:
    [finding] = undefined_constant(tmp_path, snippet)
    uk = shown(finding, "uk")
    assert uk.title.startswith("Невизначена константа класу Model::")
    assert "`Model::" in uk.body and "Undefined constant" in uk.body
    assert "Найближча константа" in uk.body if near else "Найближча" not in uk.body
    assert (uk.suggestion == f"Використайте `{near}`.") if near else uk.suggestion is None
    assert "is used here" not in uk.body and "searched" not in uk.body


def test_a_known_vulnerability_is_said_in_ukrainian_with_the_advisory_left_as_published() -> None:
    finding = _cve()
    uk = shown(finding, "uk")
    assert "має відому вразливість" in uk.title and "lodash 4.17.15" in uk.title
    assert "зачеплено вразливістю" in uk.body and "Виправлено у **4.17.21**" in uk.body
    assert LODASH_ADVISORY["summary"] in uk.body, "the advisory is data, not ours"
    assert "carries known vulnerability" not in uk.title and "Fixed in" not in uk.body
    assert uk.suggestion == finding.suggestion, "the fixed manifest line is code"


def test_a_breaking_change_is_said_in_ukrainian(monkeypatch) -> None:
    finding = _breaking(monkeypatch)
    uk = shown(finding, "uk")
    assert uk.title == "Ризик зламу сумісності: apply_refund — споживачів: 2"
    assert "Зміна сигнатури зачіпає `function` **apply_refund**" in uk.body
    assert "`billing` · `api.py:4` — charge" in uk.body, "the consumers are data"
    assert "Signature change" not in uk.body


def test_a_compliance_title_is_said_in_ukrainian_and_the_stored_title_is_not_touched() -> None:
    finding = Finding(
        file_path="a.py", line=1, title="Compliance: No secrets in code", body="b",
        template=TemplateText(key="compliance.rule", title="Compliance: No secrets in code",
                              args={"name": "No secrets in code"}))
    assert shown(finding, "uk").title == "Відповідність: No secrets in code"
    assert finding.title == "Compliance: No secrets in code"


# ─── what the pull request is shown, and what is stored ─────────────


def test_the_comment_is_ukrainian_and_the_fingerprint_is_the_english_ones() -> None:
    finding = _structural(_RULES[0])
    before = finding_fingerprint(finding)
    body = _format_finding_body(finding, "<!--m-->", language="uk", head_sha="a" * 40)
    assert "Порожній except мовчки ковтає помилки" in body
    assert "Empty except clause" not in body
    assert finding_fingerprint(finding) == before
    assert finding.title == _RULES[0].title, "the stored finding is not changed by showing it"
    assert before[0] in body, "the marker carries the English title's fingerprint"
    english = _format_finding_body(finding, "<!--m-->", language="en", head_sha="a" * 40)
    assert "Empty except clause silently swallows errors" in english and before[0] in english


def test_the_summary_lists_the_title_in_the_review_language() -> None:
    from src.review.models import ReviewBatch

    finding = _structural(_RULES[0])
    batch = ReviewBatch(pull_request=github_pr("github", "o/r", 1), findings=[finding])
    assert "Порожній except мовчки ковтає помилки" in "\n".join(_top_findings_lines(batch, "uk"))
    assert "Empty except clause" in "\n".join(_top_findings_lines(batch, "en"))


def test_a_comment_the_provider_refused_is_folded_into_the_summary_in_the_review_language() -> None:
    finding = _structural(_RULES[1])
    text = _format_unanchored([finding], github_pr("github", "o/r", 1), "uk")
    assert "Голий `except:` ловить усе" in text and "Bare `except:`" not in text


def test_a_merge_keeps_what_the_other_agent_said_around_the_template() -> None:
    template = _structural(_RULES[0])
    other = Finding(file_path="app/a.py", line=7, severity=FindingSeverity.WARNING,
                    title="Errors are swallowed", body="The handler hides failures.",
                    agent="defect", rule_id="defect.swallow", confidence=0.8)
    merged = _merge_same_line([template, other])
    assert merged.title == template.title and merged.template is template.template
    uk = shown(merged, "uk")
    assert uk.title == "Порожній except мовчки ковтає помилки"
    assert "`except` лише з `pass` ховає баги" in uk.body
    assert "[defect] The handler hides failures." in uk.body, "the other agent's words stay"
    assert "Catching an exception with only" not in uk.body


def test_a_cluster_of_the_same_rule_at_several_lines_is_still_said_in_ukrainian() -> None:
    first = _structural(_RULES[0])
    again = dataclasses.replace(first, line=19)
    merged = _merge_cluster([first, again], "uk")
    uk = shown(merged, "uk")
    assert uk.title == "Порожній except мовчки ковтає помилки"
    assert "Також у рядку 19." in uk.body, "the merge's own sentence was already Ukrainian"
    assert "`except` лише з `pass` ховає баги" in uk.body


def test_a_merge_led_by_a_model_finding_loses_nothing() -> None:
    template = _structural(_RULES[0])
    model = Finding(file_path="app/a.py", line=7, severity=FindingSeverity.ERROR,
                    title="Errors are swallowed", body="The handler hides failures.",
                    agent="defect", rule_id="defect.swallow", confidence=0.9)
    merged = _merge_same_line([template, model])
    assert merged.template is None
    assert shown(merged, "uk") is merged


# ─── the language check leaves them alone ───────────────────────────


def test_the_language_check_never_rewrites_a_template_finding_or_its_title() -> None:
    breaking = Finding(
        file_path="app/refunds.py", line=1, severity=FindingSeverity.ERROR,
        title="Breaking change risk: apply_refund — 2 consumer(s)",
        body="Signature change touches the function apply_refund and other services call it.",
        agent="breaking_change", rule_id="bc.apply_refund",
        template=TemplateText(key="breaking_change.risk", title="x", body="y"))
    compliance = dataclasses.replace(
        breaking, title="Compliance: Public functions need docstrings",
        rule_id="compliance.docs", agent="compliance",
        template=TemplateText(key="compliance.rule", title="x"))
    asked: list[str] = []

    assert language.stray_fields(breaking, "uk"), "it does read as foreign to the check"
    done = language.restore_language(
        [breaking, compliance], "uk", lambda system, prompt: asked.append(prompt) or "[]")
    assert asked == [] and done.asked == 0
    assert breaking.title.startswith("Breaking change risk")
    assert compliance.title == "Compliance: Public functions need docstrings"


def test_a_model_finding_that_reads_as_foreign_is_still_sent_back() -> None:
    model = Finding(
        file_path="a.py", line=1, severity=FindingSeverity.ERROR, title="Errors are swallowed",
        body="The handler hides failures and the caller never learns about them.",
        agent="defect", rule_id="defect.swallow")
    asked: list[str] = []
    language.restore_language(
        [model], "uk", lambda system, prompt: asked.append(prompt) or "[]")
    assert len(asked) == 1
