"""The Analytics tab is drawn only for people the API would let in.

`require_analytics_access` (src/api/deps.py) admits a global admin and the
roles in `ANALYTICS_ROLES`; the web hook spells the same set, and the tab is
filtered on it. Two spellings of one rule are pinned to agree here, because
the failure is quiet either way: a tab that 403s, or a lead with no tab.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.api.deps import ANALYTICS_ROLES

WEB = Path(__file__).resolve().parents[2] / "web"


def test_the_hook_and_the_api_name_the_same_roles() -> None:
    hook = (WEB / "lib" / "use-analytics-access.ts").read_text(encoding="utf-8")
    m = re.search(r"ANALYTICS_ROLES = new Set\(\[([^\]]*)\]\)", hook)
    assert m, "the hook no longer declares its role set"
    roles = set(re.findall(r'"(\w+)"', m.group(1)))
    assert roles == set(ANALYTICS_ROLES)
    assert "editor" in roles


def test_the_tab_is_filtered_on_the_hook() -> None:
    tabs = (WEB / "components" / "section-tabs.tsx").read_text(encoding="utf-8")
    assert re.search(r'href: "/analytics"[^}]*analyticsOnly: true', tabs)
    assert "useCanViewAnalytics()" in tabs
    assert "!d.analyticsOnly || canAnalytics" in tabs


def test_the_page_refuses_before_it_asks() -> None:
    page = (WEB / "app" / "(app)" / "analytics" / "page.tsx").read_text(encoding="utf-8")
    assert "enabled: !!token && allowed === true" in page
    assert "allowed === false" in page
