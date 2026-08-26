"""Three surfaces, three different answers to "what language is this in".

They are genuinely different questions and the product gets them wrong in
different ways, so they are pinned together where the difference is visible:

  documentation  → the workspace setting. A repository's docs belong to the
                   repository, not to whoever pressed Generate.
  Q&A answers    → the language of the question. Somebody who asks in German is
                   asking to be answered in German, and a workspace-wide
                   default would be wrong for whoever else is in the room.
  PR comments    → the review_language setting. Read by outside contributors on
                   GitHub, so the team's own language is often the wrong one.

Every prompt used to hardcode Ukrainian, which answered all three at once and
all three incorrectly.

The verifier is the exception that has to stay an exception: its output is
parsed as JSON and never shown to anyone, so telling it to write in Ukrainian
would corrupt a machine-read response. It works today by omission rather than
by decision, which is exactly the kind of thing somebody tidies up.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

CYRILLIC = re.compile(r"[а-яїієґА-ЯЇІЄҐ]")


def _prompt_strings(path: Path) -> list[str]:
    """Every string literal in the file that is not a docstring."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
               and isinstance(body[0].value, ast.Constant) \
               and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


# ─── Q&A: the question's language ────────────────────────────────────

QA_PROMPTS = ["ba_answer.py", "technical_answer.py", "overview.py"]


@pytest.mark.parametrize("module", QA_PROMPTS)
def test_a_chat_answer_follows_the_question(module):
    """The answer's language is tied to the question, not to a setting and not
    to the language of the surrounding context.

    This used to pin the rule's exact sentence — "answer in the SAME LANGUAGE
    the question is asked in" — and broke when that sentence was rewritten to
    stop naming German as its only worked example, which was itself the fix
    for an English question coming back in German on production. A test that
    fails when the code improves is keyed on the wrong thing.

    Keyed now on what any correct phrasing must contain: somewhere in the
    prompt, the answer's LANGUAGE is spoken about in the same breath as the
    QUESTION. Read from the AST constants, so the comment that explains all of
    this cannot satisfy the assertion by accident.
    """
    joined = "\n\n".join(_prompt_strings(SRC / "llm" / "prompts" / module))
    tied = [
        block for block in joined.split("\n\n")
        if "language" in block.lower() and "question" in block.lower()
    ]
    assert tied, (
        f"{module} never says the answer's language follows the question — "
        f"no passage mentions both"
    )


@pytest.mark.parametrize("module", QA_PROMPTS)
def test_no_chat_prompt_pins_one_language(module):
    """"Пишеш українською" answered a German question in Ukrainian."""
    for s in _prompt_strings(SRC / "llm" / "prompts" / module):
        low = s.lower()
        for phrase in ("пишеш українською", "українська мова", "in ukrainian",
                       "write in english", "answer in english"):
            assert phrase not in low, f"{module} pins the answer to one language"


def test_qa_does_not_go_through_the_documentation_setting():
    """`with_language` is for documentation. Wiring Q&A through it would make a
    workspace setting override what the person in front of it just asked."""
    qa = list((SRC / "qa").rglob("*.py"))
    offenders = [p.name for p in qa if "with_language" in p.read_text(encoding="utf-8")]
    assert not offenders, f"Q&A routed through the documentation language: {offenders}"


def test_the_classifier_still_understands_both_languages():
    """It routes a question to a prompt by regex. Translating those patterns
    would stop Ukrainian questions matching anything — the one translation in
    this codebase that would break a feature rather than reword it."""
    from src.qa.classifier import QuestionType, classify

    assert classify("як реалізовано авторизацію?") is QuestionType.TECHNICAL
    assert classify("how is auth implemented?") is QuestionType.TECHNICAL
    assert classify("де знайти конфіг?") is QuestionType.NAVIGATION
    assert classify("where is the config?") is QuestionType.NAVIGATION


def test_an_unmatched_language_degrades_instead_of_failing():
    """German matches no pattern. It must land on a usable type rather than
    raise — the answer will still come back in German."""
    from src.qa.classifier import QuestionType, classify

    assert classify("Wie funktioniert die Authentifizierung?") is QuestionType.FUNCTIONAL


# ─── review: the review_language setting ─────────────────────────────


