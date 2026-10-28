"""Search terms for a question about code written in another language.

A question is asked in one language ("порізка", "cutting"), the identifiers of
the code are in another (a Russian-named configuration, an English service).
Neither the graph's exact-name lookup nor a substring scan bridges that gap, and
neither knows that a Cyrillic word has endings. Two language-agnostic layers:

* ``lexical_terms`` — the question's own words folded to a stem that survives
  case endings and the ru/uk/be spelling differences of the same root
  (``порізка``/``порезка``), no dictionary involved;
* ``llm_terms`` — ONE short model call that restates the question as search
  words in the language(s) the repository's identifiers use. It is the only
  step here that needs a model; without one (no key, a failure, a timeout)
  it returns nothing and the lexical layer carries on alone.

``rank_symbols`` then scores a repository's symbols (name, docstring,
signature) against the stems, down-weighting stems that occur everywhere.
The module reads data it is handed; the access rules are the caller's.
"""

from __future__ import annotations

import json
import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from src.qa.text_search import terms_of

logger = logging.getLogger(__name__)

#: ru/uk/be letters that differ between the languages for the same root, folded
#: to one letter. Lossy on purpose: it is only ever used to COMPARE two spellings.
_FOLD = str.maketrans({
    "ё": "и", "е": "и", "є": "и", "э": "и", "ї": "и", "і": "и", "ы": "и", "ґ": "г", "ў": "у",
    "ъ": None, "ь": None, "'": None, "’": None, "ʼ": None,
})
#: Inflection letters peeled off the end of a Cyrillic word (case endings).
_ENDING = frozenset("аяоеиуюй")
_MIN_STEM = 4
_MAX_TERMS = 24
_CAMEL = re.compile(r"[A-ZА-ЯІЇЄҐ]?[a-zа-яіїєґё]+|[A-ZА-ЯІЇЄҐ]+(?![a-zа-яіїєґё])|\d+")


def fold(text: str) -> str:
    """Case-folded, NFC, ru/uk spelling differences levelled."""
    return unicodedata.normalize("NFC", text).casefold().translate(_FOLD)


def stem(word: str) -> str:
    """Folded word with up to two trailing inflection letters removed (never
    below ``_MIN_STEM`` letters). Latin and short words are only folded."""
    w = fold(word)
    if not any("а" <= c <= "я" for c in w):
        return w
    for _ in range(2):
        if len(w) > _MIN_STEM and w[-1] in _ENDING:
            w = w[:-1]
    return w


def lexical_terms(question: str, limit: int = 12) -> list[str]:
    """Distinct stems of the question's content words, longest first."""
    seen: dict[str, None] = {}
    for w in terms_of(question, limit=limit * 2):
        s = stem(w)
        if len(s) >= 3:
            seen.setdefault(s)
    return list(seen)[:limit]


def vocabulary_sample(names: Iterable[str], size: int = 40) -> list[str]:
    """The most frequent words of identifier names (camelCase, snake_case and
    dotted names split): what language the code is written in, shown to the model."""
    counts: Counter[str] = Counter()
    for n in names:
        for w in _CAMEL.findall(n or ""):
            if len(w) >= 4:
                counts[w.lower()] += 1
    return [w for w, _ in counts.most_common(size)]


_PROMPT = (
    "You help search a source-code repository. Rewrite the question as search "
    "keywords for FINDING THE CODE: the key concepts of the question, each in the "
    "spellings, synonyms and translations an identifier or comment could use. "
    "Write them in the language(s) of the repository's own words (sample below); "
    "when that language is Cyrillic, give both the Russian and the Ukrainian "
    "spelling. Single words or word stems, no phrases, at most {n} items. "
    "Reply with a JSON array of strings and nothing else.\n\n"
    "Repository words: {vocab}\n\nQuestion: {question}"
)
_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]", re.DOTALL)


def parse_terms(reply: str) -> list[str]:
    """Words of a model reply (a JSON array; fences and prose around it are
    tolerated). Anything that is not a plain word is dropped."""
    m = _JSON_ARRAY.search(reply or "")
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except ValueError:
        return []
    out: dict[str, None] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        for w in re.findall(r"\w{3,}", item, re.UNICODE):
            out.setdefault(stem(w))
    return list(out)[:_MAX_TERMS]


