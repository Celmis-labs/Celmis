"""A package nobody could look up does not read as current.

THE DEFECT. `outdated_level` returned "none" when there was no latest version,
and `_row` defaulted to the same. "none" is what an up-to-date package gets,
and the table and the exported document both render it that way — so
`idna 2.10`, two majors behind, printed as:

    idna 2.10 -> ? (PyPI, none)

The aggregate histogram already knew better; its own comment says "'unknown'
is not 'up to date'". The row-level field it was built from did not, and the
row is what a person reads.

THE FIX HAS A MIRROR TO AVOID. Once an unresolvable row says "unknown",
anything counting `outdated != "none"` starts counting it as drift and
inflates the headline with packages nobody could check. Both the counter and
the recommendation are narrowed to the three real drift levels.
"""

from __future__ import annotations

import pytest

from src.deps.registries import outdated_level


def test_no_latest_version_is_unknown():
    assert outdated_level("2.10", None) == "unknown"
    assert outdated_level("2.10", "") == "unknown"


def test_unknown_is_not_none():
    """They render identically to a reader and mean opposite things."""
    assert outdated_level("2.10", None) != outdated_level("2.10", "2.10")


@pytest.mark.parametrize("current,latest,expected", [
    ("2.10", "2.10", "none"),
    ("1.2.0", "1.2.8", "patch"),
    ("2.25.1", "2.34.2", "minor"),
    ("3.3.2", "50.0.0", "major"),
])
def test_the_real_levels_are_unchanged(current, latest, expected):
    assert outdated_level(current, latest) == expected


def test_a_newer_installed_version_is_still_none():
    """Ahead of the registry is not drift, and not unknown either."""
    assert outdated_level("3.0.0", "2.9.9") == "none"


def test_the_outdated_counter_excludes_unknown():
    """The mirror-image bug: `!= "none"` was right while "none" was the only
    non-drift value, and starts over-counting the moment "unknown" exists."""
    import ast
    import inspect

    from src.deps import auditor

    tree = ast.parse(inspect.getsource(auditor))
    body = ast.unparse(tree)
    assert "row['outdated'] in ('patch', 'minor', 'major')" in body


def test_the_recommendation_has_its_own_word_for_unknown():
    """"ok" reads as nothing to do. A package whose latest nobody resolved
    needs the reader to decide, and they cannot if the row says it is fine."""
    import ast
    import inspect

    from src.deps import auditor

    body = ast.unparse(ast.parse(inspect.getsource(auditor)))
    assert "'recommendation'] = 'unknown'" in body
