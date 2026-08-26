"""Alerting was scattered across three sections and locked to platform admins.

Two separate problems, found by asking production rather than reading:

  * `POST /api/notifications/channels` answered **403 "Admin scope required"**
    to the account that OWNS the workspace. Every other workspace-scoped
    mutation moved to `require_workspace_admin` long ago; these did not. So the
    owner could not create a channel, and the "send test message" button —
    which already existed — had nothing to test.

  * `delete_channel` and `test_channel` loaded a channel BY ID with no tenant
    check, while `list_channels` filtered by workspace. Same shape as the
    by-id holes closed in projects, chats and qa: an id was enough to delete
    another tenant's channel, or to POST text into their chat room.

The log tail stays platform-only on purpose: its ring buffer holds every
workspace's lines, so it is a platform view sitting inside an otherwise
workspace-scoped section, and the tab is hidden rather than the endpoint
loosened.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from src.api.routers import intel

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"

#: Workspace-scoped: the channel row carries a workspace_id and is only ever
#: listed within one.
WORKSPACE_SCOPED = ("create_channel", "delete_channel", "test_channel",
                    "create_binding", "delete_binding")


def test_the_workspace_owner_can_configure_their_own_alerting():
    for name in WORKSPACE_SCOPED:
        source = inspect.getsource(getattr(intel, name))
        assert "require_workspace_admin" in source, (
            f"{name} still demands a PLATFORM admin — the person who owns the "
            f"workspace gets 403 and cannot set up alerting at all"
        )
        assert "Depends(require_admin)" not in source, name


def test_every_by_id_channel_handler_checks_the_tenant():
    for name in ("delete_channel", "test_channel"):
        source = inspect.getsource(getattr(intel, name))
        assert "Depends(current_workspace_id)" in source, (
            f"{name} has no workspace to compare against"
        )
        assert "row.workspace_id != ws_id" in source, (
            f"{name} loads a channel by id without a tenant check"
        )


def test_the_test_message_refuses_a_foreign_channel_by_404():
    """It POSTs to a webhook URL. An unscoped id would put one tenant's text
    into another tenant's chat room — and 404 rather than 403, so the reply
    does not confirm the id exists somewhere."""
    source = inspect.getsource(intel.test_channel)
    assert 'status_code=404' in source
    assert "row is None or row.workspace_id != ws_id" in source


def test_deleting_a_foreign_channel_is_a_silent_no_op():
    """Matching the missing-row branch directly above it."""
    source = inspect.getsource(intel.delete_channel)
    idx = source.find("row.workspace_id != ws_id")
    assert idx > 0
    assert "return" in source[idx:idx + 80]
    assert "404" not in source[idx:idx + 80], "a 404 confirms the id exists"


# ─── the section ─────────────────────────────────────────────────────

TABS = (WEB / "components" / "section-tabs.tsx").read_text()


def test_alerts_channels_and_logs_live_in_one_section():
    idx = TABS.find("  monitoring: [")
    assert idx > 0, "there is no monitoring section"
    block = TABS[idx:TABS.find("],", idx)]
    for href in ("/alerts", "/admin/notifications", "/admin/logs"):
        assert f'"{href}"' in block, f"{href} is not in the monitoring section"


def test_none_of_the_three_is_left_behind_in_its_old_section():
    """A page listed twice gets two different breadcrumbs depending on how you
    arrived at it."""
    for href in ("/alerts", "/admin/notifications", "/admin/logs"):
        assert TABS.count(f'href: "{href}"') == 1, f"{href} is listed twice"


def test_the_log_tail_is_hidden_from_non_admins():
    idx = TABS.find('href: "/admin/logs"')
    line = TABS[idx:TABS.find("\n", idx)]
    assert "adminOnly: true" in line, (
        "the log buffer holds every workspace's lines — the tab must not be "
        "offered to a member who would only get a 403"
    )


def test_the_tab_row_actually_filters_on_that_flag():
    assert "d.adminOnly || isAdmin" in TABS, "adminOnly is declared but unused"
    assert "useSession" in TABS, "the tab row has no source for isAdmin"


def test_the_three_pages_point_at_the_new_set():
    for rel in ("app/(app)/alerts/page.tsx",
                "app/(app)/admin/notifications/page.tsx",
                "app/(app)/admin/logs/page.tsx"):
        source = (WEB / rel).read_text()
        assert 'SectionTabs set="monitoring"' in source, f"{rel} kept its old set"


def test_the_section_label_exists_in_every_locale():
    """`t()` echoes an unknown key straight back, so a missing translation
    ships the literal string "nav.monitoring" as a sidebar label."""
    messages = WEB / "lib" / "i18n" / "messages"
    files = sorted(p for p in messages.glob("*.json") if not p.name.startswith("._"))
    assert len(files) > 5, "the locale sweep found almost nothing"
    missing = [
        p.name for p in files
        if "nav.monitoring" not in json.loads(p.read_text(encoding="utf-8"))
    ]
    assert not missing, f"nav.monitoring missing from {missing}"


def test_the_sidebar_renders_the_section():
    shell = (WEB / "components" / "app-shell.tsx").read_text()
    assert "SECTION_TABS.monitoring" in shell
    assert re.search(r'labelKey:\s*"nav\.monitoring"', shell)
