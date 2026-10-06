"""/api/memories — the team's memories, through the real routers, the real
role checks and the real workspace resolution (tests/api/rbac_world.py).

  * editor, admin and owner read and write; viewers and members get 403 on
    everything (the role and repository matrix is in
    tests/api/test_memories_role_and_repo_matrix.py);
  * a repository's memories need the repository to be this workspace's and
    the caller's teams to grant `review` on it;
  * an id of another workspace answers 404 and is never touched;
  * approve / reject / delete in bulk act on this workspace's memories only;
  * every write is audited, and the audit never carries the text;
  * the preview says what a review would be told, and what the budget cut;
  * the prompt preview of a repository policy shows the memories a review of
    it gets.
"""

from __future__ import annotations

import json

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_SECRET, world

URL = "/api/memories"


def _routers() -> tuple:
    from src.api.routers import memories

    return (memories.router,)


def _world(tmp_path, monkeypatch):
    return world(tmp_path, monkeypatch, extra_routers=_routers())


async def _create(w, who: str, **body):
    payload = {"text": "Money is stored as integer cents.", **body}
    return await w.client.post(URL, json=payload, headers=w.h(who, "ws-a"))


async def _list(w, who: str = "editor_a", ws: str = "ws-a", **params) -> dict:
    r = await w.client.get(URL, params=params, headers=w.h(who, ws))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403), ("editor_a", 201), ("admin_a", 201),
    ("owner_a", 201), ("gadmin", 201),
])
async def test_editors_write_viewers_and_members_are_refused(tmp_path, monkeypatch, who, status):
    async with _world(tmp_path, monkeypatch) as w:
        r = await _create(w, who)
        assert r.status_code == status, r.text
        body = await _list(w, "owner_a")
        assert body["counts"]["all"] == (1 if status == 201 else 0)
        if status == 201:
            memory = r.json()
            assert (memory["scope"], memory["status"], memory["origin"]) == (
                "workspace", "active", "ui")
            assert w.audit[-1]["action"] == "memories.created"
            assert w.audit[-1]["workspace_id"] == w.ws["ws-a"]
            assert "integer cents" not in json.dumps(w.audit[-1]), "the text is not audited"


