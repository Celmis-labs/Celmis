"""Password policy (Stage 23).

Deliberately modest: length does far more for real-world strength than forcing
symbol classes, so we require a decent minimum length, a couple of character
classes, and block the passwords attackers actually try first. The same rules
back both the API validator and the strength meter in the UI.
"""

from __future__ import annotations

import re

MIN_LENGTH = 10
MAX_LENGTH = 256

# Substrings that make a password worthless regardless of the rest.
_COMMON = frozenset({
    "password", "passwd", "qwerty", "asdfgh", "zxcvbn", "111111", "123456",
    "12345678", "123456789", "1234567890", "letmein", "welcome", "admin",
    "iloveyou", "monkey", "dragon", "sunshine", "princess", "football",
    "abc123", "changeme", "secret", "master", "login", "celmis",
})

_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"\d")
_SYMBOL = re.compile(r"[^A-Za-z0-9]")


def password_problems(password: str, *, email: str = "") -> list[str]:
    """Return a list of human-readable problems. Empty list = acceptable."""
    problems: list[str] = []
    pwd = password or ""

    if len(pwd) < MIN_LENGTH:
        problems.append(f"must be at least {MIN_LENGTH} characters")
    if len(pwd) > MAX_LENGTH:
        problems.append(f"must be at most {MAX_LENGTH} characters")

    classes = sum(bool(rx.search(pwd)) for rx in (_UPPER, _LOWER, _DIGIT, _SYMBOL))
    if classes < 3:
        problems.append(
            "must mix at least three of: lowercase, uppercase, digits, symbols"
        )

    lowered = pwd.lower()
    if any(c in lowered for c in _COMMON):
        problems.append("contains a commonly guessed word")

    # Whitespace-only padding and single repeated characters.
    if pwd.strip() != pwd:
        problems.append("must not start or end with whitespace")
    if pwd and len(set(pwd)) <= 3:
        problems.append("is too repetitive")

    # Don't let the password be (part of) the account identity.
    local = (email or "").split("@", 1)[0].lower()
    if local and len(local) >= 4 and local in lowered:
        problems.append("must not contain your email address")

    return problems


def validate_password(password: str, *, email: str = "") -> None:
    """Raise ``ValueError`` describing every problem, or return None."""
    problems = password_problems(password, email=email)
    if problems:
        raise ValueError("Password " + "; ".join(problems) + ".")


def password_score(password: str) -> int:
    """0–4 strength score for the UI meter (not a security control)."""
    pwd = password or ""
    if len(pwd) < MIN_LENGTH:
        return 0
    score = 1
    classes = sum(bool(rx.search(pwd)) for rx in (_UPPER, _LOWER, _DIGIT, _SYMBOL))
    if classes >= 3:
        score += 1
    if len(pwd) >= 14:
        score += 1
    if len(pwd) >= 18 and classes == 4:
        score += 1
    if any(c in pwd.lower() for c in _COMMON):
        score = min(score, 1)
    return max(0, min(4, score))


__all__ = [
    "MIN_LENGTH", "MAX_LENGTH", "password_problems", "password_score",
    "validate_password",
]
