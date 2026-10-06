"""The preview shows an admin the task as a review reads it, from the workspace's own Jira.

Read-only, admin-only, and bound to the connection the workspace saved: the
site and the token come from the credential row, so the request cannot name a
host. It shows the curated fields, the numbered criteria and the exact fenced
text the model receives, bypasses the cache so an edit in Jira shows at once,
and answers Jira's refusals as sentences with a status that does not sign the
admin out.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from src.api.routers import task_context as tc
from tests.review import jira_fakes
from tests.review.jira_fakes import EMAIL, TOKEN, FakeJira, doc, issue, para


@pytest.fixture
def site(monkeypatch, tmp_path):
    fake = FakeJira()
    fake.issues["PROJ-6066"] = issue()
    fake.fields = [
        {"id": "summary", "name": "Summary", "custom": False},
        {"id": "customfield_10042", "name": "Acceptance criteria", "custom": True,
         "schema": {"type": "string"}},
        {"id": "customfield_10001", "name": "Team", "custom": True, "schema": {"type": "option"}},
        {"id": "customfield_bad", "name": "Odd id", "custom": True},
    ]
    jira_fakes.install(monkeypatch, tmp_path, fake)
    return fake


def _preview(key="PROJ-6066", **kw):
    return tc.preview_issue(key, kw.pop("acceptance_field", None), kw.pop("comments", 0),
                            None, "ws-a")


def test_the_preview_has_the_curated_fields_the_criteria_and_the_model_text(site):
    out = _preview()
    assert (out.key, out.summary, out.status, out.issue_type) == (
        "PROJ-6066", "Cut profiles by length", "In Progress", "Story")
    assert out.url == "https://acme.atlassian.net/browse/PROJ-6066"
    assert [(c["id"], c["text"]) for c in out.criteria] == [
        ("AC1", "A profile is cut to the length on the order"),
        ("AC2", "Offcuts shorter than 10 mm are discarded")]
    assert out.llm_text.startswith('<external_untrusted source="jira" key="PROJ-6066">')
    assert "AC2. Offcuts shorter than 10 mm are discarded" in out.llm_text


def test_a_key_typed_in_lower_case_is_read_as_the_task(site):
    assert _preview("proj-6066").key == "PROJ-6066"


def test_the_preview_reads_fresh_so_an_edit_in_jira_shows_at_once(site):
    _preview()
    site.issues["PROJ-6066"] = issue(summary="Edited", updated="2026-10-03T00:00:00.000+0000")
    assert _preview().summary == "Edited"
    assert site.calls.count("/issue/PROJ-6066") == 2


def test_the_preview_marks_a_description_that_was_cut(site, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "jira_max_description_chars", 500, raising=False)
    site.issues["PROJ-6066"] = issue(description=doc(*[para("x" * 100) for _ in range(30)]))
    assert _preview().truncated is True


@pytest.mark.parametrize("key", ["", "not-a-key", "PROJ", "PROJ-", "../../etc", "PROJ-1/comment"])
def test_something_that_is_not_a_key_never_reaches_jira(site, key):
    with pytest.raises(HTTPException) as err:
        _preview(key)
    assert err.value.status_code == 400
    assert site.calls == []


def test_an_acceptance_field_must_look_like_a_custom_field_id(site):
    with pytest.raises(HTTPException) as err:
        _preview(acceptance_field="summary,description")
    assert err.value.status_code == 400 and site.calls == []


def test_the_configured_field_decides_where_the_criteria_come_from(site):
    site.issues["PROJ-6066"] = issue(fields={"customfield_10042": "- from the field"})
    out = _preview(acceptance_field="customfield_10042")
    assert out.criteria_source == "field" and out.criteria[0]["text"] == "from the field"


@pytest.mark.parametrize(("jira_status", "code"), [(404, 404), (403, 403), (401, 502), (500, 502)])
def test_jiras_refusals_become_sentences_and_a_401_is_not_passed_on_as_one(site, jira_status, code):
    site.status_for["PROJ-6066"] = jira_status
    with pytest.raises(HTTPException) as err:
        _preview()
    assert err.value.status_code == code
    assert TOKEN not in err.value.detail and EMAIL not in err.value.detail


def test_without_a_connection_the_answer_says_where_to_make_one(monkeypatch, tmp_path):
    jira_fakes.install(monkeypatch, tmp_path, FakeJira(), connected=False)
    with pytest.raises(HTTPException) as err:
        _preview()
    assert err.value.status_code == 404 and "Connections" in err.value.detail


def test_the_field_list_offers_only_custom_fields_with_a_usable_id(site):
    fields = tc.list_fields(None, "ws-a")
    assert [(f.id, f.name, f.schema_type) for f in fields] == [
        ("customfield_10042", "Acceptance criteria", "string"),
        ("customfield_10001", "Team", "option")]


def test_the_project_list_gives_keys_and_names(site):
    assert [(p.key, p.name) for p in tc.list_projects(None, "ws-a")] == [
        ("PROJ", "Acme 2D"), ("AIR", "AI")]


def test_every_route_needs_a_workspace_admin():
    from src.api.deps import require_workspace_admin

    for fn in (tc.preview_issue, tc.list_fields, tc.list_projects):
        user = inspect.signature(fn).parameters["_user"].default
        assert user.dependency is require_workspace_admin, fn.__name__


def test_the_routes_are_mounted_and_refuse_a_caller_who_is_not_signed_in():
    from fastapi.testclient import TestClient

    from src.api.main import app

    client = TestClient(app, raise_server_exceptions=False)
    for path in ("/api/task-context/issue/PROJ-6066", "/api/task-context/fields",
                 "/api/task-context/projects"):
        assert client.get(path).status_code == 401, path


def test_no_route_takes_a_host_so_none_can_be_pointed_elsewhere():
    for fn in (tc.preview_issue, tc.list_fields, tc.list_projects):
        assert not {"url", "host", "base_url", "site"} & set(inspect.signature(fn).parameters)
