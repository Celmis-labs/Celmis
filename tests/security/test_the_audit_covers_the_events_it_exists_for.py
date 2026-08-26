"""Half a lifecycle in the audit file reads as a lie about the other half.

The trail recorded four things: a successful password login, a git connection
saved, a git connection deleted, a repository registered. Everything below was
missing, and each absence has the same character — the file looks complete,
because the row that would contradict it was never written.

  auth.login_failed        Only successes. A brute-force run leaves NO trace
                           until it succeeds, and then leaves one row that
                           looks like an ordinary Tuesday.
  auth.master_login        The only path to global admin anywhere in this
                           product. It had a log line and no audit row, so the
                           action an investigation starts from was absent from
                           the file an investigation reads.
  auth.master_login_failed Somebody who knows the master email and is guessing
                           at the key — the highest-signal failure available.
  llm_key.saved            A git connection saved was audited; the LLM key
                           that every model call in the workspace is billed to
                           was not.
  repo.unregistered        Registering was audited. Removing was not.
  repo.purged              Destroys the clone, the graph, the vault notes, the
                           Qdrant points and the review history.
  auth.account_deleted     Drops memberships and a whole personal workspace.

Asserted by CALLING the endpoints and reading the records back. A test that
greps for `record_action(` proves a call exists, not that it fires — and the
login 500 that passed 4894 tests was exactly a call that existed and never ran.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def recorded(monkeypatch):
    """Every `record_action` call made during the block."""
    rows: list[dict] = []

    import src.security.audit as audit_mod
    real = audit_mod.record_action

    def _spy(**kw):
        rows.append(kw)
        return real(**kw)

    # Patched where each router LOOKED IT UP, not only at the source module:
    # every one of them does `from src.security.audit import record_action` at
    # import time, so rebinding the source alone rebinds nothing they can see.
    monkeypatch.setattr(audit_mod, "record_action", _spy)
    for mod in ("src.api.routers.auth", "src.api.routers.repos",
                "src.api.routers.llm", "src.api.routers.connections"):
        import importlib
        m = importlib.import_module(mod)
        if hasattr(m, "record_action"):
            monkeypatch.setattr(m, "record_action", _spy)
    return rows


def _actions(rows) -> list[str]:
    return [r.get("action") for r in rows]


def _only(rows, action) -> dict:
    hits = [r for r in rows if r.get("action") == action]
    assert hits, f"no {action} row; got {_actions(rows)}"
    return hits[0]


@pytest.fixture
def account(client) -> dict:
    email = f"audit-{uuid.uuid4().hex[:10]}@audit.example.com"
    password = "S3curePassw0rd!x"
    r = client.post("/api/auth/signup",
                    json={"email": email, "password": password, "name": "Audit"})
    assert r.status_code == 200, r.text
    return {"email": email, "password": password,
            "token": r.json()["access_token"]}


# ─── authentication ──────────────────────────────────────────────────


def test_a_signup_is_recorded(client, recorded):
    email = f"audit-{uuid.uuid4().hex[:10]}@audit.example.com"
    r = client.post("/api/auth/signup",
                    json={"email": email, "password": "S3curePassw0rd!x"})
    assert r.status_code == 200
    row = _only(recorded, "auth.signup")
    assert row["actor"] == email


def test_a_wrong_password_is_recorded(client, account, recorded):
    r = client.post("/api/auth/login",
                    json={"email": account["email"], "password": "not-the-password"})
    assert r.status_code == 401
    row = _only(recorded, "auth.login_failed")
    assert row["actor"] == account["email"]
    assert row["error"] == "wrong-password"


def test_an_unknown_account_is_recorded_and_told_apart(client, recorded):
    """The RESPONSE must not distinguish the two — that is an enumeration
    oracle. The audit file must, because the operator reading it needs the
    difference: many addresses tried once is credential stuffing, one address
    tried many times is a targeted guess."""
    stranger = f"nobody-{uuid.uuid4().hex[:8]}@audit.example.com"
    r = client.post("/api/auth/login",
                    json={"email": stranger, "password": "whatever"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid credentials"
    row = _only(recorded, "auth.login_failed")
    assert row["error"] == "unknown-account"


def test_the_failure_row_never_carries_the_password(client, account, recorded):
    secret = "hunter2-do-not-log-me"
    client.post("/api/auth/login",
                json={"email": account["email"], "password": secret})
    blob = repr(recorded)
    assert secret not in blob
    # Nor its length, which narrows a brute-force search almost as well.
    #
    # Checked structurally, not by substring. `str(len(secret))` was "21" and
    # a UUID contains "21" about half the time — an assertion that fails on a
    # coin flip is worse than none, because the next person deletes it.
    row = _only(recorded, "auth.login_failed")
    assert not [k for k in row if "len" in k.lower()]
    assert len(secret) not in [v for v in row.values() if isinstance(v, int)]
    assert not any(k for k, v in (row.get("detail") or {}).items()
                   if v == len(secret) or "len" in k.lower())


def test_a_successful_login_is_still_recorded(client, account, recorded):
    r = client.post("/api/auth/login",
                    json={"email": account["email"], "password": account["password"]})
    assert r.status_code == 200
    assert _only(recorded, "auth.login")["actor"] == account["email"]


def test_the_master_login_is_recorded(client, recorded, monkeypatch):
    """The only way to obtain global admin, and it re-asserts that admin over
    any demotion. It had `logger.warning("master_admin_login")` and nothing in
    the audit file."""
    key = "a-long-enough-master-key-for-the-gate"
    monkeypatch.setenv("CELMIS_MASTER_KEY", key)
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "master@audit.example.com")
    r = client.post("/api/auth/login",
                    json={"email": "master@audit.example.com", "password": key})
    assert r.status_code == 200, r.text
    row = _only(recorded, "auth.master_login")
    assert row["target"] == "master-key"


def test_a_wrong_master_key_is_recorded(client, recorded, monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_KEY", "a-long-enough-master-key-for-the-gate")
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "master@audit.example.com")
    r = client.post("/api/auth/login",
                    json={"email": "master@audit.example.com", "password": "wrong-guess-here"})
    assert r.status_code == 401
    _only(recorded, "auth.master_login_failed")


def test_the_master_key_never_appears_in_the_row(client, recorded, monkeypatch):
    key = "a-long-enough-master-key-for-the-gate"
    monkeypatch.setenv("CELMIS_MASTER_KEY", key)
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "master@audit.example.com")
    client.post("/api/auth/login",
                json={"email": "master@audit.example.com", "password": key})
    assert key not in repr(recorded)


# ─── the rest of the lifecycle ───────────────────────────────────────


# ─── the LLM keys and the routing ────────────────────────────────────
#
# Called directly rather than over HTTP. `PUT /api/llm/config` resolves a
# workspace, which needs Postgres, and the audit behaviour under test has
# nothing to do with the database — an in-memory credential store is the only
# persistence `put_config` touches. Same technique as
# tests/api/test_local_model_setup.py, which this borrows from.


_LLM_ADMIN = SimpleNamespace(id="u-ops", email="ops@audit.example.com",
                             is_admin=True)


class _FakeCredentialStore:
    def __init__(self):
        self.rows: dict[tuple, SimpleNamespace] = {}

    def save(self, *, provider, secret, metadata=None, user_id="",
             account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=metadata or {})

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))


@pytest.fixture
def llm_store(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY",
                "LITELLM_PROXY_API_BASE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    fake = _FakeCredentialStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


def _fake_request():
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "PUT", "path": "/api/llm/config",
        "headers": [], "client": ("203.0.113.9", 51234), "query_string": b"",
    })


def _put_config(**payload):
    from src.api.routers.llm import LLMConfigIn, put_config

    return put_config(LLMConfigIn(**payload), _fake_request(),
                      user=_LLM_ADMIN, workspace_id="ws-audit")


def test_an_llm_key_being_saved_is_recorded(llm_store, recorded):
    """A git connection saved was audited. The key every model call in the
    workspace is billed to was not.

    Through `PUT /api/llm/config`, which is the MOUNTED route. The obvious
    place for this was `routers/byok.py`, whose `save_key` looked exactly like
    the endpoint for it — and was mounted nowhere. Auditing that one would have
    produced a passing test and an unaudited product. (That file has since been
    deleted; the trap it illustrates has not gone anywhere, which is why the
    story stays here.)
    """
    _put_config(provider="anthropic", api_key="sk-ant-" + "z" * 60)
    row = _only(recorded, "llm_key.saved")
    assert "anthropic" in row["detail"]["providers"]
    assert row["workspace_id"] == "ws-audit"


def test_the_llm_key_itself_never_reaches_the_row(llm_store, recorded):
    secret = "sk-ant-" + "q" * 60
    _put_config(provider="anthropic", api_key=secret)
    # The row must EXIST before its contents mean anything. Asserting only
    # "the secret is absent" passes vacuously when nothing was recorded, which
    # is the failure mode this whole file is about.
    _only(recorded, "llm_key.saved")
    blob = repr(recorded)
    assert secret not in blob
    assert "qqqq" not in blob


def test_repointing_the_routing_is_recorded_even_with_no_key(llm_store, recorded):
    """The change with the larger blast radius. Re-pointing the review surface
    sends every diff in the workspace somewhere new and needs no key saved at
    all, so an audit that only fired on a saved key would miss it."""
    _put_config(provider="openai", model="gpt-5-mini")
    row = _only(recorded, "llm_config.changed")
    assert row["detail"]["provider"] == "openai"
    assert "llm_key.saved" not in _actions(recorded)


def test_an_untouched_config_records_nothing(llm_store, recorded):
    """PATCH semantics: an absent field means untouched, not cleared. Saving
    only the documentation language must not read as a provider change."""
    _put_config(docs_language="de")
    assert "llm_config.changed" not in _actions(recorded)
    assert "llm_key.saved" not in _actions(recorded)


# ─── the repository lifecycle ────────────────────────────────────────


def test_unregistering_a_repository_is_recorded(recorded, monkeypatch):
    """Registering was audited. Removing was not — half a lifecycle, which
    reads as a repository still connected long after somebody disconnected it.

    Called directly: the route resolves a workspace and a permission, neither
    of which has anything to do with what is being asserted.
    """
    from src.api.routers import repos as repos_router

    class _Store:
        def delete_in_workspace(self, ws, slug):
            return True

    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: _Store())

    user = SimpleNamespace(id="u-lead", email="lead@audit.example.com")
    import asyncio
    asyncio.run(repos_router.remove_repo(
        "github_acme-worker", _fake_request(), purge=False,
        user=user, workspace_id="ws-audit", _perm=user))

    row = _only(recorded, "repo.unregistered")
    assert row["target"] == "github_acme-worker"
    assert row["workspace_id"] == "ws-audit"
    assert row["detail"]["purge"] is False


def test_a_purge_is_a_different_action_from_an_unregister():
    """Not merged into one `repo.removed`, and the distinction is the point:
    an unregister drops a row, a purge destroys the clone, the graph, the
    vault notes, the Qdrant points and the review history. One action name
    covering both would hide the irreversible one among the reversible ones.

    Asserted on the source's string constants rather than by driving a purge,
    which needs Postgres.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/api/routers/repos.py"
    literals = {n.value for n in ast.walk(ast.parse(src.read_text()))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "repo.unregistered" in literals
    assert "repo.purged" in literals
