"""Every locale carries every key, with the same placeholders.

Sixteen locales drift the same way every time: a feature lands with `en` and
`uk` written by whoever built it, and the other fourteen quietly fall behind.
A missing key is not a crash — it renders as the raw key id or the English
fallback, so it survives review and ships. Twenty-two keys had accumulated that
way before this test existed.

The placeholder check is the sharper of the two. `{count}` dropped in
translation does not fall back to English, it renders a sentence with the
number silently missing; `{cout}` renders the typo. Both look like finished
copy to anyone who does not read that language.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

MESSAGES = Path(__file__).resolve().parents[2] / "web" / "lib" / "i18n" / "messages"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

BASE = "en"


def _load(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text())


def _locales() -> list[str]:
    return sorted(p.stem for p in MESSAGES.glob("*.json") if p.stem != BASE)


def test_there_are_locales_to_check() -> None:
    """Guards the guard: a bad glob would make every test below vacuous."""
    assert len(_locales()) >= 14, _locales()


@pytest.mark.parametrize("locale", _locales())
def test_locale_has_every_key(locale: str) -> None:
    missing = sorted(set(_load(BASE)) - set(_load(locale)))
    assert not missing, (
        f"{locale}.json is missing {len(missing)} key(s): "
        + ", ".join(missing[:12])
        + (" …" if len(missing) > 12 else "")
    )


@pytest.mark.parametrize("locale", _locales())
def test_locale_keeps_every_placeholder(locale: str) -> None:
    base, other = _load(BASE), _load(locale)
    wrong: list[str] = []
    for key, source in base.items():
        if key not in other:
            continue  # reported by the test above; not worth failing twice
        want, got = sorted(PLACEHOLDER.findall(source)), sorted(PLACEHOLDER.findall(other[key]))
        if want != got:
            wrong.append(f"{key}: expected {want}, found {got}")
    assert not wrong, f"{locale}.json placeholder mismatch:\n" + "\n".join(wrong)


@pytest.mark.parametrize("locale", _locales())
def test_locale_has_no_empty_strings(locale: str) -> None:
    blank = sorted(k for k, v in _load(locale).items() if not v.strip())
    assert not blank, f"{locale}.json has blank values: {blank}"
