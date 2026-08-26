"""The repository had no LICENSE at all, which means "all rights reserved".

An open repository in that state is one nobody may legally run or fork — the
opposite of what publishing it is for. Celmis is AGPL-3.0 with one exception,
drawn by path so that git shows it and no separate registry of covered files
can drift.

`ee/` holds no product code and is expected not to for a while. That is the
point: adding the boundary after the first outside contribution means
re-asking every contributor who has already sent work under an unqualified
AGPL, because a contribution arrives under the licence it was made under. The
line costs an hour now and a negotiation later.

These are cheap guards on facts that are easy to lose in a move between
repositories — which is exactly when they will be lost.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_licence_exists_and_is_the_agpl():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text


def test_the_licence_carries_the_whole_text_not_a_reference():
    """A pointer to gnu.org is not a licence grant."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "13. Remote Network Interaction" in text, "§13 is the one that binds us"
    assert len(text) > 30_000, "the full AGPL is ~34KB; this looks like a stub"


def test_the_exception_is_stated_before_the_licence_text():
    """A reader must meet the carve-out before they meet the grant."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    exception = text.index("ee/")
    grant = text.index("GNU AFFERO GENERAL PUBLIC LICENSE")

    assert exception < grant


def test_the_exception_names_both_halves_of_the_rule():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "`ee/`" in text or "ee/" in text
    assert ".ee." in text, "the filename half of the rule is missing"
    assert "LICENSE_EE" in text


@pytest.mark.parametrize("name", ["LICENSE", "LICENSE_EE", "CONTRIBUTING.md",
                                  "ee/README.md"])
def test_the_file_is_present(name):
    assert (ROOT / name).is_file(), f"{name} is missing"


def test_the_enterprise_licence_permits_evaluation():
    """A boundary that forbids trying the thing is a boundary nobody crosses."""
    text = (ROOT / "LICENSE_EE").read_text(encoding="utf-8")

    assert "evaluation" in text.lower()


def test_the_audit_log_is_promised_to_stay_free():
    """The console may one day be commercial; the record never is. Both files
    have to say so, because they are read by different people."""
    for name in ("LICENSE_EE", "ee/README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "src/security/audit.py" in text, f"{name} does not name the log"


def test_contributing_states_where_enterprise_code_goes():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "ee/" in text
    assert "AGPL" in text


def test_no_product_code_has_crossed_the_boundary_yet():
    """If this ever fails it is not a bug — it is a decision that has to be
    made deliberately, with the licence consequences in view."""
    strays = [
        p for p in (ROOT / "ee").rglob("*")
        if p.is_file() and p.suffix in {".py", ".ts", ".tsx"}
    ]

    assert not strays, f"code moved behind the licence boundary: {strays}"


def test_the_distribution_is_named_for_the_product():
    """The name lands in image paths and other people's compose files the
    moment there is a tag, so it has to be right before the first one."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'name = "celmis"' in text
    assert "code-analysis-system" not in text


def test_the_old_distribution_name_still_resolves():
    """A container built before the rename carries the old name; a version
    lookup that answers "unknown" during a rollout is a worse answer than a
    two-element tuple."""
    from src import DISTRIBUTIONS

    assert DISTRIBUTIONS[0] == "celmis"
    assert "code-analysis-system" in DISTRIBUTIONS
