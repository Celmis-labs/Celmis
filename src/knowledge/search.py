"""Finding things in a pack: a tokenizer, BM25 ranking and a snippet.

No index, no embeddings, no external service. A pack holds a few hundred small
text files, so ranking is done over the rows the store hands in; what makes it
cheap is that each item carries, from ingest, a ``terms`` map (its most frequent
tokens) so the content itself is never re-read to rank it.

Tokens are Unicode words, lower-cased, at least two characters, never a bare
number, minus a short stop-word list (English and Ukrainian). Text is composed
(NFC) first, so a word typed on a keyboard that decomposes ``й`` or ``ї`` is the
same word; and an apostrophe INSIDE a word (``'``, ``’`` or ``ʼ``) is dropped, so
``об'єкт``, ``об’єкт`` and ``обʼєкт`` are one token, ``обєкт``, in an index and in
a query alike. The English possessive ``'s`` is not part of the word
(``user's`` -> ``user``). A word of seven
characters or more also yields a ``~``-prefixed five-character stem
(``authentication`` -> ``~authe``): the two forms of an inflected word share it,
which is what lets a query in Ukrainian or Russian find another case of the
same noun. The ``~`` keeps a stem from ever colliding with a real word.

Ordering is deterministic: score, then path, then name.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

#: The most frequent terms of an item that are kept for ranking.
MAX_TERMS = 400
#: Words at least this long also produce a stem token.
STEM_FROM = 7
STEM_LEN = 5

#: A word, with an apostrophe allowed between two of its parts (not the possessive
#: ``'s``). Always run on text through :func:`_fold`, which makes every apostrophe ``'``.
_WORD = re.compile(r"\w+(?:'(?!s\b)\w+)*", re.UNICODE)
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})
_NUMBER = re.compile(r"^[\d_]+$")

_STOP = frozenset("""
a an and are as at be but by for from has have how i if in into is it its of on
or our so that the their then there these they this to was we were what when
where which who why will with you your not no can do does did should would could
about also than them been being
і й та а але або що це як на у в з із до по за від для при про між над під без
чи ж би бо то так не ні він вона воно вони ми ви я ти його її їх мій наш ваш цей
ця ці той та ті який яка яке які де коли чому щоб також тут там вже ще лише
""".split())  # noqa: SIM905 — a word list reads better than a tuple


def _fold(text: str) -> str:
    """``text`` composed (NFC), lower-cased, every apostrophe the ASCII one. Each
    step keeps one character per character, so positions in the result are
    positions in the text."""
    return unicodedata.normalize("NFC", text or "").lower().translate(_APOSTROPHES)


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens of ``text``, in order, with stems for long words."""
    out: list[str] = []
    for found in _WORD.finditer(_fold(text)):
        word = found.group(0).replace("'", "").strip("_")
        if len(word) < 2 or _NUMBER.match(word) or word in _STOP:
            continue
        out.append(word)
        if len(word) >= STEM_FROM:
            out.append("~" + word[:STEM_LEN])
    return out


def terms_for(text: str, limit: int = MAX_TERMS) -> dict[str, int]:
    """term -> count for the ``limit`` most frequent tokens of ``text`` (ties by
    term, so the same text always gives the same map)."""
    counts: dict[str, int] = {}
    for token in tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return dict(top)


# ─── Ranking ─────────────────────────────────────────────────────────

#: BM25 parameters, and the weight of each field of an item.
K1 = 1.2
B = 0.75
W_NAME, W_DESCRIPTION, W_PATH, W_TERMS = 3.0, 2.0, 1.0, 1.0


class Hit(NamedTuple):
    score: float
    item: Any


