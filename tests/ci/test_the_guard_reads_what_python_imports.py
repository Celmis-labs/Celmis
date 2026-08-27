"""The release guard reads the version with `sed`. Python reads it by import.

`ce24813` added a step to release.yml that refuses to build a tag disagreeing
with `src/__init__.py`. It is the right place for that comparison — it is the
only place where a tag and the file exist at once, and no test can see a tag
that does not exist yet.

But the step reads the literal with

    sed -n 's/^__version__ = "\\(.*\\)"/\\1/p' src/__init__.py

and that program does not ask Python anything. It asks for a byte pattern:
column zero, exactly one space either side of `=`, double quotes, nothing
after the closing quote, LF endings. Five edits nobody would think twice about
break it while every existing test stays green — measured, on the real
program, with tests/api/test_the_api_says_which_build_is_running.py's AST
check passing throughout:

    __version__ = "0.1.9"                     sed -> "0.1.9"     ← only this one
    __version__ = '0.1.9'                     sed -> ""
    __version__="0.1.9"                       sed -> ""
    __version__ =  "0.1.9"                    sed -> ""
    __version__ = "0.1.9"  # bumped with tag  sed -> "0.1.9  # bumped with tag"
    __version__ = "0.1.9"\\r\\n                 sed -> "0.1.9\\r"

Each makes `[ "$tag" = "v$lit" ]` false, so `git push --tags` fails all three
matrix legs on a file that is objectively correct. And ci.yml does not run on
tags, so the first signal is a red release — the one moment where a confusing
failure costs the most.

The existing AST test asserts the value IS a literal. This one asserts it is a
literal SHAPED THE WAY THE PIPELINE CAN READ, which is a different claim and
the one the release depends on.

It takes the sed program OUT of the workflow rather than restating it, and
compares against the IMPORTED value rather than a second regex. Both matter:
a copy here could drift into agreeing with a wrong sed, and a test that agrees
with the bug is worse than no test.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

#: Steps that cost real time or publish anything. The guard's whole argument
#: is that a mismatch is caught before these run.
EXPENSIVE = ("setup-buildx", "build-push-action", "login-action")


def _steps() -> list[dict]:
    doc = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    return doc["jobs"]["images"]["steps"]


def _guard_index() -> int:
    """The one step that reads src/__init__.py.

    Matched on the PARSED `run:` scalar, never the raw file: the comment above
    the step names the same path, and a grep would find it there and call a
    deleted guard present.
    """
    hits = [i for i, s in enumerate(_steps()) if "src/__init__.py" in (s.get("run") or "")]
    assert len(hits) == 1, (
        f"expected exactly one step reading src/__init__.py, found {len(hits)} — "
        f"the tag/literal guard was removed or duplicated"
    )
    return hits[0]


def test_the_guard_extracts_exactly_what_python_imports():
    """Byte for byte, including the trailing newline.

    The newline is not pedantry: it is what separates a healthy read from the
    CRLF and trailing-comment cases, both of which return a non-empty string
    that looks fine in a log and compares false in the shell.
    """
    from src import __version__

    run = _steps()[_guard_index()]["run"]
    programs = re.findall(r"sed -n ('[^']*'|\"[^\"]*\")", run)
    assert len(programs) == 1, (
        "the guard no longer extracts the literal with a single sed; this test "
        "reads the program out of the workflow and cannot follow it elsewhere"
    )
    out = subprocess.run(
        ["sed", "-n", programs[0][1:-1], "src/__init__.py"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    assert out == (__version__ + "\n").encode(), (
        f"the release guard reads {out!r} out of src/__init__.py where Python "
        f"imports {__version__!r}. Every tag push will fail until the line is "
        f"written the way the sed expects."
    )


def test_the_guard_runs_before_anything_expensive():
    """The commit's own argument, which nothing else holds to.

    "Checked BEFORE the build, so a mismatch costs seconds rather than three
    multi-arch images." True as written; a step reordered above it would make
    it false silently, and the only symptom would be a bigger bill.
    """
    steps = _steps()
    guard = _guard_index()
    for i, step in enumerate(steps):
        uses = step.get("uses") or ""
        if any(e in uses for e in EXPENSIVE):
            assert i > guard, (
                f"step {i} ({uses}) runs before the version guard at {guard} — "
                f"a mismatched tag now costs a build instead of a second"
            )


def test_the_guard_fails_when_the_literal_is_unreadable():
    """An empty extraction must block, not pass.

    `lit=""` makes the comparison `[ "$tag" = "v" ]`, false for every real tag.
    Worth pinning: a future rewrite that defaulted the empty case to the tag
    would turn the guard into a formality that agrees with whatever it is
    given.
    """
    run = _steps()[_guard_index()]["run"]
    stripped = "\n".join(
        ln for ln in run.splitlines() if not ln.lstrip().startswith("#")
    )
    assert re.search(r'"v\$\{?lit\}?"', stripped), (
        "the guard no longer compares the tag against v+literal"
    )
    assert "exit 1" in stripped, "the guard no longer fails the job"


@pytest.mark.parametrize("line,reason", [
    ('__version__ = "9.9.9"', None),
    ("__version__ = '9.9.9'", "single quotes"),
    ('__version__="9.9.9"', "no spaces around ="),
    ('__version__ =  "9.9.9"', "two spaces after ="),
    ('__version__ = "9.9.9"  # bumped', "trailing comment"),
])
def test_the_program_this_test_runs_is_the_one_that_breaks(tmp_path, line, reason):
    """A guard on the guard: prove the sed really is this picky.

    Without this, `test_the_guard_extracts_exactly_what_python_imports` could
    pass for ever against a sed that had quietly become permissive, and the
    docstring above would be describing a hazard that no longer exists.
    """
    run = _steps()[_guard_index()]["run"]
    program = re.findall(r"sed -n ('[^']*'|\"[^\"]*\")", run)[0][1:-1]
    probe = tmp_path / "v.py"
    probe.write_text(line + "\n", encoding="utf-8")
    out = subprocess.run(["sed", "-n", program, str(probe)],
                         capture_output=True, check=True).stdout
    if reason is None:
        assert out == b"9.9.9\n"
    else:
        assert out != b"9.9.9\n", (
            f"the sed now tolerates {reason}; this file's warning is stale and "
            f"its sibling test is weaker than it reads"
        )


# ─── and the file that reports releases to humans ────────────────────

CHANGELOG = ROOT / "CHANGELOG.md"


def test_the_changelog_has_a_section_for_the_version_being_shipped():
    """It said "Nothing has been tagged or released" while eight tags were public.

    Every clause of that sentence was false, in the root-level file a
    self-hoster opens to decide whether to upgrade. It survived for the same
    reason the literal did: nothing read it, so nothing noticed.

    The CI guard ties the tag to the literal. This ties the literal to a human
    record of what is in it — the third side of the same triangle, and the
    only one a person actually reads. Bumping the version without saying what
    changed now fails here, locally, in a second, instead of shipping a
    release that documents itself as not existing.
    """
    from src import __version__

    text = CHANGELOG.read_text(encoding="utf-8")
    heading = f"## [{__version__}]"
    assert heading in text, (
        f"src/__init__.py says {__version__} and CHANGELOG.md has no "
        f"'{heading}' section. Write what is in the release before tagging it."
    )


def test_the_changelog_does_not_claim_the_project_is_unreleased():
    """The specific sentence, pinned so it cannot come back by rebase.

    Keyed on the claim rather than on its wording: any phrasing that tells a
    reader nothing has shipped is wrong while `src/__init__.py` names a
    version above the placeholder it started from.
    """
    text = CHANGELOG.read_text(encoding="utf-8").lower()
    for claim in ("nothing has been tagged or released",
                  "no releases yet",
                  "not been released"):
        assert claim not in text, (
            f"CHANGELOG.md still says {claim!r}; releases v0.1.0 onward are "
            f"public and this is the file people check"
        )
