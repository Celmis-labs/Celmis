"""The Codebase section exists, is reachable from where people look for it,
and does not show itself to an install that does not have it.

Four promises, each checked on the code that keeps it:

* the sidebar entry and the three tabs come from ONE list (`SECTION_TABS`),
  sit before Administration (which must stay last: a guide test reads the
  list up to its closing brace), and carry `requiresFeature: "codebase"`, so
  a build without the router shows neither;
* a pack's own page has no tab of its own, so the Packs tab claims it
  (`alsoUnder`) and the tab, the sidebar and the breadcrumb stay lit there;
* the landing page of an invite link is in `UNLISTED_PAGES` (a breadcrumb
  without a sidebar entry) and every route the section names has a page;
* the browser opens none of this by guessing: the invite token travels in the
  path the maintainer shared and in a POST body, never in a query string, and
  it is never kept past the screen that shows it.

Read with comments stripped: a name surviving in prose must not count as the
name surviving in code.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _lift,
    _strip_comments,
)
from tests.web.test_the_seams_between_page_and_server_hold import _compile_and_run

TABS = WEB / "components" / "section-tabs.tsx"
SHELL = WEB / "components" / "app-shell.tsx"
API = WEB / "lib" / "api.ts"
CODEBASE_APP = WEB / "app" / "(app)" / "codebase"
CODEBASE_COMPONENTS = WEB / "components" / "codebase"

ROUTES = {
    "/codebase": CODEBASE_APP / "page.tsx",
    "/codebase/search": CODEBASE_APP / "search" / "page.tsx",
    "/codebase/connect": CODEBASE_APP / "connect" / "page.tsx",
    "/codebase/packs/<id>": CODEBASE_APP / "packs" / "[id]" / "page.tsx",
    "/codebase/join/<token>": CODEBASE_APP / "join" / "[token]" / "page.tsx",
}


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _codebase_sources() -> list:
    return sorted([*CODEBASE_APP.rglob("*.tsx"), *CODEBASE_COMPONENTS.rglob("*.tsx")])


@pytest.fixture
def _needs_web_deps():
    if not TSC.exists():
        pytest.skip("web deps not installed")


# ─── the tab set ─────────────────────────────────────────────────────


def _tab_block() -> str:
    code = _code(TABS)
    m = re.search(r"\n  codebase: \[(.*?)\n  \],", code, re.S)
    assert m, "the codebase tab set changed shape"
    return m.group(1)


def test_the_section_has_three_tabs_and_each_needs_the_feature():
    tabs = re.findall(r'\{ href: "([^"]+)"[^}]*\}', _tab_block())
    assert tabs == ["/codebase", "/codebase/search", "/codebase/connect"]
    for entry in re.findall(r'\{ href: "[^"]+"[^}]*\}', _tab_block()):
        assert 'requiresFeature: "codebase"' in entry, entry


def test_the_section_comes_before_administration_and_administration_stays_last():
    """`test_every_workspace_section_tab_is_linkable` reads the set up to the
    first closing brace after `admin:`; a section placed after it would be
    skipped by that test and by the platform-admin split it makes."""
    code = _code(TABS)
    keys = re.findall(r"\n  (\w+): \[", code[code.index("SECTION_TABS"):])
    assert "codebase" in keys and "admin" in keys
    assert keys.index("codebase") < keys.index("admin")
    assert keys[-1] == "admin", keys


def test_a_tab_object_has_no_closing_brace_inside_it():
    """The guide test's `\\{ href: "…"[^}]*\\}` stops at the first `}`: a
    brace inside a tab (a template literal, an object-valued field) would cut
    the entry short and hide fields after it, `adminOnly` among them."""
    for entry in re.findall(r'\{ href: "[^\n]*\},?\n', _tab_block()):
        assert entry.count("}") == 1, entry


def test_the_packs_tab_claims_the_pack_page_that_has_no_tab_of_its_own():
    first = re.findall(r'\{ href: "[^"]+"[^}]*\}', _tab_block())[0]
    assert "exact: true" in first
    assert 'alsoUnder: ["/codebase/packs"]' in first


@pytest.mark.usefixtures("_needs_web_deps")
@pytest.mark.parametrize("pathname,expected", [
    ("/codebase", True),
    ("/codebase/packs/abc", True),
    ("/codebase/packs", True),
    ("/codebase/search", False),
    ("/codebase/join/abc", False),
    ("/codebase/packsX", False),
    ("/other", False),
])
def test_the_packs_tab_is_lit_on_a_pack_and_only_there(tmp_path, pathname, expected):
    lifted = _lift("matchesTab", _code(TABS), TABS)
    got = _compile_and_run(
        tmp_path, {},
        lifted + "\n\n"
        f"const tab = {{ href: '/codebase', exact: true, alsoUnder: ['/codebase/packs'] }};\n"
        f"console.log(JSON.stringify(matchesTab({json.dumps(pathname)}, tab)));\n",
    )
    assert got is expected


@pytest.mark.usefixtures("_needs_web_deps")
def test_a_tab_without_exact_matches_its_children_but_not_a_lookalike(tmp_path):
    lifted = _lift("matchesTab", _code(TABS), TABS)
    got = _compile_and_run(
        tmp_path, {},
        lifted + "\n\n"
        "const tab = { href: '/codebase/search' };\n"
        "console.log(JSON.stringify(["
        "matchesTab('/codebase/search', tab), matchesTab('/codebase/search/x', tab), "
        "matchesTab('/codebase/searching', tab)]));\n",
    )
    assert got == [True, True, False]


# ─── the sidebar ─────────────────────────────────────────────────────


def test_the_sidebar_entry_is_built_from_the_same_tabs_and_needs_the_feature():
    code = _code(SHELL)
    entry = next(line for line in code.splitlines() if 'labelKey: "nav.codebase"' in line)
    assert 'href: "/codebase"' in entry
    assert 'requiresFeature: "codebase"' in entry
    assert "pages: SECTION_TABS.codebase" in entry


def test_the_sidebar_hides_a_section_whose_feature_is_off():
    code = _code(SHELL)
    assert "requiresFeature?: string" in code
    assert "featureOff(capabilities, s.requiresFeature)" in code


def test_the_sidebar_sits_before_administration():
    code = _code(SHELL)
    assert code.index('labelKey: "nav.codebase"') < code.index('labelKey: "nav.adminSection"')


def test_the_invite_landing_page_is_a_breadcrumb_not_a_sidebar_entry():
    code = _code(SHELL)
    unlisted = code[code.index("const UNLISTED_PAGES"):]
    unlisted = unlisted[:unlisted.index("];")]
    assert '{ href: "/codebase/join", labelKey: "codebase.join.crumb" }' in unlisted


# ─── the pages ───────────────────────────────────────────────────────


@pytest.mark.parametrize("route,page", ROUTES.items(), ids=list(ROUTES))
def test_every_route_of_the_section_has_a_page(route, page):
    assert page.is_file(), f"{route} has no page"
    src = page.read_text(encoding="utf-8")
    assert src.lstrip().startswith('"use client"'), f"{route} must be a client page"
    assert "export default function" in src


def test_every_tab_leads_to_a_page():
    for href in re.findall(r'\{ href: "([^"]+)"[^}]*\}', _tab_block()):
        assert (CODEBASE_APP / href.removeprefix("/codebase").strip("/") / "page.tsx").is_file(), href


def test_the_pack_and_join_pages_read_the_route_parameter_the_way_next_hands_it_over():
    for route in ("/codebase/packs/<id>", "/codebase/join/<token>"):
        code = _code(ROUTES[route])
        assert "params: Promise<{" in code, route
        assert re.search(r"\buse\(params\)", code), route


# ─── hidden is missing ───────────────────────────────────────────────


def test_a_pack_that_is_not_shown_looks_like_one_that_does_not_exist():
    """The API answers 404 for both; the page must not tell them apart, so any
    404 is the same sentence, and nothing else reads the status."""
    code = _code(ROUTES["/codebase/packs/<id>"])
    assert "status === 404" in code
    assert 'codebase.pack.notFoundTitle' in code
    assert code.count("status ===") == 1


def test_the_catalogue_never_hints_at_packs_it_does_not_show():
    """The list is whatever the API returned. The only thing the page does with
    its length is choose between the empty state and the grid: no "N packs", no
    "and 3 more you cannot see"."""
    code = _code(ROUTES["/codebase"])
    uses = re.findall(r"packs\.length[^\n]*", code)
    assert uses == ["packs.length === 0 ? ("], uses
    for word in ("more packs", "restricted_count", "hiddenCount"):
        assert word not in code, word


def test_an_invite_link_that_fails_says_one_thing_whatever_the_reason():
    """Wrong, expired, revoked, used up and not-for-you are one answer from the
    server; showing its wording, or telling them apart, would undo that."""
    code = _code(ROUTES["/codebase/join/<token>"])
    assert 'codebase.join.invalid' in code
    assert "preview.error" not in code, "the server's message must not reach the page"
    assert "(e as Error).message" not in code
    assert "(err as Error).message" not in code
    assert "retry: false" in code


# ─── the invite token ────────────────────────────────────────────────


def test_the_invite_token_goes_in_a_body_never_in_a_query_string():
    code = _code(API)
    for name in ("previewInvite", "redeemInvite"):
        entry = code[code.index(f"{name}:"):]
        entry = entry[:entry.index("}),") if "}),"in entry else 400]
        assert 'method: "POST"' in entry, name
        assert "json: { token: inviteToken }" in entry, name
        assert "queryString" not in entry, name


@pytest.mark.parametrize("source", _codebase_sources(), ids=lambda p: str(p.relative_to(WEB)))
def test_nothing_in_the_section_puts_a_secret_in_a_url_or_keeps_one(source):
    code = _code(source)
    assert not re.search(r"[?&]token=", code), "a token in a query string"
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        assert storage not in code, f"{storage}: an invite link is shown once and kept nowhere"


def test_the_join_form_cannot_be_autofilled_with_a_credential():
    """The existing rule for secret-bearing forms (a `token` field must POST)
    keys on `name`/`id`; this page has no form field for the token at all."""
    code = _code(ROUTES["/codebase/join/<token>"])
    assert not re.search(r"""(?:name|id)=["']token["']""", code)
    assert "<form" not in code


def test_joining_switches_workspace_the_way_the_switcher_does():
    code = _code(ROUTES["/codebase/join/<token>"])
    assert "x-workspace=${r.workspace_slug}" in code
    assert "forgetAgentSession()" in code
    assert "window.location.assign(" in code
    assert code.index("document.cookie") < code.index("forgetAgentSession()") < code.index(
        "window.location.assign(")
