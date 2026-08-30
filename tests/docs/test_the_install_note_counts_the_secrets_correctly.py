"""`.env.example` told a new installer to fill in four secrets by hand.

`init-env.sh` generates fourteen and asks for none of them. Somebody
following the header went looking for four blanks that are not there, in a
file of three hundred lines, on their first five minutes with the product.

The number was probably right once. Keyed on the property so it stays right:
the secrets the header names are the secrets the script generates, both ways.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INIT = ROOT / "scripts" / "init-env.sh"
EXAMPLE = ROOT / ".env.example"


def _generated() -> set[str]:
    """The keys init-env.sh makes a value for, from its own generator table."""
    text = INIT.read_text()
    table = re.search(r"GENERATED\s*[:=].*?\{(.*?)\n\}", text, re.S)
    body = table.group(1) if table else text
    keys = set(re.findall(r'"([A-Z][A-Z0-9_]+)":\s*(?:lambda|fernet_key)', body))
    assert keys, "no generator entries found; this test is reading the wrong file"
    return keys


def _header() -> str:
    """The first forty lines — what a first-time installer actually reads.

    Bounded on purpose: the rest of the file names nearly every variable there
    is, so scanning all of it would compare the list against itself and pass
    whatever the header said.
    """
    return "\n".join(EXAMPLE.read_text().splitlines()[:40])


def _instructions(text: str) -> str:
    """The header with quoted spans removed.

    A phrase inside quotation marks is a citation, not an instruction — the
    header now quotes the old wording in order to correct it, and a scan that
    cannot tell the difference reports the correction as the mistake. This
    file's first version did exactly that.
    """
    return re.sub(r"[\"\u201c][^\"\u201d]*[\"\u201d]", " ", text)


def _named_in_header() -> set[str]:
    return set(re.findall(r"\b([A-Z][A-Z0-9_]{4,})\b", _header())) & _generated()


def test_the_header_names_every_generated_secret() -> None:
    missing = _generated() - _named_in_header()
    assert not missing, (
        f"init-env.sh generates these and the .env.example header does not "
        f"name them: {sorted(missing)}. The header is what a first-time "
        f"installer reads."
    )


def test_the_header_does_not_promise_manual_work_the_script_does() -> None:
    assert not re.search(r"fill in the (four|three|five|\d+) generated",
                         _instructions(_header())), (
        "the header still tells the installer to fill in generated secrets by "
        "hand; init-env.sh writes all of them"
    )


def test_the_count_is_not_stated_wrongly() -> None:
    """If the header gives a number, it has to be the number."""
    head = _instructions(_header())
    words = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
             "fourteen": 14, "fifteen": 15, "sixteen": 16}
    actual = len(_generated())
    for match in re.finditer(r"generates (\w+)", head):
        word = match.group(1).lower()
        stated = words.get(word) or (int(word) if word.isdigit() else None)
        if stated is not None:
            assert stated == actual, (
                f"the header says init-env.sh generates {word} secrets; it "
                f"generates {actual}"
            )
