"""Usage is a workspace's own figures, so a workspace member can reach it.

The page was built, translated into sixteen languages, filled with breakdowns
— and filed under the global-admin section, `adminOnly: true`, wrapped in an
AdminGate. The account that owns a workspace is not necessarily a global
admin, so the person who pays for it could not open the page that says what it
costs. Asked where the section was, the honest answer was "behind a flag you
do not have".

The API never agreed with that. `/api/spend/summary` has always been
workspace-scoped and readable by any member; only the budget cap is an admin
write. This pins the UI to the rule the backend already had.
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
    """Reachable by name, not by knowing the URL."""
    body = _strip_comments(SHELL)
    entry = next((line for line in body.splitlines()
                  if "/admin/usage" in line and "labelKey" in line), None)
    assert entry, "the Usage page is not in the navigation at all"
    assert "nav.usage" in entry, "it is filed under some other section's label"
    assert "adminOnly" not in entry, (
        "the workspace's own spend is hidden from the workspace"
    )


def test_the_admin_section_does_not_open_on_the_same_page():
    """Two entries pointing at one page is a menu that lies about how much is
    in it, and leaves the admin section highlighted while you read Usage."""
    body = _strip_comments(SHELL)
    admin = next(line for line in body.splitlines() if "nav.adminSection" in line)
    assert "/admin/usage" not in admin


def test_the_page_itself_no_longer_gates_everything():
    """The gate moved rather than disappeared: off the page, onto the one
    control that changes something for everybody else."""
    body = _strip_comments(PAGE)
    assert "AdminGate" not in body, "the page is still gated as a whole"
    assert "useCanManageWorkspace" in body, (
        "nothing decides who may set the cap, so it renders for everyone"
    )
    assert "canManage && (" in body.replace("\n", " "), (
        "the budget card is drawn without checking"
    )


def test_the_backend_agrees_that_members_may_read_it():
    """If the endpoint were admin-only this change would produce a nav entry
    leading to a 403 — worse than a hidden page."""
    tree = ast.parse(SPEND)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "summary")
    src = ast.dump(fn)
    assert "get_current_user" in src, "the summary is unauthenticated"
    assert "require_admin" not in src, (
        "the endpoint is admin-only, so the new nav entry leads to a refusal"
    )


def test_writing_the_cap_needs_the_workspace_not_the_globe():
    """Reading what a workspace spent is its members' business. Setting a cap
    is a control over everyone else's work — but it is still THIS workspace's
    business, so it belongs to its owner rather than to a global admin who may
    have nothing to do with it."""
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
