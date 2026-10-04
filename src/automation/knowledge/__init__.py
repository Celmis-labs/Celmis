"""What the Celmis agent knows about the product, and how it picks what to read.

`src.automation.guide.GUIDE` is the short page map that travels with every
planner call. It answers "where is X" and nothing deeper: asked whether agent
prompts can be changed per repository as well as for the whole workspace, the
agent could only say that prompts are "described in the Review agents section
of our guide" — true, and no use to anybody.

This package is the depth behind that map: one `Section` per topic, written
from the code, the pages under `web/app` and the labels in
`web/lib/i18n/messages/en.json`. Too much to send with every call, so
`select_sections` picks the few that a question is about and `knowledge_for`
renders them under a fixed budget.

WHY PYTHON AND NOT MARKDOWN. `.dockerignore` drops every `*.md` except the
README, and `docs/` is not copied into the image at all. Knowledge written as
markdown files would exist in the repository and be missing from the
container the agent runs in.

WHY SCORING AND NOT EMBEDDINGS. The question is one sentence, the sections are
a few dozen, and the answer has to be the same every time it is asked: a test
can pin that the Ukrainian question about per-repository prompts reads the
per-repository prompt section. Keywords are written as STEMS ("репозитор",
"промпт") and match the start of a word, which is enough morphology for
Ukrainian and Russian without a stemmer in the image.

THE LINKS ARE THE ALLOW-LIST TOO. Every `[label](/route)` written here joins
`GUIDE_ROUTES`, so an answer may link any page a section names — and a test
checks that each of them is a page, and that every quoted label is on screen.
"""

from __future__ import annotations

import re

from src.automation.knowledge._base import Section

#: Characters of knowledge one call may carry, on top of the page map. About
#: 4 characters to a token for this English prose, so ~6k tokens: enough for
#: three or four whole sections, small enough that a flash model answers in
#: seconds and a provider that caches the stable prefix still pays little.
KNOWLEDGE_BUDGET_CHARS = 24_000

#: At most this many sections, whatever their size. Past four the extra ones
#: are matches on a single common word and dilute the ones that matter.
MAX_SECTIONS = 5


def _sections() -> tuple[Section, ...]:
    from src.automation.knowledge import (
        access,
        ask,
        git,
        llm,
        ops,
        pages,
        review,
        troubleshooting,
    )

    out: list[Section] = []
    for mod in (pages, git, review, llm, access, ask, ops, troubleshooting):
        out.extend(mod.SECTIONS)
    ids = [s.id for s in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:  # a typo here would silently shadow a section
        raise RuntimeError(f"duplicate knowledge section ids: {dupes}")
    return tuple(out)


SECTIONS: tuple[Section, ...] = _sections()
BY_ID: dict[str, Section] = {s.id: s for s in SECTIONS}

#: Read when nothing else matches: the questions with no keyword in them are
#: mostly "where do I start".
FALLBACK_IDS: tuple[str, ...] = ("pages", "first-steps")


_WORD = re.compile(r"[\w'’-]+", re.UNICODE)


def _normalise(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").replace("’", "'")
    return re.sub(r"\s+", " ", text)


def _words(text: str) -> list[str]:
    return [w.strip("'-") for w in _WORD.findall(text) if w.strip("'-")]


def _hits(keyword: str, norm: str, words: list[str]) -> bool:
    kw = _normalise(keyword)
    if " " in kw or "/" in kw or "-" in kw:
        return kw in norm
    if len(kw) <= 3:  # short words must match whole: "pr", "sso", "api"
        return kw in words
    return any(w.startswith(kw) for w in words)


def score(section: Section, question: str) -> int:
    """How strongly `question` is about `section`. Deterministic."""
    norm = _normalise(question)
    words = _words(norm)
    total = 0
    for kw in section.keywords:
        if _hits(kw, norm, words):
            total += 1
    for kw in section.strong:
        if _hits(kw, norm, words):
            total += 3
    return total


def select_sections(question: str, *, budget_chars: int = KNOWLEDGE_BUDGET_CHARS,
                    max_sections: int = MAX_SECTIONS) -> list[Section]:
    """The sections to answer `question` from, best first, within the budget.

    Ties keep the order the sections are declared in, so the same question
    always reads the same text. A section that does not fit in what is left
    of the budget is skipped rather than cut: half a list of steps is worse
    than the next section whole.
    """
    ranked = sorted(
        ((score(s, question), i, s) for i, s in enumerate(SECTIONS)),
        key=lambda t: (-t[0], t[1]),
    )
    chosen = [s for sc, _, s in ranked if sc > 0]
    if not chosen:
        chosen = [BY_ID[i] for i in FALLBACK_IDS]

    picked: list[Section] = []
    used = 0
    for s in chosen:
        size = len(s.render())
        if used + size > budget_chars:
            continue
        picked.append(s)
        used += size
        if len(picked) >= max_sections:
            break
    return picked


def knowledge_for(question: str, *, budget_chars: int = KNOWLEDGE_BUDGET_CHARS) -> str:
    """The rendered sections for `question`, ready for the system prompt."""
    return "\n".join(s.render() for s in select_sections(question, budget_chars=budget_chars))


def all_text() -> str:
    """Every section, for the link and label checks."""
    return "\n".join(s.render() for s in SECTIONS)


__all__ = [
    "BY_ID", "KNOWLEDGE_BUDGET_CHARS", "SECTIONS", "Section", "all_text",
    "knowledge_for", "score", "select_sections",
]
