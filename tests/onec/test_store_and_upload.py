"""The upload of a 1C file for `inspect_external`: a ticket is the whole
credential, so it is single use, short-lived and bound to its caller; the file
is readable by that caller alone and kept for a day."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers import onec_external
from src.onec import store
from tests.onec import acme

_REAL_ROOT = store._root


@pytest.fixture(autouse=True)
def _scratch_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path / "onec-external")


def test_the_store_lives_in_the_configured_workspace(monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(store, "_root", _REAL_ROOT)
    root = store._root()
    assert root == Path(get_settings().workspace_dir) / "onec-external"
    saved = store.redeem(store.issue_ticket("u-9", "ws-9"), acme.report_file(), "a.erf")
    assert saved.path.is_relative_to(root)
    assert store.get(saved.handle, "u-9", "ws-9") is not None


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(onec_external.router)
    return TestClient(app)


def _post(client: TestClient, ticket: str, data: bytes, name: str = "Отчет.erf"):
    return client.post(f"/api/onec/external/{ticket}", files={"file": (name, data)})


def test_a_ticket_stores_one_file_for_its_owner(client):
    ticket = store.issue_ticket("u-1", "ws-1")
    r = _post(client, ticket, acme.report_file())
    assert r.status_code == 201, r.text
    handle = r.text.split(" as ", 1)[1].split(",", 1)[0]
    assert store.HANDLE_RE.match(handle)
    assert f'inspect_external(file="{handle}"' in r.text
    saved = store.get(handle, "u-1", "ws-1")
    assert saved is not None and saved.read() == acme.report_file()
    assert saved.file_name == "Отчет.erf"
    assert store.get(handle, "u-2", "ws-1") is None
    assert store.get(handle, "u-1", "ws-2") is None


def test_a_ticket_is_spent_by_its_first_upload(client):
    ticket = store.issue_ticket("u-1", "ws-1")
    assert _post(client, ticket, acme.report_file()).status_code == 201
    again = _post(client, ticket, acme.report_file())
    assert again.status_code == 404 and "unknown or expired" in again.text


def test_a_file_that_is_not_1c_keeps_the_ticket(client):
    ticket = store.issue_ticket("u-1", "ws-1")
    r = _post(client, ticket, b"%PDF-1.7 a document")
    assert r.status_code == 415 and "still valid" in r.text
    assert _post(client, ticket, acme.report_file()).status_code == 201


def test_an_oversized_file_is_refused(client, monkeypatch):
    monkeypatch.setattr(store, "MAX_BYTES", 1000)
    ticket = store.issue_ticket("u-1", "ws-1")
    r = _post(client, ticket, acme.report_file() + b"\0" * 2000)
    assert r.status_code == 413


def test_an_unknown_or_malformed_ticket_is_refused(client):
    assert _post(client, "a" * 32, acme.report_file()).status_code == 404
    assert _post(client, "../../etc", acme.report_file()).status_code in (404, 405)


def test_tickets_and_files_expire(monkeypatch):
    ticket = store.issue_ticket("u-1", "ws-1")
    real = time.time
    monkeypatch.setattr(store.time, "time", lambda: real() + store.TICKET_TTL + 5)
    with pytest.raises(store.StoreError):
        store.redeem(ticket, acme.report_file(), "a.erf")

    monkeypatch.setattr(store.time, "time", real)
    saved = store.redeem(store.issue_ticket("u-1", "ws-1"), acme.report_file(), "a.erf")
    monkeypatch.setattr(store.time, "time", lambda: real() + store.HANDLE_TTL + 5)
    assert store.get(saved.handle, "u-1", "ws-1") is None


def test_a_caller_keeps_at_most_the_newest_uploads(monkeypatch):
    monkeypatch.setattr(store, "MAX_PER_USER", 3)
    handles = [store.redeem(store.issue_ticket("u-1", "ws-1"), b"x", f"{i}.erf").handle
               for i in range(5)]
    kept = {s.handle for s in store.mine("u-1", "ws-1")}
    assert kept == set(handles[-3:])


def test_a_handle_that_is_not_one_reads_nothing():
    assert store.get("../../etc/passwd", "u-1", "ws-1") is None
    assert store.get("x" + "0" * 16, "u-1", "ws-1") is None


@pytest.mark.parametrize(("sent", "shown"), [
    ("C:\\Users\\me\\Отчет.erf", "Отчет.erf"),
    ("Отчет.erf".encode().decode("latin-1"), "Отчет.erf"),
    ("../../a b;rm -rf.epf", "a b_rm -rf.epf"),
    ("", "upload"),
])
def test_the_file_name_is_cleaned_for_display(sent, shown):
    assert store.clean_name(sent) == shown
