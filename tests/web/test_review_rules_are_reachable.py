"""The review rules page exists, is reachable from where people look for it,
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

PAGE = WEB / "app" / "(app)" / "admin" / "review-rules" / "page.tsx"
SETTINGS = WEB / "components" / "review-settings"
TABS = WEB / "components" / "section-tabs.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_the_page_is_a_tab_of_code_review():
    review = re.search(r"review: \[(.*?)\n  \],", _code(TABS), re.S)
    assert review, "the review tab set changed shape"
    assert '"/admin/review-rules"' in review.group(1)
    assert PAGE.is_file()


def test_the_repo_settings_rules_section_links_to_the_repos_rules():
    """The Rules section of a repository's settings opens the library on
    that repository; the legacy folder rules point there too."""
    rules = _code(SETTINGS / "section-rules.tsx")
    assert "/admin/review-rules?repo=" in rules
    assert "reviewRulesApi.list(" in rules, "the section no longer shows the counts"
    assert "/admin/review-rules?repo=" in _code(SETTINGS / "section-advanced.tsx")


def test_the_api_client_names_every_route_the_router_serves():
    from src.api.routers.review_rules import router

    code = _code(API)
    served = {r.path for r in router.routes}
    for path in served:
        stem = path.replace("/api/review-rules", "").split("/{")[0]
        assert f"/api/review-rules{stem}" in code, f"no client call for {path}"


def test_the_page_uses_the_client_for_each_action():
    code = _code(PAGE)
    for call in ("reviewRulesApi.list(", "reviewRulesApi.create(", "reviewRulesApi.update(",
                 "reviewRulesApi.bulkStatus(", "reviewRulesApi.bulkDelete(",
                 "reviewRulesApi.library(", "reviewRulesApi.addFromLibrary(",
                 "reviewRulesApi.generate(", "reviewRulesApi.importFromRepo(",
                 "reviewRulesApi.job("):
        assert call in code, call
    # The filters and the tags the page promises.
    for word in ('"all"', '"active"', '"pending"', '"rejected"'):
        assert word in code


def test_every_label_is_translated_and_uk_is_real():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    keys = set(re.findall(r'"(admin\.reviewRules\.[\w.]+)"', _code(PAGE)))
    # Built from a variable in the page: every member of each family.
    for family, members in {
        "severity": ("info", "warning", "error", "critical"),
        "origin": ("library", "generated", "imported", "agent"),
        "filter": ("all", "active", "pending", "rejected"),
        "status": ("pending", "rejected"),
        "jobKind": ("generate", "import"),
        "bulkDone": ("active", "rejected", "delete"),
    }.items():
        keys |= {f"admin.reviewRules.{family}.{m}" for m in members}
    assert len(keys) > 40
    for key in keys | {"nav.reviewRules", "admin.reviewPolicies.detail.rulesLibraryLink"}:
        assert key in en, key
        if re.search(r"[A-Za-z]{3}", re.sub(r"\{\w+\}", "", en[key])):
            assert uk[key] != en[key], f"{key} is not translated to Ukrainian"
