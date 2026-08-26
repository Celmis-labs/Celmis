"""Severity normalisation shared by every dependency-audit source.

Native auditors, OSV and the hygiene checks all speak slightly different
severity dialects (`moderate`, `info`, a bare CVSS score, a CVSS vector).
Everything downstream — the DB column, the summary counters, the UI badge —
speaks exactly one: none | low | medium | high | critical.

The CVSS v3 base-score calculator is the official formula (spec 3.1 §7.1).
It matters more than it looks: OSV advisories carry the *vector*, not the
score, so without it every CVSS-only advisory silently collapsed to "medium".
"""

from __future__ import annotations

import math
from typing import Any

SEV_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Vocabularies seen in the wild → ours.
_ALIASES = {
    "moderate": "medium",       # GHSA / npm / pnpm / yarn
    "info": "low",              # npm audit
    "informational": "low",
    "warning": "low",
    "notice": "low",
    "important": "high",        # RedHat/Ruby advisories
    "": "",
}


def normalize_severity(value: Any, *, default: str = "medium") -> str:
    """Any advisory's severity word → our vocabulary.

    `default` is what an advisory with an unusable severity gets: an advisory
    exists, so "none" would be a lie — but "critical" would be alarmism.
    """
    s = str(value or "").strip().lower()
    s = _ALIASES.get(s, s)
    if s in SEV_ORDER:
        return s
    return default


def severity_from_score(score: float) -> str:
    """CVSS qualitative rating (spec 3.1 §5)."""
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


# ─── CVSS v3 base score ──────────────────────────────────────────────

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}
_AC = {"L": 0.77, "H": 0.44}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.50}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def _roundup(value: float) -> float:
    """CVSS 3.1 Roundup — ceil to one decimal, guarding float noise."""
    scaled = int(round(value * 100_000))
    if scaled % 10_000 == 0:
        return scaled / 100_000.0
    return (math.floor(scaled / 10_000) + 1) / 10.0


def cvss_base_score(vector: str) -> float | None:
    """Base score for a CVSS v3.x vector string, or None if not parseable.

    A bare number ("9.8") is accepted too — some feeds put the score where the
    vector belongs. CVSS v2 and v4 vectors return None (the caller falls back
    to the advisory's own severity word).
    """
    text = str(vector or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if not text.upper().startswith("CVSS:3"):
        return None

    parts: dict[str, str] = {}
    for chunk in text.split("/")[1:]:
        key, _, val = chunk.partition(":")
        if val:
            parts[key.strip().upper()] = val.strip().upper()
    try:
        scope_changed = parts["S"] == "C"
        av = _AV[parts["AV"]]
        ac = _AC[parts["AC"]]
        pr = (_PR_C if scope_changed else _PR_U)[parts["PR"]]
        ui = _UI[parts["UI"]]
        conf, integ, avail = _CIA[parts["C"]], _CIA[parts["I"]], _CIA[parts["A"]]
    except KeyError:
        return None

    iss = 1.0 - ((1.0 - conf) * (1.0 - integ) * (1.0 - avail))
    # The two branches are the two distinct published CVSS 3.1 impact
    # sub-formulas (scope changed vs unchanged). Folding them into a single
    # ternary hides which formula applies, and these constants have to stay
    # readable against the spec.
    if scope_changed:  # noqa: SIM108
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    raw = (1.08 if scope_changed else 1.0) * (impact + exploitability)
    return _roundup(min(raw, 10.0))


def severity_from_vector(vector: str, *, default: str = "medium") -> str:
    score = cvss_base_score(vector)
    return severity_from_score(score) if score is not None else default


def worst_severity(items: list[dict[str, Any]]) -> str:
    """Highest severity across a list of {"severity": …} dicts."""
    worst = "none"
    for item in items:
        s = normalize_severity(item.get("severity"), default="none")
        if SEV_ORDER.get(s, 0) > SEV_ORDER[worst]:
            worst = s
    return worst


__all__ = [
    "SEV_ORDER",
    "cvss_base_score",
    "normalize_severity",
    "severity_from_score",
    "severity_from_vector",
    "worst_severity",
]
