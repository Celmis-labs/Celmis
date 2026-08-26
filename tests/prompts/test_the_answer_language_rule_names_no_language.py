"""The output-language rule must not name the language it is an example of.

OBSERVED on the deployed product: an ENGLISH question answered entirely in
German, twice out of two, on the include_code=false path — and a third time
earlier, recorded as "not reproducible on retry". The rule at the time read:

    A person who writes in German gets German back.

An instruction whose single worked example is a language is a mention of that
language sitting in the context window. On the degraded path there is almost
nothing else in the window: no code was read, the notes are English, the
notices are English, and the question is one short sentence. A model with
little to go on can take a mention for an instruction.

These assertions run against the PROMPT STRINGS, not the source files. The
explanation above lives in a comment beside each prompt and says "German"
several times; a test that grepped the file would fail on the comment that
documents the fix. That mistake has been made in this repository five times.
"""

from __future__ import annotations

import pytest

from src.llm.prompts.ba_answer import BA_ANSWER_PROMPT, BA_ANSWER_SYSTEM
from src.llm.prompts.language import LANGUAGE_NAMES
from src.llm.prompts.overview import OVERVIEW_PROMPT, OVERVIEW_SYSTEM
from src.llm.prompts.technical_answer import (
    TECHNICAL_ANSWER_PROMPT,
    TECHNICAL_ANSWER_SYSTEM,
)

SYSTEMS = {
    "technical": TECHNICAL_ANSWER_SYSTEM,
    "ba": BA_ANSWER_SYSTEM,
    "overview": OVERVIEW_SYSTEM,
}
TEMPLATES = {
    "technical": TECHNICAL_ANSWER_PROMPT,
    "ba": BA_ANSWER_PROMPT,
    "overview": OVERVIEW_PROMPT,
}
ALL = {**{f"{k}-system": v for k, v in SYSTEMS.items()},
       **{f"{k}-prompt": v for k, v in TEMPLATES.items()}}

# English is the exception, and only in the negative: the prompt says the
# English around the question is not what the answer follows. Naming it there
# is the point — that sentence exists to disarm the surrounding English.
NAMEABLE = {"English"}


@pytest.mark.parametrize("name", sorted(ALL))
def test_no_human_language_is_named_as_the_target(name):
    text = ALL[name]
    named = sorted({
        lang for lang in LANGUAGE_NAMES.values()
        if lang not in NAMEABLE and lang in text
    })
    assert not named, (
        f"{name} names {named}. The output-language rule takes the question's "
        f"language; naming one puts it in the window as a candidate. This is "
        f"how an English question came back in German on production."
    )


@pytest.mark.parametrize("name", sorted(ALL))
def test_english_is_only_ever_mentioned_to_be_ruled_out(name):
    """Not a style rule. "Answer in English" and "this is English, which is not
    the answer's language" are opposite instructions built from the same word,
    and only one of them may appear."""
    text = ALL[name]
    for line in text.splitlines():
        if "English" not in line:
            continue
        assert "not" in line or "that is not" in text, (
            f"{name} mentions English in {line!r} without ruling it out"
        )


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_rule_is_restated_where_nothing_can_outvote_it(name):
    """`language.py` states the reason for its own design: "recency wins when a
    long prompt asks for output in a different language — a directive at the
    top gets outvoted by the hundred lines beneath it." Generated docs get
    their directive appended last for exactly that reason. The answer prompts
    had theirs in the middle of a system prompt with ~2000 tokens of English
    beneath it.

    Keyed on the SECTION, not on a character count: a short prompt and a long
    one put the same final section at very different offsets, and a threshold
    tuned on one of them fails the other for being short.
    """
    text = TEMPLATES[name].rstrip()
    headers = [i for i, ln in enumerate(text.splitlines())
               if ln.startswith("# ")]
    assert headers, f"{name} has no sections to reason about"
    last_section = "\n".join(text.splitlines()[headers[-1]:])
    assert "language" in last_section.lower(), (
        f"{name}: the last section is {last_section.splitlines()[0]!r} and it "
        f"says nothing about the answer's language. The rule has to sit below "
        f"everything that could outvote it."
    )


@pytest.mark.parametrize("name", sorted(SYSTEMS))
def test_the_code_itself_is_never_translated(name):
    """The other half of the rule, and the half with a wrong-answer cost:
    translating an identifier produces a symbol that does not exist."""
    text = SYSTEMS[name].lower()
    assert "never translate" in text or "never the code" in text, (
        f"{name} asks for a language without protecting identifiers from it"
    )
