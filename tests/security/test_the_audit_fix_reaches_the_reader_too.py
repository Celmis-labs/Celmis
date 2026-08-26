"""Four places the actor fix stopped short of.

The previous commit added `actor`, `actor_id`, `ip` and `target` to
`AuditRecord` and wired four call sites. Re-testing on production found the
WRITER worked and three consumers did not:

  * the CSV export — the artefact an auditor is handed — has a hardcoded
    column list and `extrasaction="ignore"`, so every new field was dropped
    without a word;
  * `auth.login` rows carry no workspace, and `_Scope` refuses an untenanted
    record to every non-global-admin. Four real login rows existed and ZERO
    were readable by the account that owns the installation;
  * `require_ops_access` still logged `request.client.host`, so every
    privileged ops-token access read `ip=172.18.0.7` — the Docker bridge;
  * `_requester` called `get_user_store().get()`, which does not exist. The
    bare `except` swallowed the AttributeError and returned "", so no agent
    commit ever carried the attribution trailer. Verified on a live push.

The last one is the instructive one: the unit test passed because it called
`_commit_message` with an explicit string and never exercised `_requester`.
Same shape as the login 500 — a test covering the path beside the one that
matters.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields as dataclass_fields

import pytest

from src.api.routers.audit import _Scope
from src.security.audit import AuditRecord

# ─── the export ──────────────────────────────────────────────────────


def test_the_csv_export_carries_who_did_it():
    from src.api.routers import audit

    src = inspect.getsource(audit.export_audit)
    assert "dataclass_fields(AuditRecord)" in src, (
        "columns are retyped by hand again — the next field added to "
        "AuditRecord will go missing the same way"
    )


def test_the_derived_columns_include_every_actor_field():
    excluded = {"files_sent", "redaction", "extra", "question_hash",
                "response_hash"}
    cols = [f.name for f in dataclass_fields(AuditRecord) if f.name not in excluded]

    for name in ("actor", "actor_id", "ip", "target"):
        assert name in cols, f"the export would drop {name}"


def test_the_derived_columns_exclude_the_unserialisable_ones():
    """A CSV cell holding a JSON blob is worse than no cell."""
    src = inspect.getsource(inspect.getmodule(_Scope))
    assert "files_sent" in src and "redaction" in src


# ─── the reader ──────────────────────────────────────────────────────


def test_you_can_read_your_own_actions_without_a_workspace():
    """A login is recorded before any workspace is chosen, so those rows are
    untenanted by construction."""
    scope = _Scope(global_=False, workspace_id="ws-1", actor_id="u-1")

    assert scope.allows({"actor_id": "u-1", "workspace_id": None}) is True


def test_you_cannot_read_somebody_elses_untenanted_action():
    """The widening is exactly one person's own history and nothing else."""
    scope = _Scope(global_=False, workspace_id="ws-1", actor_id="u-1")

    assert scope.allows({"actor_id": "u-2", "workspace_id": None}) is False


def test_a_hidden_record_is_still_counted():
    """The page says "N records are not shown" rather than presenting a short
    total as the whole truth."""
    scope = _Scope(global_=False, workspace_id="ws-1", actor_id="u-1")
    scope.allows({"actor_id": "u-2", "workspace_id": None})

    assert scope.hidden_unattributed == 1


def test_another_tenants_records_are_still_refused():
    scope = _Scope(global_=False, workspace_id="ws-1", actor_id="u-1")

    assert scope.allows({"actor_id": None, "workspace_id": "ws-2"}) is False


def test_the_scope_is_built_with_the_callers_id():
    from src.api.routers import audit

    src = inspect.getsource(audit._scope)
    assert "actor_id=user.id" in src


# ─── the ops access line ─────────────────────────────────────────────


def test_privileged_access_logs_the_client_not_the_proxy():
    from src.api import deps

    tree = ast.parse(inspect.getsource(deps))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "require_ops_access":
            body = ast.unparse(node)
            assert "client_ip(request)" in body
            assert "request.client.host" not in body
            return
    raise AssertionError("require_ops_access not found")


# ─── the trailer that never appeared ─────────────────────────────────


def test_the_requester_uses_a_method_that_exists():
    """`get()` does not exist on the user store. `get_by_id` does."""
    from src.users import get_user_store

    store = get_user_store()
    assert hasattr(store, "get_by_id")
    assert not hasattr(store, "get"), (
        "if a `get` appears, re-check `_requester` — the original bug was "
        "calling one that did not exist"
    )


def test_the_requester_resolves_a_real_user():
    """The test that was missing. The previous one passed an explicit string
    into `_commit_message` and never ran this function at all."""
    from src.agent.runner import _requester
    from src.users import get_user_store

    users = get_user_store().list()
    if not users:
        pytest.skip("no users in the local store")
    user = users[0]

    got = _requester(type("Row", (), {"user_id": user.id})())

    assert user.email in got
    assert got.endswith(f"<{user.email}>")


def test_an_unknown_user_yields_nothing():
    """A wrong name is worse than none."""
    from src.agent.runner import _requester

    assert _requester(type("Row", (), {"user_id": "no-such-user"})()) == ""


def test_a_missing_user_id_yields_nothing():
    from src.agent.runner import _requester

    assert _requester(type("Row", (), {"user_id": ""})()) == ""


def test_the_requester_does_not_swallow_a_programming_error():
    """The bare `except Exception` is what hid the broken call for a whole
    round of testing. An AttributeError must now surface."""
    src = inspect.getsource(inspect.getmodule(
        __import__("src.agent.runner", fromlist=["x"])))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_requester":
            body = ast.unparse(node)
            assert "except Exception" not in body
            return
    raise AssertionError("_requester not found")
