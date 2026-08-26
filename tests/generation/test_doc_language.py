"""Documentation comes out in the language the workspace asked for.

Every generation system prompt carried the sentence "Пишеш українською". So a
workspace whose people read German got Ukrainian documentation, and there was
nothing to change: the interface had sixteen locales and the thing the product
actually produces had one, hardcoded three files deep.

The prompts stay in Ukrainian. A model does not need an instruction written in
the language it is asked to answer in, and translating them into every
supported language would be that many times the surface for a wording change.
The language is one directive, appended last.

Most of what is worth pinning here is the resolution order and the failure
behaviour: a value stored months ago must not take a background vault build
down, and an override must not quietly become a setting.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.llm.prompts import (
    FEATURE_PROMPT,
    FEATURE_SYSTEM,
    INTEGRATION_PROMPT,
    INTEGRATION_SYSTEM,
    MODULE_PRD_PROMPT,
    MODULE_PRD_SYSTEM,
)
from src.llm.prompts.language import (
    DEFAULT_DOC_LANGUAGE,
    LANGUAGE_NAMES,
    NATIVE_PHRASES,
    language_directive,
    normalise,
    with_language,
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

#: Every string that reaches the model on a documentation run — the system
#: instruction AND the task prompt.
#:
#: Checking only the system constants is how this shipped half-broken:
#: MODULE_PRD_PROMPT carried "Українська мова у контенті" in its rules list, so
#: picking German produced two contradicting instructions with the Ukrainian
#: one sitting closer to the task. The tests were green the whole time.
#:
#: Q&A prompts are deliberately absent — a chat answer follows the question,
#: not a workspace setting.
DOC_PROMPTS = {
    "MODULE_PRD_SYSTEM": MODULE_PRD_SYSTEM,
    "MODULE_PRD_PROMPT": MODULE_PRD_PROMPT,
    "FEATURE_SYSTEM": FEATURE_SYSTEM,
    "FEATURE_PROMPT": FEATURE_PROMPT,
    "INTEGRATION_SYSTEM": INTEGRATION_SYSTEM,
    "INTEGRATION_PROMPT": INTEGRATION_PROMPT,
}


# ─── the sentence that caused this ───────────────────────────────────


@pytest.mark.parametrize("name,prompt", sorted(DOC_PROMPTS.items()))
def test_no_documentation_prompt_names_an_output_language(name, prompt):
    """A language named anywhere in the prompt is a second instruction, and it
    contradicts the directive rather than agreeing with it.

    Two shapes have appeared: "Пишеш українською" in a system prompt, and
    "Українська мова у контенті" buried in a rules list in the task prompt.
    The second one sat closer to the task than the directive did.
    """
    lowered = prompt.lower()
    banned = (
        "пишеш українською", "пиши українською", "українська мова",
        "українською мовою", "write in english", "answer in english",
        "in ukrainian",
    )
    for phrase in banned:
        assert phrase not in lowered, (
            f"{name} names an output language; it will fight the directive"
        )


@pytest.mark.parametrize("name,prompt", sorted(DOC_PROMPTS.items()))
def test_the_directive_reaches_every_documentation_prompt(name, prompt):
    out = with_language(prompt, "de")
    assert "OUTPUT LANGUAGE" in out
    assert "auf Deutsch" in out and "German" in out


def test_the_directive_is_last():
    """A prompt is a hundred lines of Ukrainian; a language instruction at the
    top loses to all of it. Recency is the only reason this works."""
    out = with_language(MODULE_PRD_SYSTEM, "ja")
    assert out.index("OUTPUT LANGUAGE") > len(MODULE_PRD_SYSTEM.rstrip()) - 1
    assert out.rstrip().endswith("never the code.")


def test_the_directive_protects_code_from_translation():
    """Identifiers and paths translated into Romanian make a document that
    cannot be used to find anything."""
    out = language_directive("ro")
    assert "identifiers" in out and "file paths" in out


def test_the_native_phrase_is_used_where_there_is_one():
    """"auf Deutsch" sits inside the target language and pulls the model into
    it; "in German" describes the target from outside."""
    assert "auf Deutsch" in language_directive("de")
    # And where there is none, the English name still carries it rather than
    # producing a directive with a hole in it.
    assert "Georgian" in language_directive("ka")
    assert "None" not in language_directive("ka")


# ─── normalisation: never raise on the read path ─────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("de", "de"), ("de-AT", "de"), ("en_GB", "en"), ("UK", "uk"),
    (None, DEFAULT_DOC_LANGUAGE), ("", DEFAULT_DOC_LANGUAGE),
    ("xx", DEFAULT_DOC_LANGUAGE), ("  fr  ", "fr"),
])
def test_a_stored_value_never_takes_a_build_down(raw, expected):
    """Generation runs in a background worker. A code stored months ago, or a
    regional tag a browser handed us, must degrade rather than raise."""
    assert normalise(raw) == expected


def test_chinese_is_not_collapsed_to_one_script():
    """zh-CN and zh-TW are different scripts. Folding them to a bare "zh"
    silently picks one, and the reader gets the wrong characters."""
    assert normalise("zh-CN") == "zh-CN"
    assert normalise("zh-TW") == "zh-TW"
    assert normalise("zh") in ("zh-CN", "zh-TW")


def test_the_default_is_what_the_prompts_used_to_hardcode():
    """Any other default silently rewords every existing vault the next time
    somebody regenerates it."""
    assert DEFAULT_DOC_LANGUAGE == "uk"


# ─── one list, not two ───────────────────────────────────────────────


def test_review_and_documentation_offer_the_same_languages():
    """Review comments had their own copy of this list. Two lists drift, and a
    language offered on one settings card but not the other is a dropdown that
    means something different depending on which page you opened."""
    from src.review.agents.base import _REVIEW_LANG_NAMES

    assert _REVIEW_LANG_NAMES is LANGUAGE_NAMES


def test_the_review_list_is_not_redeclared():
    source = (SRC / "review" / "agents" / "base.py").read_text()
    assert "_REVIEW_LANG_NAMES = {" not in source, (
        "the language list has been copied back into the review agents"
    )


def test_every_native_phrase_names_a_supported_language():
    """A native phrase for a code the product does not offer is dead text that
    reads like coverage."""
    assert set(NATIVE_PHRASES) <= set(LANGUAGE_NAMES)


# ─── resolution order ────────────────────────────────────────────────


def test_an_explicit_language_wins_over_the_workspace(monkeypatch):
    from src.generation import doc_language

    monkeypatch.setattr(doc_language, "get_workspace_language", lambda ws="default": "uk")
    assert doc_language.resolve_doc_language("en", "ws-1") == "en"


def test_no_argument_falls_back_to_the_workspace(monkeypatch):
    from src.generation import doc_language

    monkeypatch.setattr(doc_language, "get_workspace_language", lambda ws="default": "pl")
    assert doc_language.resolve_doc_language(None, "ws-1") == "pl"


def test_an_unreadable_store_does_not_raise(monkeypatch):
    """The read path runs inside a worker. A credential store that is briefly
    unreachable must not fail a ten-minute build over a dropdown value."""
    import src.api.routers.llm as llm_router
    from src.generation import doc_language

    def boom(workspace_id="default"):
        raise RuntimeError("store down")

    monkeypatch.setattr(llm_router, "_load_workspace_config", boom)
    assert doc_language.get_workspace_language("ws-1") == DEFAULT_DOC_LANGUAGE


def test_setting_an_unsupported_language_is_refused(monkeypatch):
    """Unlike the read path, this one has a user and a form to show an error
    in — so it says no instead of silently storing something inert."""
    from src.generation import doc_language

    with pytest.raises(ValueError, match="unsupported"):
        doc_language.set_workspace_language("klingon", "ws-1")


# ─── the wiring, which is the part that silently does not happen ─────


def _calls_with_language(path: Path) -> bool:
    """Does this generator wrap its system prompt at the call site?"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "system_instruction":
                continue
            if isinstance(kw.value, ast.Call) and \
               getattr(kw.value.func, "id", "") == "with_language":
                return True
    return False