def llm_terms(
    question: str, vocab: list[str], generate: Callable[..., object],
) -> list[str]:
    """Stems from ONE model call, or [] when the call fails for any reason.

    ``generate`` is ``LLMClient.generate``-shaped: it takes ``prompt`` and
    returns an object with ``.text``."""
    prompt = _PROMPT.format(
        n=_MAX_TERMS, vocab=", ".join(vocab) or "(unknown)", question=question[:1500])
    try:
        res = generate(prompt=prompt, operation="qa_search_terms",
                       max_output_tokens=200, temperature=0.0, num_retries=0, timeout=20)
    except Exception as exc:  # noqa: BLE001 — no key, no network, budget: carry on lexically
        logger.info("qa_search_terms_unavailable err=%s", type(exc).__name__)
        return []
    return parse_terms(getattr(res, "text", "") or "")


# ─── scoring ─────────────────────────────────────────────────────────────


@dataclass
class SymbolHit:
    id: str
    name: str
    kind: str
    file: str
    start_line: int
    end_line: int
    score: float
    docstring: str = ""
    signature: str = ""


@dataclass
class FileHit:
    file: str
    score: float
    symbols: list[SymbolHit] = field(default_factory=list)


def _local(name: str) -> str:
    return name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def rank_symbols(
    rows: Iterable[Mapping], terms: list[str], *, max_files: int = 8,
    max_symbols: int = 10, common_share: float = 0.3,
) -> tuple[list[FileHit], list[SymbolHit]]:
    """Files and symbols that best match ``terms`` (stems from :func:`stem`).

    A stem found in a symbol's local name weighs 3, in the rest of its name
    (the module/object it belongs to) 2, in its docstring or signature 1; each
    stem is weighted by ``log(N / df)`` and ignored when it occurs in more than
    ``common_share`` of the symbols (``процедура``, ``get``). A file scores its
    best symbol plus a quarter of the next four, so a module whose name matches
    outranks one that merely mentions the words once.
    """
    prepared = []
    for r in rows:
        name = str(r.get("name") or "")
        prepared.append((r, fold(name), fold(_local(name)), fold(
            f"{r.get('docstring') or ''} {r.get('signature') or ''}")))
    n = len(prepared)
    terms = [t for t in dict.fromkeys(terms) if len(t) >= 3]
    if not n or not terms:
        return [], []
    df = {t: sum(1 for _, nm, _, doc in prepared if t in nm or t in doc) for t in terms}
    small = n < 50  # too few symbols for "common" to mean anything
    weight = {t: math.log(n / d) for t, d in df.items()
              if 0 < d < n and (small or d <= common_share * n)}
    if not weight:
        return [], []

    hits: list[SymbolHit] = []
    for r, nm, loc, doc in prepared:
        score = 0.0
        for t, w in weight.items():
            if t in loc:
                score += 3 * w
            elif t in nm:
                score += 2 * w
            elif t in doc:
                score += w
        if score:
            hits.append(SymbolHit(
                id=str(r.get("id") or ""), name=str(r.get("name") or ""),
                kind=str(r.get("kind") or ""), file=str(r.get("file") or ""),
                start_line=int(r.get("start_line") or 0),
                end_line=int(r.get("end_line") or r.get("start_line") or 0),
                score=score, docstring=str(r.get("docstring") or ""),
                signature=str(r.get("signature") or "")))
    hits.sort(key=lambda h: (-h.score, h.file, h.start_line))

    by_file: dict[str, list[SymbolHit]] = {}
    for h in hits:
        by_file.setdefault(h.file, []).append(h)
    files = [
        FileHit(f, hs[0].score + 0.25 * sum(h.score for h in hs[1:5]), hs)
        for f, hs in by_file.items()
    ]
    files.sort(key=lambda f: (-f.score, f.file))
    code = [h for h in hits if h.kind != "file_module"]
    return files[:max_files], code[:max_symbols]