def test_review_output_language_is_still_wired():
    base = (SRC / "review" / "agents" / "base.py").read_text(encoding="utf-8")
    assert "_review_language_instruction" in base
    assert '"review_language"' in base, "the setting is no longer read"
    # And it reaches the agents through the one composition point.
    assert "_compose_effective_system_prompt" in base


def test_english_adds_no_instruction():
    """The prompts are English now, so the default costs nothing. An
    instruction saying "write in English" on every review would be tokens spent
    to restate the obvious."""
    from src.review.agents.base import _REVIEW_LANG_NAMES

    assert _REVIEW_LANG_NAMES["en"] == "English"
    base = (SRC / "review" / "agents" / "base.py").read_text(encoding="utf-8")
    assert 'if code == "en":' in base


def test_the_claude_engine_gets_the_same_instruction():
    """Two review engines, one setting. The subscription path had to be wired
    separately and can silently fall behind."""
    engine = (SRC / "review" / "claude_engine.py").read_text(encoding="utf-8")
    assert "_review_language_instruction" in engine


def test_the_verifier_is_deliberately_left_out():
    """Its response is parsed as JSON — `keep` indices — and shown to nobody.
    A language instruction there risks corrupting a machine-read reply for no
    benefit, so the omission is correct and must stay."""
    verifier = (SRC / "review" / "agents" / "verifier.py").read_text(encoding="utf-8")
    assert "_review_language_instruction" not in verifier, (
        "the verifier now asks for prose in a chosen language, but its answer "
        "is parsed as JSON and never read by a person"
    )
    assert '"keep"' in verifier


# ─── documentation: the workspace setting ────────────────────────────


def test_documentation_still_goes_through_the_workspace_setting():
    for module in ("module_prd.py", "feature_doc.py", "integration_doc.py"):
        text = (SRC / "generation" / module).read_text(encoding="utf-8")
        assert "with_language(" in text, f"{module} lost the language directive"


# ─── the translation itself ──────────────────────────────────────────

TRANSLATED = [
    "src/llm/prompts/ba_answer.py", "src/llm/prompts/feature.py",
    "src/llm/prompts/integration.py", "src/llm/prompts/module_prd.py",
    "src/llm/prompts/overview.py", "src/llm/prompts/technical_answer.py",
    "src/qa/exploration_agent.py", "src/qa/multi_repo_retriever.py",
    "src/review/agents/contract.py", "src/review/agents/defect.py",
    "src/review/agents/security.py", "src/review/agents/verifier.py",
]


def _is_prose(s: str) -> bool:
    """Is this string prose sent to a model, or data keyed on a language?

    The distinction is the whole point. Two kinds of Ukrainian survive in this
    codebase on purpose:

      * regex patterns that recognise a Ukrainian question and route it to the
        right prompt (src/qa/classifier.py);
      * stopwords filtered out before identifiers are pulled from a question,
        so that "знаходиться" is not searched for as a symbol name
        (src/qa/multi_repo_retriever.py).

    Translate either and a feature stops working for Ukrainian speakers —
    the only translations in this repository that would break behaviour rather
    than reword it. Neither is prose: a prompt is never one word.
    """
    return " " in s.strip() and len(s.strip()) > 30


@pytest.mark.parametrize("path", TRANSLATED)
def test_no_ukrainian_is_left_in_a_prompt(path):
    """Part of this is going open source. A prompt half in Ukrainian is worse
    than one entirely in it — a reader cannot tell which half they are missing."""
    leftovers = [s for s in _prompt_strings(ROOT / path)
                 if CYRILLIC.search(s) and _is_prose(s)]
    assert not leftovers, (
        f"{path} still has Ukrainian in {len(leftovers)} prompt string(s): "
        f"{leftovers[0][:80]!r}"
    )


def test_the_question_patterns_are_exempt_and_still_ukrainian():
    """The two places that must NOT be translated, named so the exemption is a
    decision on the record rather than an oversight somebody later 'fixes'."""
    patterns = _prompt_strings(SRC / "qa" / "classifier.py")
    assert any(CYRILLIC.search(s) for s in patterns), (
        "the Ukrainian question patterns are gone — Ukrainian questions will "
        "no longer be routed to the right prompt"
    )
    stopwords = _prompt_strings(SRC / "qa" / "multi_repo_retriever.py")
    assert any(CYRILLIC.search(s) for s in stopwords), (
        "the Ukrainian stopwords are gone — common words like 'знаходиться' "
        "will be searched for as if they were symbol names"
    )