@pytest.mark.parametrize("module", [
    "module_prd.py", "feature_doc.py", "integration_doc.py",
])
def test_every_generator_passes_the_language_to_the_model(module):
    """The prompt can be perfect and the setting can be stored, and if the call
    site passes the bare constant none of it reaches the model."""
    assert _calls_with_language(SRC / "generation" / module), (
        f"{module} sends its system prompt without the language directive"
    )


def test_the_queued_job_carries_the_language():
    """Resolved when the job is queued, not when a worker picks it up — a run
    started an hour ago must write the language chosen then, not whatever the
    setting says now."""
    repos = (SRC / "api" / "routers" / "repos.py").read_text()
    assert '"language": language' in repos, "the job payload drops the language"
    handlers = (SRC / "sync" / "handlers.py").read_text()
    assert 'language=p.get("language")' in handlers, (
        "the worker ignores the language the job was queued with"
    )


def test_one_run_writes_one_language():
    """A build takes minutes and calls the model once per module. Re-resolving
    per document would let a setting edited midway through split a vault
    across two languages."""
    orch = (SRC / "generation" / "orchestrator.py").read_text()
    assert orch.count("resolve_doc_language(") == 1
    assert "gen.language = doc_language" in orch


def test_qa_answers_are_not_wired_to_the_documentation_setting():
    """Scoped deliberately, and the scope has a reason.

    A chat answer should follow the person who asked, not a workspace-wide
    setting — whoever else is in that workspace does not get a vote on the
    language of someone else's question. Documentation is the opposite: it
    belongs to the repository, so it takes the workspace's setting.

    This started life asserting that the Q&A prompt still hardcoded Ukrainian,
    which was true when it was written and stopped being true two commits
    later. Its replacement pinned the rule's exact sentence — "answer in the
    SAME LANGUAGE the question is asked in" — and broke the moment that
    sentence was rewritten to stop naming German as its worked example. Same
    mistake in a new coat: a surface feature standing in for the property.

    What the property actually is: these prompts must not be wired to the
    documentation language. What the rule SAYS is asserted on the prompt
    strings themselves in
    tests/prompts/test_the_answer_language_rule_names_no_language.py.
    """
    import importlib

    for name in ("ba_answer", "technical_answer", "overview"):
        module = importlib.import_module(f"src.llm.prompts.{name}")
        assert not hasattr(module, "with_language"), (
            f"{name} imported with_language — a workspace default would then "
            "override the language the person just asked in"
        )
        # Source too, for a call routed some other way. Comments stripped
        # first: the comment in each of these files EXPLAINS that Q&A does not
        # go through `with_language()`, so grepping raw text finds the very
        # word whose absence is the point — the trap this repository has now
        # walked into five times.
        text = (SRC / "llm" / "prompts" / f"{name}.py").read_text()
        code = "\n".join(
            line.split("#", 1)[0] for line in text.splitlines()
            if not line.strip().startswith("#")
        )
        assert "with_language(" not in code, (
            f"{name} is wired to the documentation setting, which would let a "
            "workspace default override what the person just asked in"
        )
