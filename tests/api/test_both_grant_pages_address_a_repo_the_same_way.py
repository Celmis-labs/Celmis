"""Two admin pages granted access to a repository, in two different spellings.

Access (`web/app/(app)/admin/access/page.tsx`) binds its picker to `r.slug` —
the indexed slug that every `{repo_slug}` route, the clone directory, the vault
and five tables are keyed on. Teams (`web/app/(app)/admin/teams/page.tsx`) had
a free-text box whose own placeholder read "owner/repo".

So which enforcement family could see a grant depended on which admin page the
admin happened to open, and nothing anywhere objected: on the deployed instance
both spellings were granted through the API and both were stored, as two rows
for one repository.

The lookup now accepts either spelling (see
tests/security/test_a_grant_is_found_however_it_was_typed.py) so existing rows
keep working. This pins the other half: the product must stop MINTING the
ambiguity. A picker over the workspace's own repositories cannot produce a
spelling the rest of the system does not use.

These read the TSX as text because Python cannot parse it. They check for the
one binding that matters rather than for prose, and the placeholder assertion
is over JSON, which is data.
"""

from __future__ import annotations

import json
import pathlib

import pytest

WEB = pathlib.Path(__file__).resolve().parents[2] / "web"
PAGES = {
    "access": WEB / "app/(app)/admin/access/page.tsx",
    "teams": WEB / "app/(app)/admin/teams/page.tsx",
}


@pytest.mark.parametrize("page", sorted(PAGES))
def test_the_grant_control_is_bound_to_the_indexed_slug(page):
    src = _uncommented(PAGES[page])

    assert "value: r.slug" in src, (
        f"the {page} page grants against something other than the repo slug"
    )


@pytest.mark.parametrize("page", sorted(PAGES))
def test_the_repository_list_comes_from_the_workspace(page):
    src = PAGES[page].read_text(encoding="utf-8")

    assert '"/api/repos"' in src, (
        f"the {page} page does not offer the workspace's own repositories"
    )


def test_no_locale_still_tells_the_admin_to_type_a_path():
    """The placeholder taught the wrong spelling in sixteen languages."""
    offenders = []
    for f in sorted((WEB / "lib/i18n/messages").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        value = data.get("admin.teams.repoSlugPlaceholder")
        if value and "/" in value:
            offenders.append(f"{f.name}: {value!r}")

    assert not offenders, offenders


def _uncommented(path: pathlib.Path) -> str:
    """TSX with `//` and `{/* */}` removed.

    Both halves of this file were satisfiable by a comment: restore the
    free-text Input, leave the Select in a `{/* */}` block, and the suite went
    green. An absence assertion that a comment can satisfy asserts nothing.
    """
    import re

    src = path.read_text(encoding="utf-8")
    # `{/* … */}` is how a comment is written inside JSX, which is the only
    # place these assertions can be dodged. A plain `/* … */` sweep is NOT
    # done: an unpaired `/*` inside a string literal made it swallow 7KB of
    # this very file, including the binding under test — a stripper that
    # removes the evidence turns a real check into a passing one.
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


def test_the_teams_page_no_longer_takes_free_text_for_a_grant():
    src = _uncommented(PAGES["teams"])

    # Any handler spelling, not one exact string: `e.currentTarget.value`
    # sidesteps `e.target.value` and means the same thing.
    assert "setNewRepoSlug(e." not in src, (
        "a typed repository name is a spelling nobody else has to agree with"
    )
    assert "setNewRepoSlug(v)" in src, "the picker no longer sets the slug"
