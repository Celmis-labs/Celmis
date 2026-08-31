"""A 187,000-line repository under one root commit needs an explanation.

That shape is what a code drop of unclear origin looks like to a provenance
scanner. The difference between "unexplained drop" and "explained beginning"
is a file.

This docstring used to say the history had been rebuilt "because the old one
named a customer throughout". It did not: the discarded history held test
fixtures — example.com, acme.tech, .local machine names from the author's own
git configuration. The sentence was a stronger reason than the truth, placed
where nobody outside could check it, since the history it described is not
published. A justification for withholding evidence is the last place an
overstatement belongs, so it is gone rather than softened.

What this pins is narrow on purpose: that the record EXISTS, that it says who
wrote the code in numbers that can be recomputed, that it does not quietly
turn into a licence, and that it never again weakens in the same commit as the
test standing over it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / "PROVENANCE.md"
GUARD = Path(__file__).resolve()

# Repo-relative, because that is how git names them.
PROVENANCE_PATH = "PROVENANCE.md"
GUARD_PATH = "tests/ci/test_the_repository_says_where_it_came_from.py"


def _git(*args: str) -> str:
    """Run git in the repository, or skip if there is no repository to run in.

    Skipping is honest here and nowhere else in this file: an sdist has no
    .git, and a test that cannot look at history has nothing to say about it.
    Every non-git assertion below runs unconditionally.
    """
    if not (ROOT / ".git").exists():
        pytest.skip("no .git — history assertions need a repository, not a tree")
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True, text=True, check=True, timeout=60,
        )
    except FileNotFoundError:
        pytest.skip("no git binary")
    return out.stdout


def _authorship_section() -> str:
    body = PROVENANCE.read_text(encoding="utf-8")
    start = body.find("## Authorship")
    assert start != -1, (
        "PROVENANCE.md has no Authorship section — an external audit read the "
        "commit metadata and concluded bus factor 1, which is true and which "
        "the record is supposed to say out loud rather than leave to inference"
    )
    end = body.find("\n## ", start + 1)
    return body[start:] if end == -1 else body[start:end]


def test_the_record_exists():
    assert PROVENANCE.exists(), (
        "one root commit over 187k lines with no origin record reads as a "
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


def test_it_names_its_author_and_does_not_hint_at_a_team():
    """The audit finding this answers: bus factor 1, inferred from the log.

    It is a true finding, and the honest move is to say it first. What this
    guards against is the opposite drift — a record that leaves the number of
    people vague so a reader supplies a flattering guess. The deleted version
    of this file did exactly that: it said attribution was "not a statement
    that it was written by one person", which invites a team that never
    existed.
    """
    section = _authorship_section().lower()
    assert "one author" in section, "the record does not say how many people wrote this"
    assert "no second maintainer" in section, (
        "the record does not state the bus factor plainly — an evaluator who "
        "has to derive it will derive it less charitably than the truth"
    )
    for hedge in ("not a statement that it was written by one person",
                  "three authors"):
        assert hedge not in section, (
            f"the record hints at more contributors than there were ({hedge!r}); "
            "an overstatement in the direction that flatters is still an "
            "overstatement, and this one was deleted once already"
        )


def test_the_authorship_numbers_match_the_tag_they_name():
    """Numbers, not adjectives — and checked against the tag they claim.

    This is the assertion the previous guard lost. It used to require the
    string "204 commits"; it was relaxed to "single commit" in the same commit
    that thinned the record, and a phrase that loose is satisfied by any
    wording at all. A count tied to an immutable tag cannot be satisfied by
    vagueness and cannot rot: the tag does not move, so the number stays true.
    """
    section = _authorship_section()

    tags = re.findall(r"\bv\d+\.\d+\.\d+\b", section)
    assert tags, "the authorship numbers name no tag, so nothing can recompute them"
    tag = tags[0]

    refs = _git("tag", "--list", tag).split()
    assert refs == [tag], f"the record pins {tag}, which is not a tag in this repository"

    # Strip the tag before reading integers, or "v0.1.23" would satisfy an
    # assertion about 23 commits by accident.
    stated = {int(n) for n in re.findall(r"\d+", section.replace(tag, ""))}

    commits = len(_git("rev-list", tag).split())
    bodies = _git("log", "--format=%b", tag)
    trailers = len(re.findall(
        r"(?im)^\s*co-authored-by:.*(?:claude|opus|anthropic)", bodies))
    identities = len(set(_git("log", "--format=%ae", tag).split()))

    for label, actual in (("commits", commits),
                          ("model-trailer commits", trailers),
                          ("git identities", identities)):
        assert actual in stated, (
            f"PROVENANCE.md's authorship section does not state the real number "
            f"of {label} at {tag} ({actual}); it states {sorted(stated)}. Either "
            f"the record drifted or the tag it pins is stale — repin it, do not "
            f"loosen this assertion"
        )


def test_the_record_and_its_guard_never_moved_in_one_commit():
    """The failure mode this exists for, which already happened once.

    The commit that thinned PROVENANCE.md from 73 lines to 14 also rewrote the
    assertions in this file, replacing `assert "204 commits"` with
    `assert "single commit"`. A guard edited alongside the thing it guards is
    not a guard. Split the change into two commits: the record, then the
    assertions — and the second commit is where you notice you have to justify
    the first.

    The root commit is exempt: it introduced every file in the tree.
    """
    roots = set(_git("rev-list", "--max-parents=0", "HEAD").split())
    log = _git("log", "--format=%H", "--name-only", "HEAD")

    offenders, sha, touched = [], None, set()

    def close() -> None:
        if sha and sha not in roots and {PROVENANCE_PATH, GUARD_PATH} <= touched:
            offenders.append(sha)

    for line in log.splitlines():
        if re.fullmatch(r"[0-9a-f]{40}", line):
            close()
            sha, touched = line, set()
        elif line.strip():
            touched.add(line.strip())
    close()

    assert not offenders, (
        "these commits changed PROVENANCE.md and this guard together, which is "
        "how the record was weakened last time without anything failing: "
        + ", ".join(offenders[:5])
    )
