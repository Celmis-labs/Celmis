"""Every branch picker is the searchable one.

Production: on a Bitbucket repository with hundreds of branches, the branch
dropdowns on /repositories (the per-repo branch chip and the PR filter),
/dependencies (the run-wide override and the per-repo table) and the review
policy's target branches showed the first provider page — 100 names — as if
it were the whole list, with no search. The server now walks every page and
searches (tests/repos/test_a_branch_list_is_every_branch.py); this guards the
other half: that each of those pages actually uses `BranchCombobox`, which
asks the server per search term, and that no page goes back to fetching
`/branches` itself and dropping the answer into a plain <Select>.

Comments are stripped before anything is matched: these files are full of
prose ABOUT the old Select and the endpoint, and a guard that reads comments
passes on the explanation of why the code is gone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
COMBOBOX = WEB / "components" / "branch-combobox.tsx"

PICKER_FILES = [
    WEB / "app" / "(app)" / "repositories" / "page.tsx",
    WEB / "app" / "(app)" / "dependencies" / "page.tsx",
    WEB / "app" / "(app)" / "admin" / "review-policies" / "[slug]" / "page.tsx",
    WEB / "components" / "repo-branch-table.tsx",
]


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # `//` comments, but not the `//` inside a string such as "https://…".
    return re.sub(r"(^|[^:\"'`])//[^\n]*", r"\1", src)


def _code(path: Path) -> str:
    return _strip_comments(path.read_text())


@pytest.mark.parametrize("path", PICKER_FILES, ids=lambda p: p.parent.name or p.name)
def test_the_page_uses_the_searchable_picker(path: Path) -> None:
    code = _code(path)
    assert re.search(r'from\s+"@/components/branch-combobox"', code), path
    assert "<BranchCombobox" in code, f"{path} has no <BranchCombobox>"


def test_no_page_fetches_the_branch_list_itself() -> None:
    """The endpoints are reached through `branchesApi` / `reviewPoliciesApi`
    only, which send `q` and `limit`. A page calling `/branches` directly is
    a page about to render one unsearchable slice again."""
    offenders = []
    for path in [*(WEB / "app").rglob("*.tsx"), *(WEB / "components").rglob("*.tsx")]:
        code = _code(path)
        if re.search(r"/branches[`'\"?]", code):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_the_api_helpers_send_search_and_limit() -> None:
    code = _code(WEB / "lib" / "api.ts")
    helper = re.search(r"function branchQuery\(.*?\n}\n", code, flags=re.S)
    assert helper, "branchQuery helper missing from lib/api.ts"
    assert '"q"' in helper.group(0) and "limit" in helper.group(0)
    assert re.search(r"/api/repos/\$\{encodeURIComponent\(slug\)\}/branches\?\$\{branchQuery", code)
    assert re.search(r"/api/review-policies/\$\{encodeURIComponent\(slug\)\}/branches\?\$\{branchQuery",
                     code)


def test_the_combobox_searches_the_server_and_says_what_it_hides() -> None:
    code = _code(COMBOBOX)
    # debounced server search keyed by the term
    assert re.search(r"queryKey:\s*\[\.\.\.queryKey,\s*debounced\]", code)
    assert re.search(r"queryFn:\s*\(\)\s*=>\s*search\(debounced\)", code)
    assert "DEBOUNCE_MS" in code
    # accessible: combobox + listbox + active descendant
    for needle in ('role="combobox"', 'role="listbox"', 'role="option"',
                   "aria-activedescendant", '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"'):
        assert needle in code, needle
    # it says when there is more than it shows
    assert 't("branchPicker.more"' in code
    assert 't("branchPicker.truncated")' in code


def test_every_picker_string_exists_in_english() -> None:
    en = json.loads((WEB / "lib" / "i18n" / "messages" / "en.json").read_text())
    used = set(re.findall(r't\("(branchPicker\.[A-Za-z]+)"', _code(COMBOBOX)))
    assert used, "the combobox uses no translated strings?"
    assert not sorted(used - set(en)), sorted(used - set(en))


def test_free_text_is_offered_only_where_it_was_before() -> None:
    """Typing a branch by hand was possible on the dependencies override and
    the policy's target branches; the repository chip and PR filter only
    ever offered listed names."""
    allow = {p.parent.name if p.name == "page.tsx" else p.name: "allowCustom" in _code(p)
             for p in PICKER_FILES}
    assert allow == {
        "repositories": False,
        "dependencies": True,
        "[slug]": True,
        "repo-branch-table.tsx": False,
    }
