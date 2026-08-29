"""`gh --json` fails the whole step when a field name does not exist.

v0.1.11 shipped its images and its release, then went red on the last line
of the last step: `gh release view "$tag" --json tagName,isLatest`.  There
is no isLatest field, so gh printed the list of real ones and exited 1.
The line was decorative — it displayed the result — which is why nothing
before it noticed.

Keyed on the property, not on that one typo: every field named in a
`gh ... --json` call has to be a field that subcommand actually offers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# `gh <subcommand> --json` with no value prints its own field list; these are
# copied from that output (gh 2.x).  A subcommand absent from this table is a
# failure, not a pass: the point is that somebody checked.
FIELDS: dict[str, frozenset[str]] = {
    "release view": frozenset(
        [
            "apiUrl",
            "assets",
            "author",
            "body",
            "createdAt",
            "databaseId",
            "id",
            "isDraft",
            "isImmutable",
            "isPrerelease",
            "name",
            "publishedAt",
            "tagName",
            "tarballUrl",
            "targetCommitish",
            "uploadUrl",
            "url",
            "zipballUrl",
        ]
    ),
    "release list": frozenset(
        ["createdAt", "isDraft", "isLatest", "isPrerelease", "name", "publishedAt", "tagName"]
    ),
}

# `gh release view --json tagName,isLatest`  ->  ("release view", "tagName,isLatest")
# The subcommand capture is greedy on purpose: non-greedy stops at the first
# word and reads `gh release view` as the subcommand `release`. Flags and
# quoted arguments cannot be swallowed — neither matches [a-z]+.
CALL = re.compile(r"\bgh\s+([a-z]+(?:\s+[a-z]+)*)\s+[^\n]*?--json\s+([A-Za-z0-9_,]+)")


def _shell_lines(path: Path) -> list[tuple[int, str]]:
    """Every `run:` line, minus its comments.

    A comment mentioning --json is prose about the flag, not a use of it.
    Scanning raw text instead would make this test read its own docstring
    in the workflow and fail on the words describing the bug.
    """
    doc = yaml.safe_load(path.read_text()) or {}
    out: list[tuple[int, str]] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            name = step.get("name", "?")
            for raw in script.splitlines():
                code = raw.split("#", 1)[0]
                if code.strip():
                    out.append((name, code))
    return out


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_json_field_exists(workflow: Path) -> None:
    for step_name, line in _shell_lines(workflow):
        for subcommand, fields in CALL.findall(line):
            where = f"{workflow.name} :: {step_name}"
            known = FIELDS.get(subcommand)
            assert known is not None, (
                f"{where}: `gh {subcommand} --json` is not in this test's "
                f"table. Run `gh {subcommand} --json` to print the real "
                f"field list and add it to FIELDS."
            )
            for field in fields.split(","):
                assert field in known, (
                    f"{where}: `gh {subcommand}` has no --json field "
                    f"{field!r}; gh exits 1 and fails the step. "
                    f"Available: {', '.join(sorted(known))}"
                )


def test_the_scanner_can_actually_see_a_bad_field(tmp_path: Path) -> None:
    """The test above passes when no workflow calls --json at all.

    That silence has to mean 'nothing to object to', not 'nothing was
    read', so give the scanner a workflow with the v0.1.11 line in it.
    """
    bad = tmp_path / "w.yml"
    bad.write_text(
        "jobs:\n"
        "  publish:\n"
        "    steps:\n"
        "      - name: Publish\n"
        "        run: |\n"
        "          # a comment naming --json isLatest is not a call\n"
        '          gh release view "$tag" --json tagName,isLatest\n'
    )
    with pytest.raises(AssertionError, match="isLatest"):
        test_every_json_field_exists(bad)

    good = tmp_path / "ok.yml"
    good.write_text(
        "jobs:\n"
        "  publish:\n"
        "    steps:\n"
        "      - name: Publish\n"
        "        run: |\n"
        "          # --json isLatest, mentioned only in prose\n"
        '          gh release view "$tag" --json tagName,isDraft\n'
    )
    test_every_json_field_exists(good)
