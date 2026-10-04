"""How-do-I and where-is questions are answered from a guide, with links.

The floating agent is the first place somebody asks "where do I put my GitLab
token". The answer is the one thing in this chat the model writes in full, so
what it is allowed to say and where it is allowed to send people are pinned:

THE GUIDE IS IN THE PROMPT. An answer about scopes written from a model's
general memory of GitLab is plausible and wrong in the details that matter
(`read_api` is not enough). The planner is handed `src.automation.guide`.

THE GUIDE IS THE ALLOW-LIST FOR LINKS. A link the guide does not name is turned
back into its words before the note is stored, so a made-up path never becomes
a button that lands on a 404 — and nothing outside the app is linkable at all.

IT IS A QUESTION. It answers immediately, with no plan card and no scope.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest


def _stub_client(monkeypatch, reply: dict, seen: dict):
    def _generate(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(text=json.dumps(reply))

    import src.llm.client as llm_client
    monkeypatch.setattr(
        llm_client, "build_llm_client",
        lambda *a, **kw: types.SimpleNamespace(generate=_generate))


def test_help_is_a_read_in_the_catalogue():
    from src.automation.chat import CATALOGUE

    assert CATALOGUE["help"]["reads"] is True
    assert CATALOGUE["help"]["arguments"] == {}


def test_the_planner_is_handed_the_guide(monkeypatch):
    from src.automation.chat import interpret
    from src.automation.guide import GUIDE

    seen: dict = {}
    _stub_client(monkeypatch, {"language": "en", "note": "x", "steps": []}, seen)
    interpret("where do I add a GitLab token?", workspace_id="ws", user_id="u")

    system = seen["system_instruction"]
    assert GUIDE in system, "the guide never reaches the model"
    assert "- help:" in seen["prompt"], "the verb is not offered"


@pytest.mark.parametrize("route", [
    "/connections", "/settings/llm", "/review-settings", "/admin/review-rules",
    "/automation",
])
def test_the_guide_names_the_pages_people_ask_about(route):
    from src.automation.guide import GUIDE_ROUTES

    assert route in GUIDE_ROUTES


def test_the_guide_says_what_a_gitlab_token_needs():
    """The detail a general-purpose answer gets wrong."""
    from src.automation.guide import GUIDE

    assert "`api`" in GUIDE and "`read_api` is not enough" in GUIDE


def test_an_unknown_link_keeps_its_words_and_loses_its_target():
    from src.automation.guide import keep_known_links

    text = ("Open [connections](/connections), then [a page](/settings/github), "
            "[evil](https://evil.example/x) and [js](javascript:alert)")
    assert keep_known_links(text) == (
        "Open [connections](/connections), then a page, evil and js")


def test_a_page_under_a_named_route_is_kept():
    """`/projects/<id>` is real; the guide names its parent."""
    from src.automation.guide import keep_known_links

    text = "[project](/projects/42)"
    assert keep_known_links(text) == text
    # A sibling that merely shares the prefix is not a child.
    assert keep_known_links("[x](/projectsX)") == "x"
    # A settings link keeps its query: the scope and section are in it.
    deep = "[prompts](/review-settings?repo=acme-api&section=prompts)"
    assert keep_known_links(deep) == deep
    # Protocol-relative is another site.
    assert keep_known_links("[x](//evil.example)") == "x"


def test_the_stored_note_has_only_known_links(monkeypatch):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub_client(monkeypatch, {
        "language": "en",
        "note": "Paste it on [Git connections](/connections) — "
                "not [here](/settings/tokens).",
        "steps": [{"action": "help", "arguments": {}}],
    }, seen)
    plan = interpret("where do tokens go?", workspace_id="ws", user_id="u")

    assert plan.reads_only, "a how-to question waits for a press"
    assert "[Git connections](/connections)" in plan.note
    assert "/settings/tokens" not in plan.note


def test_the_answer_lists_the_pages_it_pointed_at():
    from src.automation.actions import Actor
    from src.automation.chat import Plan, Step, execute

    plan = Plan(
        note="See [LLM keys](/settings/llm) and [LLM keys](/settings/llm), "
             "then [Repositories](/repositories).",
        steps=[Step(action="help")],
    )
    # No session: an answer written from the guide touches no database.
    outcome = asyncio.run(execute(
        plan, Actor(user_id="u", email="a@b.c", workspace_id="ws",
                    label="chat"),
        session=None))
    assert outcome["steps"][0]["result"] == {"links": [
        {"label": "LLM keys", "href": "/settings/llm"},
        {"label": "Repositories", "href": "/repositories"},
    ]}
    assert outcome["queued"] == [] and outcome["run_id"] is None


def test_help_has_no_scope_to_resolve():
    from src.automation.chat import Plan, Step, resolve_scope

    plan = resolve_scope(Plan(steps=[Step(action="help")]), workspace_id="ws")
    assert plan.steps[0].resolved_repos == []
    assert plan.blocked is None


_REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_every_route_the_guide_links_is_a_page():
    """The allow-list is only worth something if each entry lands on a page.

    A route the guide names but no `page.tsx` serves is whitelisted into a
    button that lands on a 404 — the failure the allow-list exists to stop.
    A page that is renamed or not merged yet fails here, not in front of a
    user.
    """
    from src.automation.guide import GUIDE_ROUTES

    app = _REPO / "web" / "app" / "(app)"
    missing = sorted(r for r in GUIDE_ROUTES
                     if not (app / r.lstrip("/") / "page.tsx").is_file())
    assert not missing, f"guide links routes with no page: {missing}"


def test_every_label_the_guide_quotes_is_on_screen():
    """Somebody told to press "Re-index all" looks for that text and finds
    "Reindex everything". Every quoted label must be an English UI string."""
    import re

    from src.automation.guide import GUIDE

    en = json.loads((_REPO / "web" / "lib" / "i18n" / "messages" / "en.json")
                    .read_text(encoding="utf-8"))
    strings: set[str] = set()

    def _walk(node):
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, str):
            strings.add(node)

    _walk(en)
    quoted = re.findall(r'"([^"\n]+)"', GUIDE)
    assert quoted, "the guide quotes no labels; the check would pass vacuously"
    stale = [q for q in quoted if q not in strings]
    assert not stale, f"guide quotes labels that are not in en.json: {stale}"


@pytest.mark.parametrize("text", [
    # Reference-style: the definition makes `[x][a]` (and a bare `[a]`) a link.
    "[x][a]\n\n[a]: /\\evil.com",
    "see [a]\n\n   [a]: https://evil.example",
    # A space or angle brackets before the destination is still a link.
    "[x]( /\\evil.com)",
    "[x](</\\evil.com>)",
    '[x](/connections "title")',
    # A backslash is a slash to the browser: `/\evil.com` is another site.
    "[x](/\\evil.com)",
    "[x](/\\\\evil.com)",
    # Autolinks.
    "<https://evil.example/x>",
    "<javascript:alert(1)>",
])
def test_no_markdown_link_shape_survives_unless_it_is_a_guide_route(text):
    """Whatever react-markdown would turn into an anchor must either be a
    guide route written exactly or come back as plain text."""
    # A CommonMark parser decides what is a link, not another regex. It comes
    # in through rich; the skip only bites on an install without it.
    MarkdownIt = pytest.importorskip("markdown_it").MarkdownIt

    from src.automation.guide import GUIDE_ROUTES, keep_known_links

    out = keep_known_links(text)
    tokens = MarkdownIt("commonmark").parse(out)
    hrefs = [
        child.attrs.get("href")
        for tok in tokens for child in (tok.children or [])
        if child.type == "link_open"
    ]
    assert all(h in GUIDE_ROUTES for h in hrefs), (out, hrefs)


def test_an_exact_guide_link_is_still_kept_beside_a_stripped_one():
    from src.automation.guide import keep_known_links

    assert keep_known_links("[ok](/connections) [x]( /\\evil.com)") == (
        "[ok](/connections) x")


def test_an_invented_action_name_cannot_carry_a_link(monkeypatch):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub_client(monkeypatch, {
        "language": "en", "note": "",
        "steps": [{"action": "[open](/\\\\evil.com)", "arguments": {}}],
    }, seen)
    plan = interpret("do it", workspace_id="ws", user_id="u")
    assert plan.note == "There is no action called 'open'."


@pytest.mark.parametrize("route", ["/issues", "/pull-requests", "/analytics"])
def test_the_review_pages_are_in_the_guide(route):
    from src.automation.guide import GUIDE_ROUTES

    assert route in GUIDE_ROUTES


def test_every_workspace_section_tab_is_linkable():
    """A page in the section tabs that the guide does not name is a page the
    agent cannot point anybody to — its link is stripped to plain words."""
    import re

    from src.automation.guide import GUIDE_ROUTES

    src = (_REPO / "web" / "components" / "section-tabs.tsx").read_text()
    body = src[src.index("export const SECTION_TABS"):]
    body = body[:body.index("\n}")]
    tabs = re.findall(r'\{ href: "([^"]+)"[^}]*\}', body)
    admin_only = set(re.findall(r'\{ href: "([^"]+)"[^}]*adminOnly: true', body))
    # The platform-admin section is not a workspace user's to be sent to.
    platform = body[body.index("  admin: ["):]
    platform_routes = set(re.findall(r'href: "([^"]+)"', platform))
    missing = sorted(set(tabs) - admin_only - platform_routes - GUIDE_ROUTES)
    assert tabs and not missing, f"section tabs the guide does not name: {missing}"
