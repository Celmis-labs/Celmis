"""The workspace review defaults page exists, is reachable, and says what the
API accepts; the repo policy page shows participation where it is asked for.

Read with comments stripped: a name surviving in prose must not count as the
name surviving in code.
"""

from __future__ import annotations

import json
import re

from src.api.schemas import WorkspaceReviewDefaultsIn
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    POLICY,
    WEB,
    _strip_comments,
)

PAGE = WEB / "app" / "(app)" / "admin" / "review-defaults" / "page.tsx"
AGENTS_PAGE = WEB / "app" / "(app)" / "admin" / "agents" / "page.tsx"
TABS = WEB / "components" / "section-tabs.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_the_page_is_a_tab_of_code_review():
    review = re.search(r"review: \[(.*?)\n  \],", _code(TABS), re.S)
    assert review, "the review tab set changed shape"
    assert '"/admin/review-defaults"' in review.group(1)
    assert PAGE.is_file()


def test_the_page_sends_every_field_the_api_takes():
    code = _code(PAGE)
    for field in WorkspaceReviewDefaultsIn.model_fields:
        assert re.search(rf"\b{field}\b", code), f"the page never sends {field}"
    assert "reviewDefaultsApi.save(" in code
    assert "reviewDefaultsApi.get(" in code


def test_the_api_client_names_the_route():
    assert '"/api/review-defaults"' in _code(API)


def test_the_policy_page_links_to_the_workspace_layer():
    code = _code(POLICY)
    assert 'href="/admin/review-defaults' in code
    assert "inherited_sources" in code, "badges no longer say where a value comes from"
    # Participation lives on the "agents" tab, with a way to its models.
    agents_tab = code[code.index('activeTab === "agents"'):]
    assert "TOGGLEABLE_AGENTS.map" in agents_tab
    assert 'setActiveTab("models")' in agents_tab


def test_the_ai_agents_page_links_to_participation_and_models():
    assert 'href="/admin/review-defaults?tab=agents"' in _code(AGENTS_PAGE)


def test_every_new_label_is_translated_and_uk_is_real():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    keys = set(re.findall(r'"(admin\.reviewDefaults\.[\w.]+)"', _code(PAGE)))
    keys |= {f"admin.reviewDefaults.tab.{t}" for t in ("agents", "comments", "ignore")}
    assert keys, "the page names no keys of its own"
    for key in keys | {"nav.reviewDefaults"}:
        assert key in en, key
        assert uk[key] != en[key], f"{key} is not translated to Ukrainian"
