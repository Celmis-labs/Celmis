"""How `find` orders its candidates.

Tiers by how the name matches (exact beats prefix beats all-tokens beats
substring beats fuzzy), then small adjustments: what kind of symbol it is, how
much it is used, whether it is exported, and whether it lives in tests, vendor
or generated code. A match that no tier accepts scores 0 and is dropped, so a
typo query returns near-misses and a nonsense query returns nothing.
"""

from __future__ import annotations

import difflib
import math
import re

from src.indexing.graph.graph_store import name_tokens

TIER_EXACT = 100
TIER_EXACT_CI = 85
TIER_PREFIX = 60
TIER_TOKENS = 45
TIER_SUBSTRING = 30
TIER_FUZZY = 15
FUZZY_MIN_RATIO = 0.75

_GOOD_KINDS = {"class", "function", "method", "interface", "struct", "enum", "type"}
_WEAK_KINDS = {"variable", "import", "export", "constant"}
_LOW_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|vendor|node_modules|migrations?|dist|build|"
    r"generated|__generated__|third_party)(/|$)|(\.min\.js|\.pb\.go|_pb2\.py|\.generated\.\w+)$",
    re.IGNORECASE,
)


def match_tier(name: str, query: str) -> int:
    """0 when `name` does not match `query` at all."""
    if name == query:
        return TIER_EXACT
    ln, lq = name.lower(), query.lower()
    if ln == lq:
        return TIER_EXACT_CI
    if ln.startswith(lq):
        return TIER_PREFIX
    toks = name_tokens(query)
    if toks and all(t in ln for t in toks):
        return TIER_TOKENS
    if lq in ln:
        return TIER_SUBSTRING
    if difflib.SequenceMatcher(None, ln, lq).ratio() >= FUZZY_MIN_RATIO:
        return TIER_FUZZY
    # A typo inside one token of a longer name: compare against each token run.
    nt = name_tokens(name)
    if nt and any(difflib.SequenceMatcher(None, t, lq).ratio() >= FUZZY_MIN_RATIO for t in nt):
        return TIER_FUZZY
    return 0


def score(row: dict, query: str) -> float:
    """Ranking score of one candidate row from `GraphStore.find_symbols`."""
    tier = match_tier(str(row.get("name") or ""), query)
    if tier == 0:
        return 0.0
    s = float(tier)
    kind = str(row.get("kind") or "")
    if kind in _GOOD_KINDS:
        s += 10
    elif kind in _WEAK_KINDS:
        s -= 10
    degree = int(row.get("in_degree") or 0)
    rank = float(row.get("rank") or 0.0)
    s += 3 * math.log1p(max(degree, rank * 10))
    if row.get("is_exported"):
        s += 5
    if _LOW_PATH.search(str(row.get("file") or "")):
        s -= 15
    # Short names win ties among substring hits: `user` before `userPreferencesCache`.
    s -= min(len(str(row.get("name") or "")), 60) / 60.0
    return max(s, 0.01)
