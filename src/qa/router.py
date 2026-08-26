"""Router — decides which retrieval path to use for a question.

Three paths:
  A: Exact symbol mentioned (`functionName`, CamelCase) → straight to the graph
  B: File/path mentioned (src/..., .ts) → read the file + the graph
  C: Natural language → Qdrant vector search
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class RoutePath(StrEnum):
    EXACT_SYMBOL = "A"
    FILE_PATH = "B"
    NATURAL_LANGUAGE = "C"


# Identifier patterns
_BACKTICK_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
_CAMEL_CASE = re.compile(r"\b[a-z][a-zA-Z]*[A-Z][a-zA-Z]+\b")
_PASCAL_METHOD = re.compile(r"\b[A-Z][a-zA-Z]+\.[a-zA-Z_][a-zA-Z0-9_]+\b")
_SNAKE_CASE = re.compile(r"\b[a-z]+(?:_[a-z]+){1,}\b")

# File/path patterns
_FILE_EXT = re.compile(r"\b[\w./-]+\.(?:ts|tsx|js|jsx|py|go|php)\b")
_SRC_PATH = re.compile(r"\bsrc/[\w./-]+")


@dataclass
class RouteDecision:
    """The chosen path + the extracted entities."""

    path: RoutePath
    symbols: list[str]
    files: list[str]
    raw_question: str

    def as_dict(self) -> dict:
        return {
            "path": self.path.value,
            "symbols": self.symbols,
            "files": self.files,
        }


class QueryRouter:
    """Analyses the question and picks the retrieval path."""

    def route(self, question: str) -> RouteDecision:
        symbols = self._extract_symbols(question)
        files = self._extract_files(question)

        if files:
            return RouteDecision(
                path=RoutePath.FILE_PATH,
                symbols=symbols,
                files=files,
                raw_question=question,
            )

        # Short questions with an explicit symbol → Exact path
        words = question.split()
        if symbols and len(words) <= 12:
            return RouteDecision(
                path=RoutePath.EXACT_SYMBOL,
                symbols=symbols,
                files=[],
                raw_question=question,
            )

        return RouteDecision(
            path=RoutePath.NATURAL_LANGUAGE,
            symbols=symbols,  # they may still come in handy as a seed
            files=[],
            raw_question=question,
        )

    @staticmethod
    def _extract_symbols(q: str) -> list[str]:
        out: set[str] = set()
        for m in _BACKTICK_IDENT.findall(q):
            out.add(m)
        for m in _PASCAL_METHOD.findall(q):
            out.add(m)
        for m in _CAMEL_CASE.findall(q):
            out.add(m)
        for m in _SNAKE_CASE.findall(q):
            # Ignore generic words such as "how_to" — snake case that is too short
            if len(m) >= 4 and "_" in m:
                out.add(m)
        # Filter out common words
        stopwords = {"how_to", "do_i", "can_i", "и_т", "to_do"}
        return sorted(s for s in out if s not in stopwords)

    @staticmethod
    def _extract_files(q: str) -> list[str]:
        out: set[str] = set()
        out.update(_FILE_EXT.findall(q))
        out.update(_SRC_PATH.findall(q))
        return sorted(out)
