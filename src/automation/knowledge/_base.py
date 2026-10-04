"""The shape of one knowledge section — its own module so the topic modules
can import it while the package is still initialising."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """One topic, self-contained enough to answer from on its own."""

    id: str
    title: str
    #: Stems and phrases in English, Ukrainian and Russian. A stem matches any
    #: word that starts with it ("промпт" matches "промпти", "промптів"); a
    #: phrase with a space matches as a substring of the normalised question.
    keywords: tuple[str, ...]
    body: str
    #: Stems that make this THE section for a question, weighted above the
    #: plain keywords. Used sparingly: for the words nobody uses about
    #: anything else ("override", "перевизнач", "webhook").
    strong: tuple[str, ...] = ()

    def render(self) -> str:
        return f"### {self.title}\n\n{self.body.strip()}\n"
