"""A job queue with no tenant on it can only be all-or-nothing.

The Jobs page was global-admin-only, so the person who owns a workspace could
not tell whether their own repository had finished indexing — "job не видно".
The reason was not a strict gate, it was a missing column: `sync_jobs` had no
workspace, some payloads carried one and some (ownership_rebuild) never did.
Opening the page as it stood would have shown one tenant another tenant's
repository names, which is a worse bug than the one being fixed.

So the column is the fix and the gate is a consequence. These tests pin the
three properties that make it safe:

  * the column is NULLABLE and never defaults to 'default' — a row with no
    tenant must stay a row with no tenant;
  * every read is filtered with `workspace_id = :ws`, which cannot match
    NULL, so untenanted rows are invisible by construction rather than by a
    rule somebody has to remember;
  * a write names a row, so it checks that row's tenant, not just the
    caller's role.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODELS = (ROOT / "src" / "db" / "models.py").read_text(encoding="utf-8")
QUEUE_SRC = (ROOT / "src" / "sync" / "queue.py").read_text(encoding="utf-8")
ROUTER = (ROOT / "src" / "api" / "routers" / "jobs.py").read_text(encoding="utf-8")
PAGES = ROOT / "web" / "app" / "(app)" / "admin"
EN = json.loads((ROOT / "web" / "lib" / "i18n" / "messages" / "en.json").read_text())


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def _page(name: str) -> str:
    return _strip_comments((PAGES / name / "page.tsx").read_text(encoding="utf-8"))


# ─── the column ──────────────────────────────────────────────────────


def test_sync_jobs_carries_a_nullable_workspace_that_never_defaults():
    from src.db.models import SyncJob

    col = SyncJob.__table__.c.workspace_id
    assert col.nullable, "a job with no tenant must be able to say so"
    assert col.server_default is None, (
        "defaulting the tenant hands queue-wide maintenance to whoever owns "
        "the 'default' workspace"
    )
    assert any(
        [c.name for c in idx.columns][:1] == ["workspace_id"]
        for idx in SyncJob.__table__.indexes
    ), "the page filters on workspace_id on every poll and nothing indexes it"


def test_the_migration_backfills_from_the_payload_and_invents_nothing():
    versions = ROOT / "alembic" / "versions"
    src = next(
        (p.read_text(encoding="utf-8") for p in versions.glob("*.py")
         if "sync_jobs" in p.read_text(encoding="utf-8")
         and "add_column" in p.read_text(encoding="utf-8")
         and "workspace_id" in p.read_text(encoding="utf-8")),
        None,
    )
    assert src, "no migration adds the column"
    body = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    upgrade = body.split("def upgrade")[1].split("def downgrade")[0]
    assert "payload->>'workspace_id'" in upgrade, "historical rows are left blank"
    assert "server_default" not in upgrade, (
        "a server_default would attribute untenanted rows to a tenant"
    )
    assert "nullable=True" in upgrade


# ─── the reads ───────────────────────────────────────────────────────


class _Result:
    def fetchone(self):
        return None

    def mappings(self):
        return self

    def all(self):
        return []

    rowcount = 0


class _Conn:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, stmt, params=None):
        self._calls.append((" ".join(str(stmt).split()), params or {}))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self, calls):
        self._calls = calls

    def begin(self):
        return _Conn(self._calls)


@pytest.fixture()
def calls(monkeypatch):
    recorded: list[tuple[str, dict]] = []
    from src.sync import queue

    monkeypatch.setattr(queue, "_engine", lambda: _Engine(recorded))
    return recorded


def test_a_scoped_read_cannot_reach_another_tenant(calls):
    from src.sync import queue

    queue.list_jobs(workspace_id="ws-alice")
    sql, params = calls[-1]
    assert "workspace_id = :ws" in sql, sql
    assert params["ws"] == "ws-alice"


def test_an_unscoped_read_is_the_global_admin_case(calls):
    from src.sync import queue

    queue.list_jobs()
    sql, _ = calls[-1]
    assert "workspace_id" not in sql, "a global admin's list is filtered anyway"


def test_stats_are_scoped_too(calls):
    from src.sync import queue

    queue.stats(workspace_id="ws-alice")
    sql, params = calls[-1]
    assert "workspace_id = :ws" in sql and params["ws"] == "ws-alice", sql


def test_a_blank_workspace_never_becomes_a_filter(calls):
    """`workspace_id=""` must not degrade into "show me everything"."""
    from src.sync import queue

    queue.list_jobs(workspace_id="   ")
    sql, _ = calls[-1]
    assert "workspace_id" not in sql
    assert queue._normalize_workspace("  ") is None
    assert queue._normalize_workspace(None) is None
    assert queue._normalize_workspace(123) is None


# ─── the writes ──────────────────────────────────────────────────────


def test_enqueue_stamps_the_tenant_from_the_payload_when_not_told(calls):
    from src.sync import queue

    queue.enqueue(kind="generate_vault", payload={"workspace_id": "ws-bob"})
    insert = next(p for sql, p in calls if "INSERT INTO sync_jobs" in sql)
    assert insert["ws"] == "ws-bob"


def test_enqueue_leaves_maintenance_untenanted(calls):
    from src.sync import queue

    queue.enqueue(kind="ownership_rebuild", payload={"repo_slug": "a/b"})
    insert = next(p for sql, p in calls if "INSERT INTO sync_jobs" in sql)
    assert insert["ws"] is None, (
        "guessing a tenant for queue-wide work shows it to the wrong one"
    )


def _endpoint(name: str) -> ast.AST:
    tree = ast.parse(ROUTER)
    return next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


@pytest.mark.parametrize("name", ["retry", "cancel", "delete_job"])
def test_a_write_checks_the_row_and_not_only_the_caller(name: str):
    src = ast.dump(_endpoint(name))
    assert "require_workspace_admin" in src, f"{name} takes any member"
    assert "_authorize_write" in src, (
        f"{name} trusts the id it was handed — another tenant's job id is a "
        f"valid id"
    )


@pytest.mark.parametrize("name", ["list_jobs", "stats"])
def test_a_read_is_scoped_rather_than_gated(name: str):
    src = ast.dump(_endpoint(name))
    assert "current_workspace_id" in src, f"{name} does not know whose queue it is"
    assert "require_admin" not in src, (
        f"{name} is still global-admin-only, which is the bug"
    )


def test_manual_enqueue_stays_global():
    """kind + payload are free-form: it can mint a job about any tenant."""
    src = ast.dump(_endpoint("create"))
    assert "require_admin" in src and "require_workspace_admin" not in src


# ─── the pages ───────────────────────────────────────────────────────


def test_the_jobs_page_is_no_longer_walled_off_as_a_whole():
    body = _page("jobs")
    assert "AdminGate" not in body, "the page still refuses the owner outright"
    assert "useCanManageWorkspace" in body, "nothing decides who may retry"
    assert "canManage && (" in body.replace("\n", " "), (
        "the retry/stop/delete controls render for a reader who cannot use them"
    )


def test_the_page_admits_that_it_is_hiding_rows():
    """A queue that silently omits rows is worse than one that refuses."""
    assert "admin.jobs.scopeNotice" in _page("jobs")
    assert EN["admin.jobs.scopeNotice"].strip()


@pytest.mark.parametrize("page", ["health", "oauth-clients"])
def test_infrastructure_pages_stay_global(page: str):
    """Containers, disk and installation-level OAuth clients say nothing
    about a workspace, so a workspace role cannot be the rule for them."""
    assert "AdminGate" in _page(page)


# `audit` used to be in this list. It left when `AuditRecord` learned which
# workspace it belongs to — the same fix as the column above, one turn later —
# so the page is now owner/admin-scoped and its refusal is a different
# sentence. Its own properties are pinned beside the audit change, not here.
@pytest.mark.parametrize(
    "page,key",
    [("gdpr", "admin.gdpr.adminRequiredWhy")],
)
def test_the_pages_that_stay_global_say_why(page: str, key: str):
    body = _page(page)
    assert "session.isAdmin" in body, f"{page} is no longer global-admin"
    assert key in body and EN[key].strip(), (
        f"{page} refuses without giving a reason"
    )
