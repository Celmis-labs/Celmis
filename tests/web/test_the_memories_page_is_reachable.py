"""The memories page exists, is reachable from where people look for it,
speaks every language, and asks the API for what the API serves.

Read with comments stripped: a name surviving in prose must not count as the
name surviving in code.
"""

from __future__ import annotations

import json
import re

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    WEB,
    _strip_comments,
)

PAGE = WEB / "app" / "(app)" / "memories" / "page.tsx"
SETTINGS = WEB / "components" / "review-settings"
TABS = WEB / "components" / "section-tabs.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_the_memories_page_is_a_tab_of_code_review():
    review = re.search(r"review: \[(.*?)\n  \],", _code(TABS), re.S)
    assert review, "the review tab set changed shape"
    assert '"/memories"' in review.group(1)
    assert PAGE.is_file()


def test_the_learning_section_links_to_the_memories_of_the_scope_it_edits():
    code = _code(SETTINGS / "section-learning.tsx")
    assert "/memories" in code
    assert "?repo=" in code, "a repository's settings should open that repository's memories"


def test_the_api_client_names_every_route_the_router_serves():
    from src.api.routers.memories import router

    code = _code(API)
    for route in router.routes:
        stem = route.path.replace("/api/memories", "").split("/{")[0]
        assert f"/api/memories{stem}" in code, f"no client call for {route.path}"


def test_the_page_uses_the_client_for_each_action():
    code = _code(PAGE)
    for call in ("memoriesApi.list(", "memoriesApi.create(", "memoriesApi.update(",
                 "memoriesApi.bulkStatus(", "memoriesApi.bulkDelete(", "memoriesApi.preview("):
        assert call in code, call
    for word in ('"all"', '"active"', '"pending"', '"rejected"'):
        assert word in code


def test_every_label_is_translated_and_uk_is_real():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    keys = set(re.findall(r'"(memories\.[\w.]+)"', _code(PAGE)))
    # Built from a variable in the page: every member of each family.
    for family, members in {
        "scope": ("workspace", "repo", "directory"),
        "origin": ("manual", "command", "reply", "agent", "ui"),
        "status": ("active", "pending", "rejected"),
        "filter": ("all", "active", "pending", "rejected"),
        "bulkDone": ("active", "rejected", "delete"),
    }.items():
        keys |= {f"memories.{family}.{m}" for m in members}
    assert len(keys) > 30
    for key in keys | {"nav.memories"}:
        assert key in en, key
        if re.search(r"[A-Za-z]{3}", re.sub(r"\{\w+\}", "", en[key])):
            assert uk[key] != en[key], f"{key} is not translated to Ukrainian"


def test_the_page_keeps_the_server_limit_in_step():
    from src.review.memories import MAX_TEXT

    assert f"MAX_TEXT = {MAX_TEXT};" in _code(PAGE)


def test_the_memories_tab_and_link_are_drawn_only_for_those_the_api_lets_in():
    tabs = _code(TABS)
    assert re.search(r'href: "/memories",[^}]*editorOnly: true', tabs)
    assert "!d.editorOnly || canEditor" in tabs
    assert "useCanEditPrompts() === true" in tabs
    assert "useCanEditPrompts() === true" in _code(SETTINGS / "section-learning.tsx")


def test_the_page_says_so_to_a_member_instead_of_showing_requests_that_fail():
    code = _code(PAGE)
    assert "useCanEditPrompts()" in code
    assert "allowed === false" in code
    assert code.count("allowed === true") >= 3, "no query runs for somebody the API refuses"
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    assert "memories.forbiddenTitle" in en and "memories.forbiddenDesc" in en
