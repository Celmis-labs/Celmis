"""Question type classifier. Deterministic (regex+keywords)."""

from __future__ import annotations

import re
from enum import StrEnum


class QuestionType(StrEnum):
    OVERVIEW = "overview"  # "what is this?"
    NAVIGATION = "navigation"  # "where do I find it?"
    FUNCTIONAL = "functional"  # "how does the feature work?"
    TECHNICAL = "technical"  # "how is it implemented?" — code in the answer
    INTEGRATION = "integration"  # "how do I call / integrate with it?"
    IMPACT = "impact"  # "what will break?"


_TECHNICAL_PATTERNS = [
    r"як\s+реалізован",
    r"як\s+зроблен",
    r"як\s+працю[єе].+внутр",
    r"покаж[иі]\s+код",
    r"приклад\s+код[уа]",
    r"how\s+is\s+.+\s+implement",
    r"show\s+(me\s+)?(the\s+)?code",
    r"internals?\s+of",
]

_INTEGRATION_PATTERNS = [
    r"як\s+(?:мені\s+)?(?:інтегру|виклик|використ)",
    r"як\s+підключит",
    r"приклад\s+виклику",
    r"how\s+(?:do\s+i|to)\s+(?:integrate|call|use)",
    r"how\s+to\s+connect",
    r"integration\s+guide",
]

_NAVIGATION_PATTERNS = [
    r"^де\s+",
    r"знайт[иі]",
    r"^у\s+якому\s+файл",
    r"where\s+(?:is|can\s+i\s+find)",
    r"which\s+file",
]

_IMPACT_PATTERNS = [
    r"що\s+(?:зламається|змінит|постражда)",
    r"blast\s+radius",
    r"хто\s+(?:ще\s+)?виклика",
    r"what\s+(?:will\s+)?break",
    r"who\s+calls",
    r"impact\s+of",
]

_FUNCTIONAL_PATTERNS = [
    r"як\s+працю[єе]",
    r"що\s+робить",
    r"опиши\s+",
    r"поясни\s+",
    r"how\s+does\s+.+\s+work",
    r"explain\s+",
    r"describe\s+",
]

_OVERVIEW_PATTERNS = [
    r"^що\s+це",
    r"^про\s+що",
    r"overview",
    r"огляд",
    r"what\s+is\s+this",
]


def classify(question: str) -> QuestionType:
    """Determines the question type. The strongest match wins (order matters)."""
    q = question.lower().strip()

    def any_match(patterns: list[str]) -> bool:
        return any(re.search(p, q) for p in patterns)

    # Technical has the highest priority — it influences the answer format the most
    if any_match(_TECHNICAL_PATTERNS):
        return QuestionType.TECHNICAL
    if any_match(_INTEGRATION_PATTERNS):
        return QuestionType.INTEGRATION
    if any_match(_IMPACT_PATTERNS):
        return QuestionType.IMPACT
    if any_match(_NAVIGATION_PATTERNS):
        return QuestionType.NAVIGATION
    if any_match(_FUNCTIONAL_PATTERNS):
        return QuestionType.FUNCTIONAL
    if any_match(_OVERVIEW_PATTERNS):
        return QuestionType.OVERVIEW

    # Default — functional, but without code
    return QuestionType.FUNCTIONAL