async def test_a_repo_memory_needs_the_repo_and_review_on_it(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        r = await _create(w, "editor_a", repo_slug=A_REPO_FULL, path_glob="src/billing")
        assert r.status_code == 201, r.text
        assert r.json()["repo_slug"] == A_REPO
        assert r.json()["scope"] == "directory"
        assert r.json()["path_glob"] == "src/billing", "a plain path is kept as typed; the reader treats it as file or directory"
        r = await _create(w, "admin_a", repo_slug="github_nobody-here", text="x")
        assert r.status_code == 404
        r = await _create(w, "admin_b", repo_slug=A_REPO, text="z")
        assert r.status_code in (403, 404), r.text
        body = await _list(w, repo=A_REPO)
        assert [m["text"] for m in body["memories"]] == ["Money is stored as integer cents."]


async def test_the_store_refuses_what_it_cannot_keep(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        assert (await _create(w, "editor_a", text="   ")).status_code == 422
        assert (await _create(w, "editor_a", text="x" * 801)).status_code == 422
        r = await _create(w, "editor_a", path_glob="src/**")
        assert r.status_code == 422, "a directory needs a repository"
        assert (await _list(w))["counts"]["all"] == 0


async def test_the_same_memory_twice_is_refused_in_the_same_scope(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        assert (await _create(w, "editor_a")).status_code == 201
        again = await _create(w, "editor_a", text="  money is stored as INTEGER cents.")
        assert again.status_code == 409, again.text
        assert (await _list(w))["counts"]["all"] == 1


async def test_the_list_filters_by_status_scope_and_text(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _create(w, "editor_a", text="Workspace wide fact.")
        await _create(w, "editor_a", text="Repo fact about billing.", repo_slug=A_REPO)
        await _create(w, "editor_a", text="Waiting for a decision.", repo_slug=A_REPO,
                      status="pending")
        mine = await _list(w, repo=A_REPO)
        assert mine["counts"] == {"all": 2, "active": 1, "pending": 1, "rejected": 0}
        pending = await _list(w, repo=A_REPO, status="pending")
        assert [m["text"] for m in pending["memories"]] == ["Waiting for a decision."]
        only_ws = await _list(w, scope="workspace")
        assert [m["text"] for m in only_ws["memories"]] == ["Workspace wide fact."]
        found = await _list(w, repo=A_REPO, q="billing")
        assert [m["text"] for m in found["memories"]] == ["Repo fact about billing."]


async def test_an_editor_edits_approves_and_deletes(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        mid = (await _create(w, "editor_a", status="pending")).json()["id"]
        h = w.h("editor_a", "ws-a")
        r = await w.client.patch(f"{URL}/{mid}", json={"status": "active", "text": "Edited fact."},
                                 headers=h)
        assert r.status_code == 200, r.text
        assert (r.json()["status"], r.json()["text"]) == ("active", "Edited fact.")
        assert w.audit[-1]["action"] == "memories.updated"
        assert (await w.client.patch(f"{URL}/{mid}", json={}, headers=h)).status_code == 422
        assert (await w.client.delete(f"{URL}/{mid}", headers=h)).status_code == 200
        assert (await _list(w))["counts"]["all"] == 0
        assert (await w.client.delete(f"{URL}/{mid}", headers=h)).status_code == 404


async def test_another_workspaces_memory_is_a_404_and_stays_untouched(tmp_path, monkeypatch):
    from src.db.models import ReviewMemory

    async with _world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            row = ReviewMemory(workspace_id=w.ws["ws-b"], text=f"{B_SECRET} fact", status="active")
            s.add(row)
            await s.commit()
            foreign = row.id
        h = w.h("admin_a", "ws-a")
        assert (await w.client.patch(f"{URL}/{foreign}", json={"text": "mine now"},
                                     headers=h)).status_code == 404
        assert (await w.client.delete(f"{URL}/{foreign}", headers=h)).status_code == 404
        bulk = await w.client.post(f"{URL}/bulk-status", json={"ids": [foreign], "status": "rejected"},
                                   headers=h)
        assert bulk.status_code == 404
        assert B_SECRET not in json.dumps(await _list(w, "admin_a"))
        async with w.factory() as s:
            kept = await s.get(ReviewMemory, foreign)
            assert (kept.status, kept.text) == ("active", f"{B_SECRET} fact")


async def test_a_bulk_call_with_a_foreign_id_among_ours_only_touches_ours(tmp_path, monkeypatch):
    from src.db.models import ReviewMemory

    async with _world(tmp_path, monkeypatch) as w:
        ours = (await _create(w, "editor_a", status="pending")).json()["id"]
        async with w.factory() as s:
            row = ReviewMemory(workspace_id=w.ws["ws-b"], text="theirs", status="pending")
            s.add(row)
            await s.commit()
            theirs = row.id
        h = w.h("editor_a", "ws-a")
        r = await w.client.post(f"{URL}/bulk-status", json={"ids": [ours, theirs], "status": "active"},
                                headers=h)
        assert r.status_code == 200 and r.json()["updated"] == [ours]
        async with w.factory() as s:
            assert (await s.get(ReviewMemory, theirs)).status == "pending"
        r = await w.client.post(f"{URL}/bulk-delete", json={"ids": [ours, theirs]}, headers=h)
        assert r.json()["deleted"] == [ours]
        async with w.factory() as s:
            assert await s.get(ReviewMemory, theirs) is not None


async def test_approving_into_a_full_scope_is_a_409_not_a_server_error(tmp_path, monkeypatch):
    from src.db.models import ReviewMemory
    from src.review import memories as store

    async with _world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            s.add_all([ReviewMemory(workspace_id=w.ws["ws-a"], text=f"Filler fact {i}",
                                    status="active")
                       for i in range(store.MAX_MEMORIES_PER_SCOPE)])
            await s.commit()
        waiting = (await _create(w, "editor_a", status="pending")).json()["id"]
        h = w.h("editor_a", "ws-a")
        r = await w.client.patch(f"{URL}/{waiting}", json={"status": "active"}, headers=h)
        assert r.status_code == 409, r.text
        r = await w.client.post(f"{URL}/bulk-status", json={"ids": [waiting], "status": "active"},
                                headers=h)
        assert r.status_code == 409, r.text
        async with w.factory() as s:
            assert (await s.get(ReviewMemory, waiting)).status == "pending"


async def test_a_member_cannot_run_a_bulk_write(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        mid = (await _create(w, "editor_a")).json()["id"]
        h = w.h("member_a", "ws-a")
        for path, body in (("bulk-status", {"ids": [mid], "status": "rejected"}),
                           ("bulk-delete", {"ids": [mid]})):
            assert (await w.client.post(f"{URL}/{path}", json=body, headers=h)).status_code == 403
        assert (await _list(w))["counts"]["active"] == 1


async def test_the_preview_says_what_a_review_would_be_told(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _create(w, "editor_a", text="Workspace fact.")
        await _create(w, "editor_a", text="Billing fact.", repo_slug=A_REPO, path_glob="src/billing")
        await _create(w, "editor_a", text="Not yet approved.", repo_slug=A_REPO, status="pending")
        h = w.h("editor_a", "ws-a")
        r = await w.client.get(f"{URL}/preview", params={"repo": A_REPO}, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True and body["omitted"] == 0
        assert {m["text"] for m in body["used"]} == {"Workspace fact.", "Billing fact."}
        assert "Not yet approved." not in body["text"]
        away = await w.client.get(f"{URL}/preview", params={"repo": A_REPO, "paths": ["web/a.tsx"]},
                                  headers=h)
        assert {m["text"] for m in away.json()["used"]} == {"Workspace fact."}
        assert 0 < away.json()["chars"] <= away.json()["budget"]


async def test_the_prompt_preview_shows_the_memories_a_review_gets(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _create(w, "editor_a", text="Money is stored as integer cents.")
        await _create(w, "editor_a", text="Held back.", status="pending")
        r = await w.client.get(f"/api/review-policies/{A_REPO}/prompt-preview",
                               params={"agent": "defect"}, headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["memories_used"]) == 1
        assert body["memories_omitted"] == 0
        flat = json.dumps(body)
        assert "Money is stored as integer cents." in flat
        assert "Held back." not in flat
