"""A 101,000-line repository under one root commit needs an explanation.

That shape is what a code drop of unclear origin looks like to a provenance
scanner, and it is what this repository genuinely is — the history was
rebuilt because the old one named a customer throughout. The difference
between "unexplained drop" and "explained rebuild" is a file.

What this pins is narrow on purpose: that the record EXISTS, that it does not
quietly turn into a licence, and that nobody adds a licence without also
resolving the ownership question the record describes. The last one is the
trap: applying Apache 2.0 grants patent rights irrevocably, and rights that
are not held cannot be granted.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / "PROVENANCE.md"


def test_the_record_exists():
    assert PROVENANCE.exists(), (
        "one root commit over 101k lines with no origin record reads as a "
        "code drop to every provenance scanner there is"
    )


def test_it_accounts_for_the_single_root_commit():
    """A provenance scanner sees one commit holding the whole tree, and that
    shape asks a question. The file has to answer it — not with an inventory of
    what came before, which is nobody else's business, but with the two things
    a reader actually needs: that development happened privately first, and
    that none of it is required to build or audit what is here."""
    body = PROVENANCE.read_text(encoding="utf-8").lower()
    assert "single commit" in body, "the shape of the history is not accounted for"
    assert "not published" in body
    assert "build" in body and "audit" in body, (
        "the record says the history is absent without saying that its absence "
        "costs the reader nothing — which is the half that matters"
    )


def test_it_states_the_licence_position_without_claiming_more():
    """It must state the licence — that is the fact a reader needs.

    This used to assert the opposite: that the file said no licence had been
    chosen. AGPL-3.0 was chosen, the section was replaced as it promised it
    would be, and the assertion moved with it. The test that outlived its
    subject is the one worth noticing — it asserted a STATE, and the state was
    always going to change, so what it really needed to pin was the shape:
    name the position, do not reach past it.

    The guard below is the durable half. The first draft of this file
    explained the missing licence by describing the project as a
    generalisation of work done at an employer. That sentence asserted a
    connection the code does not contain, in the one place a record is meant
    to be checkable, and it would have been permanent.
    """
    body = PROVENANCE.read_text(encoding="utf-8").lower()
    assert "agpl-3.0" in body, "the record does not name the licence"
    assert "license" in body or "licence" in body
    for overreach in ("generalis", "employ"):
        assert overreach not in body, (
            f"the record makes a claim about {overreach!r} that belongs in a "
            f"conversation with a lawyer, not in a permanent file"
        )


def test_a_licence_has_not_appeared_without_the_record_being_updated():
    """The failure this catches: somebody adds LICENSE because a tool asked
    for one, while PROVENANCE.md still says nobody knows who owns the code.
    Two files in the same tree disagreeing about that is the worst outcome —
    it looks deliberate."""
    licence = ROOT / "LICENSE"
    if not licence.exists():
        return
    body = PROVENANCE.read_text(encoding="utf-8").lower()
    assert "licence has not been chosen" not in body, (
        "LICENSE exists while PROVENANCE.md still says no licence has been "
        "chosen — two files in one tree disagreeing about that reads as "
        "deliberate"
    )


def test_the_readme_points_at_it():
    """A record nobody is sent to is a record nobody reads."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "PROVENANCE.md" in readme
