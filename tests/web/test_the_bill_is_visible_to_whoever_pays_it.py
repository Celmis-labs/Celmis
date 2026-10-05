"""Spend and budget belong to whoever pays for the workspace: owner and admin.

History: the page was once filed under the global-admin section, so the
account that owns a workspace could not read its own bill. That was fixed by
opening the page to every member. The product decision since then is the
middle road: the workspace's money is visible to the workspace's owner and
admins (and global admins), and to nobody below. The API, the agent
(`get_spend`/`get_budget`), MCP and these pages all say the same thing, and
the UI shows an "only owners and admins" state instead of three 403s.
`/api/usage/summary` (the member's own activity) is deliberately untouched.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "web" / "components" / "app-shell.tsx").read_text(encoding="utf-8")
PAGE = (ROOT / "web" / "app" / "(app)" / "admin" / "usage" / "page.tsx").read_text(
    encoding="utf-8")
SPEND = (ROOT / "src" / "api" / "routers" / "spend.py").read_text(encoding="utf-8")
MESSAGES = ROOT / "web" / "lib" / "i18n" / "messages"


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_usage_has_its_own_entry_in_the_navigation():
    """Reachable by name, for the people allowed to read it."""
    body = _strip_comments(SHELL)
    entry = next((line for line in body.splitlines()
                  if "/admin/usage" in line and "labelKey" in line), None)
    assert entry, "the Usage page is not in the navigation at all"
    assert "nav.usage" in entry, "it is filed under some other section's label"
    assert "adminOnly" not in entry.replace("workspaceAdminOnly", ""), (
        "it is hidden behind the GLOBAL admin flag; a workspace owner pays the bill"
    )
    assert "workspaceAdminOnly: true" in entry, (
        "members below admin are offered a page that only refuses them"
    )
    assert "useCanManageWorkspace" in body


def test_the_admin_section_does_not_open_on_the_same_page():
    """Two entries pointing at one page is a menu that lies about how much is
    in it, and leaves the admin section highlighted while you read Usage."""
    body = _strip_comments(SHELL)
    admin = next(line for line in body.splitlines() if "nav.adminSection" in line)
    assert "/admin/usage" not in admin


def test_the_page_gates_its_queries_and_says_why():
    """Off for lower roles: no requests that end in 403, and a plain sentence."""
    body = _strip_comments(PAGE)
    assert "AdminGate" not in body, "the page is gated as a whole by the global flag"
    assert "useCanManageWorkspace" in body
    assert body.count("enabled: !!token && canReadSpend") >= 3, (
        "summary, daily and budget must wait for the role"
    )
    assert "common.spendAdminOnly" in body, "no 'only admins/owners' state"


def test_the_settings_spend_card_follows_the_same_rule():
    settings = _strip_comments(
        (ROOT / "web" / "app" / "(app)" / "settings" / "page.tsx").read_text(encoding="utf-8"))
    assert "useCanManageWorkspace" in settings
    assert settings.count("enabled: !!token && canReadSpend") >= 2
    assert "common.spendAdminOnly" in settings


def test_every_spend_route_needs_a_workspace_admin():
    """If the endpoints were looser than the page, the page would only be
    politeness; if stricter, the nav would lead to a refusal."""
    tree = ast.parse(SPEND)
    for name in ("summary", "daily", "get_budget", "put_budget"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
        assert "require_workspace_admin" in ast.dump(fn), f"{name} is open below admin"


def test_writing_the_cap_needs_the_workspace_not_the_globe():
    """Setting a cap is THIS workspace's business, so it belongs to its owner
    rather than to a global admin who may have nothing to do with it."""
    tree = ast.parse(SPEND)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "put_budget")
    src = ast.dump(fn)
    assert "require_workspace_admin" in src, "anyone can cap the workspace"


def test_the_label_exists_in_every_locale():
    """A nav entry rendering `nav.usage` as a raw key is worse than no entry."""
    for path in sorted(MESSAGES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("nav.usage"), f"{path.stem} has no nav.usage"
        assert data.get("common.spendAdminOnly"), f"{path.stem} has no spendAdminOnly"


# ─── who may CHANGE things, as opposed to read them ──────────────────


def test_workspace_settings_are_governed_by_the_workspace():
    """"Owner може змінювати моделі, параметри etc" — and the rule has to be
    the same everywhere, or it is not a rule.

    The LLM settings page already read it correctly: global admin OR owner/
    admin of the ACTIVE workspace. The budget cap and the embeddings reindex
    did not, so the person who chooses the models could not cap what they cost
    and any member of any workspace could start hours of embedding spend.
    """
    import ast

    for name in ("put_budget",):
        tree = ast.parse(SPEND)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
        src = ast.dump(fn)
        assert "require_workspace_admin" in src, (
            f"{name} is gated on a GLOBAL admin, so a workspace owner cannot "
            f"govern their own workspace"
        )
        assert "require_admin'" not in src


def test_a_reindex_is_not_something_any_member_can_start():
    """Hours of embedding spend, and search is degraded while it runs."""
    import ast

    llm = (ROOT / "src" / "api" / "routers" / "llm.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(llm))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "reindex_embeddings")
    assert "require_workspace_admin" in ast.dump(fn)


def test_the_rule_is_written_once():
    """It had been three inline lines on one page and absent from the others,
    which is exactly how the budget cap drifted onto a different rule."""
    hook = ROOT / "web" / "lib" / "use-workspace-role.ts"
    assert hook.exists(), "no shared answer to 'may this person change things'"
    body = hook.read_text(encoding="utf-8")
    assert "owner" in body and "admin" in body
    assert "isAdmin" in body, "a global admin can reach every workspace"
