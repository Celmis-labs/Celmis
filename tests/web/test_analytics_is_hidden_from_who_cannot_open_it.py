"""The Analytics tab is drawn only for people the API would let in.

`require_analytics_access` (src/api/deps.py) admits a global admin and the
roles in `ANALYTICS_ROLES`; the web hook spells the same set, and the tab is
filtered on it. Two spellings of one rule are pinned to agree here, because
the failure is quiet either way: a tab that 403s, or a lead with no tab.

Analytics is also an enterprise feature now (web/ee/analytics, src/ee). The
tab and the route additionally follow /api/capabilities — and only its
explicit `false`, per that endpoint's contract. Read with comments stripped:
each of these files has a comment describing the rule, and a test that
passed on the comment would pass on a file that no longer does it.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.api.deps import ANALYTICS_ROLES

WEB = Path(__file__).resolve().parents[2] / "web"
VIEW = WEB / "ee" / "analytics" / "analytics-view.tsx"
ROUTE = WEB / "app" / "(app)" / "analytics" / "page.tsx"


def _code(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_the_hook_and_the_api_name_the_same_roles() -> None:
    hook = (WEB / "lib" / "use-analytics-access.ts").read_text(encoding="utf-8")
    m = re.search(r"ANALYTICS_ROLES = new Set\(\[([^\]]*)\]\)", hook)
    assert m, "the hook no longer declares its role set"
    roles = set(re.findall(r'"(\w+)"', m.group(1)))
    assert roles == set(ANALYTICS_ROLES)
    assert "editor" in roles


def test_the_tab_is_filtered_on_the_hook() -> None:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    assert re.search(r'href: "/analytics"[^}]*analyticsOnly: true', tabs)
    assert "useCanViewAnalytics()" in tabs
    assert "!d.analyticsOnly || canAnalytics" in tabs


def test_the_tab_also_follows_the_licence_but_only_an_explicit_false() -> None:
    tabs = _code(WEB / "components" / "section-tabs.tsx")
    assert 'useFeatureOff("review_analytics")' in tabs
    assert re.search(r"canAnalytics = useCanViewAnalytics\(\) === true && !analyticsOff", tabs)
    hook = _code(WEB / "lib" / "use-capabilities.ts")
    # The whole contract in one comparison: absent or unknown is not off.
    assert "?.available === false" in hook


def test_the_page_refuses_before_it_asks() -> None:
    page = _code(VIEW)
    assert "enabled: !!token && allowed === true" in page
    assert "allowed === false" in page


def test_the_route_renders_the_enterprise_view_unless_told_it_is_off() -> None:
    route = _code(ROUTE)
    assert 'from "@/ee/analytics/analytics-view"' in route
    assert 'featureOff(caps.data, "review_analytics")' in route
    assert 't("enterprise.analytics.title")' in route
    assert "<AnalyticsView />" in route


def test_the_issue_status_control_follows_the_api_write_roles() -> None:
    """A viewer saw an enabled status select that always answered 403."""
    from src.api.deps import ISSUE_WRITE_ROLES

    hook = (WEB / "lib" / "use-analytics-access.ts").read_text(encoding="utf-8")
    m = re.search(r"ISSUE_WRITE_ROLES = new Set\(\[([^\]]*)\]\)", hook)
    assert m, "the hook no longer declares the issue write roles"
    assert set(re.findall(r'"(\w+)"', m.group(1))) == set(ISSUE_WRITE_ROLES)
    page = (WEB / "app" / "(app)" / "issues" / "page.tsx").read_text(encoding="utf-8")
    assert "useCanEditIssues()" in page
    assert "disabled={busy || readOnly}" in page


def test_a_failed_membership_fetch_is_a_refusal_not_a_wait() -> None:
    hook = (WEB / "lib" / "use-analytics-access.ts").read_text(encoding="utf-8")
    assert re.search(r"if \(me\.isError\) return false;", hook)
