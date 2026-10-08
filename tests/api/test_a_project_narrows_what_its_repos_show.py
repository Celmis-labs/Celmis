"""Per-project include/exclude patterns on a project's repository: the API.

The patterns are stored on the project/repo link and handed to Q&A for every
question asked in a chat of that project. (Matching itself is covered in
`tests/qa/test_a_project_file_scope_narrows_every_tier.py`.)
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, world


def _routers():
    from src.api.routers import chats, projects, qa

    return (projects.router, chats.router, qa.router)


@pytest.fixture
async def pw(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        yield w


async def _project(w, **link):
    r = await w.client.post(
        "/api/projects",
        json={"name": "Legacy", "repos": [{"repo_slug": A_REPO}]},
        headers=w.h("su", "ws-a"))
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    if link:
        p = await w.client.patch(f"/api/projects/{pid}/repos/{A_REPO}", json=link,
                                 headers=w.h("su", "ws-a"))
        assert p.status_code == 200, p.text
    return pid


async def test_a_new_link_has_no_scope(pw):
    pid = await _project(pw)
    got = (await pw.client.get(f"/api/projects/{pid}", headers=pw.h("su", "ws-a"))).json()
    [link] = got["repos"]
    assert link["include_globs"] == [] and link["exclude_globs"] == []


async def test_patching_sets_and_clears_each_list_separately(pw):
    pid = await _project(pw, include_globs=["src/**"], exclude_globs=["src/gen/**"])
    h = pw.h("su", "ws-a")
    got = (await pw.client.get(f"/api/projects/{pid}", headers=h)).json()["repos"][0]
    assert got["include_globs"] == ["src/**"] and got["exclude_globs"] == ["src/gen/**"]
    r = await pw.client.patch(f"/api/projects/{pid}/repos/{A_REPO}",
                              json={"exclude_globs": []}, headers=h)
    assert r.json()["include_globs"] == ["src/**"] and r.json()["exclude_globs"] == []
    r = await pw.client.patch(f"/api/projects/{pid}/repos/{A_REPO}",
                              json={"include_globs": [" docs ", "docs"]}, headers=h)
    assert r.json()["include_globs"] == ["docs"], "trimmed and de-duplicated"


async def test_too_many_or_too_long_patterns_are_refused(pw):
    pid = await _project(pw)
    h = pw.h("su", "ws-a")
    for body in ({"include_globs": [f"p{i}" for i in range(51)]},
                 {"exclude_globs": ["x" * 201]}):
        r = await pw.client.patch(f"/api/projects/{pid}/repos/{A_REPO}", json=body, headers=h)
        assert r.status_code in (422, 400), (body, r.text)


async def test_a_repo_that_is_not_in_the_project_is_404(pw):
    pid = await _project(pw)
    r = await pw.client.patch(f"/api/projects/{pid}/repos/github_nope-nothing",
                              json={"include_globs": ["a"]}, headers=pw.h("su", "ws-a"))
    assert r.status_code == 404


async def test_another_workspace_cannot_touch_the_scope(pw):
    pid = await _project(pw, include_globs=["src"])
    r = await pw.client.patch(f"/api/projects/{pid}/repos/{A_REPO}",
                              json={"include_globs": []}, headers=pw.h("admin_b", "ws-b"))
    assert r.status_code == 403, "not a superadmin"
    r = await pw.client.patch(f"/api/projects/{pid}/repos/{A_REPO}",
                              json={"include_globs": []}, headers=pw.h("su", "ws-b"))
    assert r.status_code == 404, "even the superadmin, from another workspace"
    got = (await pw.client.get(f"/api/projects/{pid}", headers=pw.h("su", "ws-a"))).json()
    assert got["repos"][0]["include_globs"] == ["src"]


async def test_a_question_in_a_project_chat_carries_the_scope_to_retrieval(pw, monkeypatch):
    from src.api.routers import qa as qa_router

    pid = await _project(pw, include_globs=["src/**"], exclude_globs=["src/gen/**"])
    seen: dict = {}

    async def fake_generate(**kw):
        seen.update(kw)
        return "answer", {}

    monkeypatch.setattr(qa_router, "_generate_full", fake_generate)
    h = pw.h("su", "ws-a")
    chat = await pw.client.post("/api/chats", json={"project_id": pid}, headers=h)
    assert chat.status_code == 201, chat.text
    r = await pw.client.post(f"/api/qa/chats/{chat.json()['id']}/ask",
                             json={"content": "where is it?", "stream": False}, headers=h)
    assert r.status_code == 200, r.text
    scope = seen["file_scopes"][A_REPO]
    assert scope.include == ("src/**",) and scope.exclude == ("src/gen/**",)
    assert seen["target_repos"] == [A_REPO]


async def test_a_project_without_patterns_passes_an_empty_scope_map(pw, monkeypatch):
    from src.api.routers import qa as qa_router

    pid = await _project(pw)
    seen: dict = {}

    async def fake_generate(**kw):
        seen.update(kw)
        return "answer", {}

    monkeypatch.setattr(qa_router, "_generate_full", fake_generate)
    h = pw.h("su", "ws-a")
    chat = await pw.client.post("/api/chats", json={"project_id": pid}, headers=h)
    await pw.client.post(f"/api/qa/chats/{chat.json()['id']}/ask",
                         json={"content": "q", "stream": False}, headers=h)
    assert seen["file_scopes"] == {}
