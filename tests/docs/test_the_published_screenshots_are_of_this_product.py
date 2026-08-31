"""The guide shipped screenshots of a UI that no longer exists.

Three of them, and nobody noticed because a PNG is opaque to every check in
this repository. `fix-with-claude-prefilled.png` showed a sidebar reading
"Claude agent" and a page headed "Claude Code"; `alert-to-fix.png` showed
buttons labelled "Fix with Claude" — a label retired in sixteen locales and
guarded against ever coming back — beside an orange banner stating that
alerts are NOT forwarded to Slack, Discord or Google Chat, which is the
opposite of what the product now does. Both were footed `Celmis 0.1.0` and
`0.1.8`.

WHAT THIS CAN AND CANNOT CHECK. It cannot read a picture. Whether a
screenshot shows the current interface is a human judgement and this file
does not pretend otherwise — say so out loud rather than leave a green tick
implying more than was checked.

What it can do is catch the two mechanical halves: a reference that points at
a file which is not there, and a filename that carries a name the product
stopped using. The second is not cosmetic. `fix-with-claude-prefilled.png`
kept the retired feature name alive in a URL on the public site for every day
the file existed, and the filename is the one part of an image a grep can
read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

#: The same name `tests/i18n` retired from every locale, in the spellings a
#: filename can take. Kept here rather than imported: a filename is not a
#: translation, and coupling the two would make one rename drag the other.
RETIRED_IN_FILENAMES = ("fix-with-claude", "fix_with_claude", "claude-agent")


def _referenced() -> list[str]:
    return re.findall(r"!\[[^\]]*\]\((docs/images/[^)]+)\)",
                      README.read_text(encoding="utf-8"))


def test_the_readme_shows_pictures() -> None:
    assert len(_referenced()) >= 5, "the guide stopped showing anything"


@pytest.mark.parametrize("ref", _referenced())
def test_every_referenced_image_is_in_the_tree(ref: str) -> None:
    assert (ROOT / ref).is_file(), (
        f"README.md points at {ref}, which is not in the repository. On the "
        f"site this renders as a broken image on the documentation page."
    )


@pytest.mark.parametrize("ref", _referenced())
def test_no_filename_carries_a_retired_feature_name(ref: str) -> None:
    name = Path(ref).name.lower()
    for retired in RETIRED_IN_FILENAMES:
        assert retired not in name, (
            f"{ref} keeps {retired!r} in its filename. The label was removed "
            f"from every locale and is guarded there; the URL on the public "
            f"site went on carrying it. Rename the file and the reference."
        )


def test_nothing_in_the_images_directory_is_orphaned() -> None:
    """An image nobody references is one nobody re-shoots when the UI moves."""
    on_disk = {p.name for p in (ROOT / "docs" / "images").iterdir() if p.is_file()}
    used = {Path(r).name for r in _referenced()}
    orphans = sorted(on_disk - used)
    assert not orphans, (
        f"docs/images holds files the README never shows: {orphans}. Either "
        f"reference them or delete them — an unreferenced screenshot is one "
        f"that will still be here, stale, when somebody reaches for it."
    )
