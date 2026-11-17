"""The web tab's endpoints: upload, run, read again, export; one person's work is theirs."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.main import app
from src.onec import review, store
from tests.onec import acme

WS = "ws-test"


def _user(uid: str):
    return SimpleNamespace(id=uid, email=f"{uid}@example.test", is_admin=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path / "onec")
    dump = acme.write_dump(tmp_path / "dump")
    from src.onec.config import Config, DirSource

    def fake_open(user_id, is_admin, workspace_id, slug):
        if slug == "forbidden":
            raise review.ReviewError(403, "you cannot read this repository")
        return Config(DirSource(dump), "acme"), {"label": slug, "ref": "main@abcdef12", "age": "1 day"}

    monkeypatch.setattr(review, "open_config", fake_open)
    monkeypatch.setattr(review, "list_configs", lambda *a, **k: [
        review.ConfigChoice("acme", "Acme", "main@abcdef12", "1 day", False)])
    state = {"uid": "u-1"}
    app.dependency_overrides[get_current_user] = lambda: _user(state["uid"])
    app.dependency_overrides[current_workspace_id] = lambda: WS
    c = TestClient(app)
    c.state = state  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(current_workspace_id, None)


def _upload(c):
    r = c.post("/api/onec/review/files", files={"file": ("Отчет.erf", acme.report_file())})
    assert r.status_code == 201, r.text
    return r.json()


def test_upload_describes_the_file(client):
    body = _upload(client)
    assert body["kind"] == "Reports" and body["forms"] == 1 and len(body["sha256"]) == 64


def test_upload_refuses_what_is_not_a_1c_file(client):
    r = client.post("/api/onec/review/files", files={"file": ("x.erf", b"hello world" * 10)})
    assert r.status_code == 415


def test_configs_are_listed(client):
    assert client.get("/api/onec/review/configs").json()[0]["slug"] == "acme"


def test_a_review_is_made_read_again_and_exported(client):
    handle = _upload(client)["handle"]
    r = client.post("/api/onec/review", json={"handle": handle, "config": "acme", "lang": "uk"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["level"] == "bad" and body["model"] == {"mode": "none", "used": False,
                                                        "label": "", "failure": ""}
    assert any(b["type"] == "table" for b in body["blocks"])
    assert client.get(f"/api/onec/review/{body['id']}").json()["blocks"] == body["blocks"]
    pdf = client.get(f"/api/onec/review/{body['id']}/export?format=pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    docx = client.get(f"/api/onec/review/{body['id']}/export?format=docx")
    assert zipfile.ZipFile(io.BytesIO(docx.content)).testzip() is None
    md = client.get(f"/api/onec/review/{body['id']}/export?format=md")
    assert "Висновок" in md.text
    assert client.get(f"/api/onec/review/{body['id']}/export?format=exe").status_code == 422


def test_without_a_configuration_it_still_answers(client):
    handle = _upload(client)["handle"]
    r = client.post("/api/onec/review", json={"handle": handle})
    assert r.status_code == 201 and r.json()["config"] == ""


def test_a_model_that_fails_leaves_the_template_report(client):
    handle = _upload(client)["handle"]
    r = client.post("/api/onec/review", json={"handle": handle, "config": "acme",
                                              "model": "nokey:model-x"})
    assert r.status_code == 201
    m = r.json()["model"]
    assert m["mode"] == "model" and m["used"] is False and m["failure"]


def test_someone_elses_upload_and_report_are_not_found(client):
    handle = _upload(client)["handle"]
    rid = client.post("/api/onec/review", json={"handle": handle, "config": "acme"}).json()["id"]
    client.state["uid"] = "u-2"
    assert client.post("/api/onec/review", json={"handle": handle}).status_code == 404
    assert client.get(f"/api/onec/review/{rid}").status_code == 404
    assert client.get(f"/api/onec/review/{rid}/export?format=md").status_code == 404


def test_an_unreadable_repository_is_forbidden(client):
    handle = _upload(client)["handle"]
    assert client.post("/api/onec/review", json={"handle": handle,
                                                 "config": "forbidden"}).status_code == 403


def test_an_unknown_handle_is_not_found(client):
    assert client.post("/api/onec/review", json={"handle": "nope"}).status_code == 404
