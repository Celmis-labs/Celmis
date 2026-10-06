"""The pages the API restricts by role have no tab for who is refused, and say why.

  * /memories    - owner, admin, editor (`require_memories_access`)
  * /productivity - owner, admin (`require_workspace_admin`)
  * /connections - owner, admin (every save, verify and delete is
                    `require_workspace_admin`; the Jira card lives there)

For each: the tab is filtered by the same hook the page uses, the page waits
for the membership before it asks anything (no flash of a refusal, no request
that is certain to be a 403), draws an explicit "this is for ..." state for a
refused role, and the sentence exists in every language. Read with comments
stripped, so a comment describing the rule cannot stand in for the rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def _tab(href: str) -> str:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    entry = re.search(r'\{ href: "' + re.escape(href) + r'"[^}]*\}', tabs)
    assert entry, f"the {href} tab is gone"
    return entry.group(0)


def test_the_memories_tab_is_for_editors_and_above() -> None:
    assert "editorOnly: true" in _tab("/memories")
    assert "!d.editorOnly || canEditor" in _code(WEB / "components" / "section-tabs.tsx")


def test_the_productivity_tab_is_for_owners_and_admins() -> None:
    entry = _tab("/productivity")
    assert "workspaceAdminOnly: true" in entry
    assert "editorOnly" not in entry and "analyticsOnly" not in entry


def test_the_connections_tab_is_for_owners_and_admins() -> None:
    assert "workspaceAdminOnly: true" in _tab("/connections")


def test_the_memories_page_asks_nothing_until_it_knows_the_role() -> None:
    page = _code(WEB / "app" / "(app)" / "memories" / "page.tsx")
    assert "useCanEditPrompts()" in page
    assert "enabled: !!token && allowed === true" in page
    assert "memories.forbiddenTitle" in page


def test_the_connections_page_asks_nothing_until_it_knows_the_role() -> None:
    page = _code(WEB / "app" / "(app)" / "connections" / "page.tsx")
    assert "useCanManageWorkspace()" in page
    assert "enabled: !!token && canManage === true" in page
    assert "canManage === false" in page
    assert "connections.forbiddenTitle" in page and "connections.forbiddenDesc" in page


def test_the_jira_card_is_only_drawn_inside_the_page_that_refuses_non_admins() -> None:
    drawn_in = [
        p for p in (WEB / "app").rglob("*.tsx") if "<JiraConnectionCard" in _code(p)
    ] + [p for p in (WEB / "components").rglob("*.tsx")
         if p.name != "jira-connection-card.tsx" and "<JiraConnectionCard" in _code(p)]
    assert [p.name for p in drawn_in] == ["page.tsx"]
    assert drawn_in[0].parent.name == "connections"


def test_the_learning_settings_link_to_memories_only_for_those_who_may_open_them() -> None:
    section = _code(WEB / "components" / "review-settings" / "section-learning.tsx")
    assert "useCanEditPrompts() === true" in section
    assert "{canOpenMemories && (" in section


@pytest.mark.parametrize("locale", sorted(p.stem for p in MESSAGES.glob("*.json")))
@pytest.mark.parametrize("key", [
    "memories.forbiddenTitle", "memories.forbiddenDesc",
    "productivity.forbiddenTitle", "productivity.forbiddenDesc",
    "connections.forbiddenTitle", "connections.forbiddenDesc",
])
def test_the_refusal_is_said_in_every_language(locale: str, key: str) -> None:
    messages = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    assert messages.get(key, "").strip(), f"{locale} has no {key}"


def test_the_refusals_do_not_name_another_product() -> None:
    messages = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    for key, text in messages.items():
        if key.endswith((".forbiddenTitle", ".forbiddenDesc")):
            assert "kodus" not in text.lower() and "kody" not in text.lower(), key
