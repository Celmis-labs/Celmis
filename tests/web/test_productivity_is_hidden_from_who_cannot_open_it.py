"""The Productivity tab and page are for workspace owners and admins only.

The API (`require_workspace_admin`, src/ee/analytics/productivity_router.py)
refuses an editor, a member and a viewer. The web spells the same rule through
`useCanManageWorkspace`, and the tab, the page and its queries all follow it.
An editor is the case that matters: the Analytics page is theirs, this one is
not, so the two must not be drawn from the same hook. Read with comments
stripped, so a comment describing the rule cannot stand in for the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"
VIEW = WEB / "ee" / "analytics" / "productivity-view.tsx"


def _code(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_the_tab_is_for_workspace_admins_and_not_for_the_analytics_audience() -> None:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    entry = re.search(r'\{ href: "/productivity"[^}]*\}', tabs)
    assert entry, "the Productivity tab is gone"
    assert "workspaceAdminOnly: true" in entry.group(0)
    assert "analyticsOnly" not in entry.group(0)
    assert "useCanManageWorkspace() === true" in tabs
    assert "!d.workspaceAdminOnly || canManage" in tabs


def test_the_tab_also_follows_the_licence_but_only_an_explicit_false() -> None:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    assert re.search(r'href: "/productivity"[^}]*requiresFeature: "productivity"', tabs)
    assert "!featureOff(capabilities, d.requiresFeature)" in tabs


def test_the_page_refuses_before_it_asks_and_does_not_borrow_the_analytics_hook() -> None:
    page = _code(VIEW)
    assert "useCanManageWorkspace()" in page
    assert "useCanViewAnalytics" not in page
    assert "const on = !!token && allowed === true" in page
    assert "productivity.forbiddenTitle" in page
