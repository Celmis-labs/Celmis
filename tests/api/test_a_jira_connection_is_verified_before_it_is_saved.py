"""Saving a Jira connection proves it works first, and never repeats the token.

The save takes the site address, the Atlassian account email and an API token
together. Nothing is stored unless Jira itself accepts the token at that
address: an address that fails the URL rules is refused before any request
leaves, a rejected token is saved nowhere, and the answer, the audit row and
the log carry the account's display name at most — never the token. The token of
one workspace is not readable from another, and "reuse the Bitbucket token"
copies it on the server without it ever reaching the browser.
"""

from __future__ import annotations

import base64
import logging
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from src.api.routers import connections as conn
from src.api.schemas import ConnectionUpsert
from src.credentials.store import CredentialStore
from src.sync import jira_instance as ji
from tests.review import jira_fakes
from tests.review.jira_fakes import EMAIL, SITE, TOKEN, FakeJira

USER = SimpleNamespace(id="u1", email="admin@acme.example")
REQUEST = SimpleNamespace()


@pytest.fixture
def world(monkeypatch, tmp_path):
    store = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    fake = FakeJira()
    audit: list[dict] = []

    monkeypatch.setattr(conn, "get_credential_store", lambda: store)
    monkeypatch.setattr("src.credentials.get_credential_store", lambda: store)
    monkeypatch.setattr(conn, "record_action", lambda **kw: audit.append(kw))
    monkeypatch.setattr(conn, "is_workspace_admin", lambda user, workspace_id: True)
    monkeypatch.setattr(conn, "client_ip", lambda request: "203.0.113.9")
    monkeypatch.setattr(ji, "_resolve", lambda host, port: ["104.192.136.1"])
    monkeypatch.setattr(ji, "allowed_hosts", lambda: [])

    def build_client(**kw):
        return httpx.Client(transport=httpx.MockTransport(fake.handler), auth=kw.get("auth"))

    monkeypatch.setattr("src.http.build_client", build_client)
    return SimpleNamespace(store=store, fake=fake, audit=audit)


def _put(world, **body):
    req = ConnectionUpsert(**{"provider": "jira", "token": TOKEN, "email": EMAIL,
                              "base_url": SITE, **body})
    return conn.upsert_connection("jira", REQUEST, req, USER, "ws-a")


def _saved(world, workspace="ws-a", provider="jira"):
    from src.credentials import git_workspace_slot

    return world.store.load(provider=provider, user_id=git_workspace_slot(workspace))


def test_a_token_jira_accepts_is_saved_with_its_site_and_account(world):
    result = _put(world)
    assert result.ok and result.username == "Robot" and result.base_url == SITE

    saved = _saved(world)
    assert saved.secret == TOKEN
    assert saved.metadata["jira_base_url"] == SITE
    assert saved.metadata["atlassian_email"] == EMAIL
    assert saved.metadata["saved_by"] == "u1"