def _counts(tokens: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tokens:
        out[t] = out.get(t, 0) + 1
    return out


class Doc(NamedTuple):
    item: Any
    tf: dict[str, float]
    length: float


def document(item: Any) -> Doc:
    """The weighted term frequencies of one item: name x3, description (and
    tags) x2, path x1, the item's own frequent terms x1. The one definition of
    what an item weighs: :func:`rank` and the streaming ranker in
    ``store.py`` both read it."""
    tf: dict[str, float] = {}

    def add(weight: float, counts: dict[str, int]) -> None:
        for term, n in counts.items():
            tf[term] = tf.get(term, 0.0) + weight * n

    add(W_NAME, _counts(tokenize(getattr(item, "name", "") or "")))
    tags = " ".join(str(t) for t in (getattr(item, "tags", None) or ()))
    add(W_DESCRIPTION, _counts(tokenize(f"{getattr(item, 'description', '') or ''} {tags}")))
    add(W_PATH, _counts(tokenize((getattr(item, "path", "") or "").replace("/", " "))))
    stored = getattr(item, "terms", None) or {}
    add(W_TERMS, {str(k): int(v) for k, v in stored.items() if isinstance(v, int | float)})
    return Doc(item, tf, sum(tf.values()))


def rank(items: Sequence[Any], query: str, limit: int = 20) -> list[Hit]:
    """The ``limit`` best items for ``query``, best first. An item scores only
    if it shares at least one token with the query; ``items`` are objects with
    ``name``, ``description``, ``path``, ``terms`` (and optionally ``tags``)."""
    wanted = sorted(set(tokenize(query)))
    if not wanted or not items or limit <= 0:
        return []
    docs = [document(i) for i in items]
    n_docs = len(docs)
    avg = (sum(d.length for d in docs) / n_docs) or 1.0
    scored: list[Hit] = []
    containing = {t: sum(1 for d in docs if t in d.tf) for t in wanted}
    for d in docs:
        score = 0.0
        for term in wanted:
            tf = d.tf.get(term)
            if not tf:
                continue
            idf = math.log(1.0 + (n_docs - containing[term] + 0.5) / (containing[term] + 0.5))
            score += idf * (tf * (K1 + 1.0)) / (tf + K1 * (1.0 - B + B * d.length / avg))
        if score > 0.0:
            scored.append(Hit(score, d.item))
    scored.sort(key=lambda h: (-h.score, getattr(h.item, "path", ""), getattr(h.item, "name", "")))
    return scored[:limit]


# ─── Snippet ─────────────────────────────────────────────────────────

_FRONT = re.compile(r"\A---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_BLANK = re.compile(r"\n\s*\n")


def _is_heading(paragraph: str) -> bool:
    return all(line.lstrip().startswith("#") for line in paragraph.splitlines() if line.strip())


def _paragraphs(content: str) -> list[str]:
    """Blank-line separated paragraphs; a heading is kept with the paragraph
    under it, so a match on the heading quotes what the heading introduces."""
    body = _FRONT.sub("", content or "", count=1)
    out: list[str] = []
    carry = ""
    for p in (p.strip() for p in _BLANK.split(body)):
        if not p:
            continue
        if _is_heading(p):
            carry = f"{carry}\n{p}" if carry else p
            continue
        out.append(f"{carry}\n{p}" if carry else p)
        carry = ""
    if carry:
        out.append(carry)
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def snippet(content: str, query_terms: Iterable[str], max_chars: int = 400) -> str:
    """The paragraph of ``content`` that matches most of ``query_terms`` (tokens
    from :func:`tokenize`), cut to ``max_chars`` around its first match. With no
    match, the first paragraph (a heading travels with the paragraph under it)."""
    wanted = set(query_terms)
    paragraphs = _paragraphs(content)
    if not paragraphs:
        return ""
    best, best_key = None, (0, 0)
    for para in paragraphs:
        hit = [t for t in tokenize(para) if t in wanted]
        key = (len(set(hit)), len(hit))
        if key > best_key:
            best, best_key = para, key
    if best is None:
        best = paragraphs[0]
    text = _squash(unicodedata.normalize("NFC", best))
    if len(text) <= max_chars:
        return text
    room = max(8, max_chars - 4)           # the two ellipses take the rest
    pos = 0
    for m in _WORD.finditer(_fold(text)):
        word = m.group(0).replace("'", "").strip("_")
        if word in wanted or ("~" + word[:STEM_LEN]) in wanted:
            pos = m.start()
            break
    start = max(0, pos - room // 3)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < pos else start
    end = min(len(text), start + room)
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start + room // 2 else end
    cut = text[start:end].strip()
    return ("… " if start > 0 else "") + cut + (" …" if end < len(text) else "")


__all__ = ["Doc", "Hit", "MAX_TERMS", "document", "rank", "snippet", "terms_for", "tokenize"]