def test_the_request_to_jira_is_basic_auth_with_the_email_and_the_token(world):
    _put(world)
    header = world.fake.auth_headers[0]
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == f"{EMAIL}:{TOKEN}"
    assert world.fake.calls == ["/myself"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_token_jira_rejects_is_not_saved(world, status):
    world.fake.myself_status = status
    result = _put(world)
    assert not result.ok
    assert TOKEN not in (result.error or "") and EMAIL not in (result.error or "")
    assert _saved(world) is None
    assert world.audit == []


@pytest.mark.parametrize("site", [
    "http://acme.atlassian.net", "https://evil.example.com",
    "https://user:pw@acme.atlassian.net", "https://acme.atlassian.net/jira/x",
])
def test_an_address_that_fails_the_url_rules_is_refused_before_any_request(world, site):
    result = _put(world, base_url=site)
    assert not result.ok and result.error
    assert world.fake.calls == []
    assert _saved(world) is None


def test_a_site_that_resolves_to_a_private_address_is_refused_before_any_request(world, monkeypatch):
    monkeypatch.setattr(ji, "_resolve", lambda host, port: ["10.0.0.5"])
    result = _put(world)
    assert not result.ok and "non-public" in result.error
    assert world.fake.calls == []


def test_a_save_without_the_email_or_the_site_is_refused(world):
    assert "email" in _put(world, email=None).error
    assert not _put(world, base_url=None).ok
    assert _saved(world) is None


def test_the_answer_the_log_and_the_audit_row_never_carry_the_token(world, caplog):
    caplog.set_level(logging.DEBUG)
    result = _put(world)
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in caplog.text and EMAIL not in caplog.text
    [row] = world.audit
    assert row["action"] == "connection.saved" and row["target"] == "jira:default"
    assert TOKEN not in str(row)
    assert row["detail"]["jira_url"] == SITE


def test_a_connection_saved_in_one_workspace_is_invisible_to_another(world):
    _put(world)
    assert _saved(world, "ws-a") is not None
    assert _saved(world, "ws-b") is None
    listed = {c.provider: c for c in conn.list_connections(USER, "ws-b")}
    assert listed["jira"].connected is False
    assert {c.provider: c.connected for c in conn.list_connections(USER, "ws-a")}["jira"] is True


def test_the_listing_shows_the_site_and_never_the_token(world):
    _put(world)
    jira = {c.provider: c for c in conn.list_connections(USER, "ws-a")}["jira"]
    assert jira.metadata["jira_base_url"] == SITE
    assert TOKEN not in jira.model_dump_json()


def test_a_member_who_is_not_an_admin_sees_connected_but_no_account_details(world, monkeypatch):
    _put(world)
    monkeypatch.setattr(conn, "is_workspace_admin", lambda user, workspace_id: False)
    jira = {c.provider: c for c in conn.list_connections(USER, "ws-a")}["jira"]
    assert jira.connected is True
    assert not jira.metadata and jira.account_label == "default"
    assert TOKEN not in jira.model_dump_json() and EMAIL not in jira.model_dump_json()


def test_verifying_again_uses_the_stored_site_and_token(world):
    _put(world)
    world.fake.calls.clear()
    again = conn.verify_existing("jira", USER, "ws-a")
    assert again.ok and world.fake.calls == ["/myself"]
    world.fake.myself_status = 401
    assert not conn.verify_existing("jira", USER, "ws-a").ok


def test_verifying_with_nothing_saved_is_a_404(world):
    with pytest.raises(HTTPException) as err:
        conn.verify_existing("jira", USER, "ws-a")
    assert err.value.status_code == 404


def test_disconnecting_removes_the_credential(world):
    _put(world)
    conn.delete_connection("jira", REQUEST, USER, "ws-a")
    assert _saved(world) is None
    assert world.audit[-1]["action"] == "connection.deleted"


@pytest.fixture
def cached_task(monkeypatch, tmp_path):
    """A stored read of a task in ws-a and one in ws-b, on a throwaway table."""
    from sqlalchemy import create_engine

    from src.db.models import TaskContextCache
    from src.review.task_context import cache

    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    TaskContextCache.__table__.create(engine)
    monkeypatch.setattr(cache, "_engine", lambda: engine)
    for workspace in ("ws-a", "ws-b"):
        assert cache.put(workspace, "acme.atlassian.net", "PROJ-1", {"v": 1, "issue": {}})
    return cache


def test_disconnecting_forgets_what_the_old_token_read_in_this_workspace_only(
    world, cached_task,
):
    _put(world)
    conn.delete_connection("jira", REQUEST, USER, "ws-a")
    assert cached_task.get("ws-a", "acme.atlassian.net", "PROJ-1") is None
    assert cached_task.get("ws-b", "acme.atlassian.net", "PROJ-1") is not None


def test_saving_another_token_forgets_what_the_old_one_read(world, cached_task):
    _put(world)
    cached_task.put("ws-a", "acme.atlassian.net", "PROJ-1", {"v": 1, "issue": {}})
    _put(world)
    assert cached_task.get("ws-a", "acme.atlassian.net", "PROJ-1") is None


def test_disconnecting_another_provider_keeps_the_task_cache(world, cached_task):
    conn.delete_connection("github", REQUEST, USER, "ws-a")
    assert cached_task.get("ws-a", "acme.atlassian.net", "PROJ-1") is not None


def test_a_custom_url_is_still_refused_for_the_other_providers(world):
    req = ConnectionUpsert(provider="github", token="ghp_12345", base_url=SITE)
    with pytest.raises(HTTPException) as err:
        conn.upsert_connection("github", REQUEST, req, USER, "ws-a")
    assert err.value.status_code == 400


def _save_bitbucket(world, email=EMAIL):
    from src.credentials import git_workspace_slot

    world.store.save(provider="bitbucket", secret=TOKEN,
                     metadata={"atlassian_email": email, "bitbucket_workspace": "ws"},
                     user_id=git_workspace_slot("ws-a"), account_label="default")


def test_reusing_the_bitbucket_token_copies_it_on_the_server(world):
    _save_bitbucket(world)
    result = _put(world, token="", email=None, reuse_bitbucket=True)
    assert result.ok
    saved = _saved(world)
    assert saved.secret == TOKEN and saved.metadata["atlassian_email"] == EMAIL
    assert world.audit[-1]["detail"]["reused_bitbucket"] is True
    assert TOKEN not in result.model_dump_json()


def test_reusing_with_no_bitbucket_connection_says_there_is_nothing_to_reuse(world):
    result = _put(world, token="", email=None, reuse_bitbucket=True)
    assert not result.ok and "nothing to reuse" in result.error
    assert world.fake.calls == []


def test_reusing_cannot_reach_another_workspaces_bitbucket_token(world):
    from src.credentials import git_workspace_slot

    world.store.save(provider="bitbucket", secret=TOKEN,
                     metadata={"atlassian_email": EMAIL}, user_id=git_workspace_slot("ws-b"),
                     account_label="default")
    result = _put(world, token="", email=None, reuse_bitbucket=True)
    assert not result.ok and world.fake.calls == []


def test_reuse_is_a_jira_option_only(world):
    req = ConnectionUpsert(provider="github", token="ghp_12345", reuse_bitbucket=True)
    with pytest.raises(HTTPException) as err:
        conn.upsert_connection("github", REQUEST, req, USER, "ws-a")
    assert err.value.status_code == 400


def test_a_short_token_is_still_refused_when_not_reusing():
    with pytest.raises(ValueError):
        ConnectionUpsert(provider="jira", token="abc", email=EMAIL, base_url=SITE)


def test_a_422_does_not_hand_the_token_back():
    from fastapi.testclient import TestClient

    from src.api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    r = client.put("/api/connections/jira", json={"token": TOKEN})   # no provider
    assert TOKEN not in r.text


def test_the_helper_module_is_what_the_service_reads(world):
    _put(world)
    from src.review.task_context import service

    loaded = service.load_connection("ws-a")
    assert loaded is not None and loaded.instance.host == "acme.atlassian.net"
    assert service.load_connection("ws-b") is None
    assert TOKEN not in repr(loaded) and EMAIL not in repr(loaded)
    assert loaded.instance.base_url == jira_fakes.SITE
