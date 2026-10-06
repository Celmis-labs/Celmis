"""Who may touch /api/memories, role by role and repository by repository.

The role rule: viewer and member get 403 on EVERY endpoint (reads included);
editor, admin, owner, a global admin and the superadmin pass.

The repository rule: a repository's memories are shown, counted, previewed and
edited only to somebody who may read that repository (owners, admins and
global admins hold every repository of their workspace; an editor needs a
team grant). A repository one may not read is never named, and an id of one of
its memories answers 404, not 403.

Built on tests/api/rbac_world.py (workspace A has the repository `A_REPO`, which
the A team grants `review` on; the tests add a repository nobody holds and one
that is read-only).
"""

from __future__ import annotations

import json

import pytest

from tests.api.rbac_world import A_REPO, world

URL = "/api/memories"
HIDDEN = "github_aco-hidden"
READ_ONLY = "github_aco-readonly"

REFUSED = ("viewer_a", "member_a")
PASS = ("editor_a", "admin_a", "owner_a", "gadmin", "su")


def _routers() -> tuple:
    from src.api.routers import memories

    return (memories.router,)


def _world(tmp_path, monkeypatch):
    return world(tmp_path, monkeypatch, extra_routers=_routers())


async def _seed(w) -> dict[str, int]:
    """A workspace memory and one per repository (A_REPO, HIDDEN, READ_ONLY)."""
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.db.models import RepoTeamAccess, ReviewMemory, Team

    ws = w.ws["ws-a"]
    async with w.factory() as s:
        s.add(Team(id="team-empty", name="nobody", description="", workspace_id=ws))
        s.add(RepoTeamAccess(repo_slug=HIDDEN, team_id="team-empty", permission="admin"))
        s.add(RepoTeamAccess(repo_slug=READ_ONLY, team_id=w.ids["team_a"], permission="read"))
        rows = {
            "ws": ReviewMemory(workspace_id=ws, text="Workspace fact.", status="active"),
            "mine": ReviewMemory(workspace_id=ws, repo_slug=A_REPO, text="Granted repo fact.",
                                 status="active"),
            "hidden": ReviewMemory(workspace_id=ws, repo_slug=HIDDEN, text="Hidden repo fact.",
                                   status="pending"),
            "ro": ReviewMemory(workspace_id=ws, repo_slug=READ_ONLY, text="Read-only repo fact.",
                               status="active"),
        }
        for row in rows.values():
            s.add(row)
        await s.commit()
        ids = {k: r.id for k, r in rows.items()}
    for slug in (HIDDEN, READ_ONLY):
        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=slug, provider="github",
            full_name=slug.replace("github_", "").replace("-", "/", 1),
            url="https://github.com/aco/x", workspace_id=ws))
    return ids


def _requests(ids: dict[str, int]) -> list[tuple[str, str, dict | None]]:
    mid = ids["ws"]
    return [
        ("GET", URL, None),
        ("GET", f"{URL}/preview", None),
        ("POST", URL, {"text": "A new fact."}),
        ("PATCH", f"{URL}/{mid}", {"text": "Changed."}),
        ("DELETE", f"{URL}/{mid}", None),
        ("POST", f"{URL}/bulk-status", {"ids": [mid], "status": "rejected"}),
        ("POST", f"{URL}/bulk-delete", {"ids": [mid]}),
    ]


async def _call(w, who: str, method: str, url: str, body: dict | None):
    return await w.client.request(method, url, json=body, headers=w.h(who, "ws-a"))


@pytest.mark.parametrize("who", REFUSED)
async def test_viewers_and_members_are_refused_on_every_endpoint(tmp_path, monkeypatch, who):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        for method, url, body in _requests(ids):
            r = await _call(w, who, method, url, body)
            assert r.status_code == 403, f"{who} {method} {url}: {r.status_code}"
            assert "fact" not in r.text, "a refusal carries no memory"
        kept = (await _call(w, "owner_a", "GET", URL, None)).json()
        assert kept["counts"]["all"] == 4
        assert {m["text"] for m in kept["memories"]} >= {"Workspace fact."}


@pytest.mark.parametrize("who", PASS)
async def test_editors_admins_owners_and_global_admins_pass_the_gate(tmp_path, monkeypatch, who):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        for method, url, body in _requests(ids):
            r = await _call(w, who, method, url, body)
            assert r.status_code in (200, 201), f"{who} {method} {url}: {r.status_code} {r.text}"
            if method == "DELETE" or url.endswith("bulk-delete"):
                break  # the shared id is gone; the reads above already passed


async def test_a_role_in_another_workspace_opens_nothing_of_this_one(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        for who in ("admin_b", "member_b", "loner"):
            r = await w.client.get(URL, headers=w.h(who, "ws-a"))
            # A header naming a workspace one is not in pins one to their own
            # (here B, which has no memories); one with no workspace is refused.
            assert r.status_code in (200, 403, 404), f"{who}: {r.status_code}"
            assert "fact" not in r.text, f"{who} read workspace A's memories"


@pytest.mark.parametrize("who, sees_hidden", [
    ("editor_a", False), ("admin_a", True), ("owner_a", True), ("gadmin", True), ("su", True),
])
async def test_the_list_and_its_counts_leave_out_repositories_one_may_not_read(
    tmp_path, monkeypatch, who, sees_hidden,
):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        r = await w.client.get(URL, headers=w.h(who, "ws-a"))
        assert r.status_code == 200, r.text
        body = r.json()
        texts = {m["text"] for m in body["memories"]}
        expected = {"Workspace fact.", "Granted repo fact.", "Read-only repo fact."}
        # the hidden one is pending: it is in the "all" count, not in the default list
        assert expected <= texts or body["counts"]["active"] >= 3
        everything = await w.client.get(URL, params={"status": "pending"}, headers=w.h(who, "ws-a"))
        assert ("Hidden repo fact." in everything.text) is sees_hidden
        assert (HIDDEN in r.text + everything.text) is sees_hidden
        assert body["counts"]["all"] == (4 if sees_hidden else 3)
        assert body["counts"]["pending"] == (1 if sees_hidden else 0)


async def test_asking_for_a_repository_one_may_not_read_is_a_404(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        h = w.h("editor_a", "ws-a")
        for url in (URL, f"{URL}/preview"):
            r = await w.client.get(url, params={"repo": HIDDEN}, headers=h)
            # The same answer as for a repository that is not there.
            assert r.status_code == 404, f"{url}: {r.status_code}"
            assert "Hidden repo fact." not in r.text
        for who in ("admin_a", "owner_a"):
            r = await w.client.get(URL, params={"repo": HIDDEN, "status": "pending"},
                                   headers=w.h(who, "ws-a"))
            assert r.status_code == 200
            assert [m["text"] for m in r.json()["memories"]] == ["Hidden repo fact."]


async def test_the_preview_shows_only_what_the_caller_may_read(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        r = await w.client.get(f"{URL}/preview", params={"repo": A_REPO},
                               headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 200
        assert {m["text"] for m in r.json()["used"]} == {"Workspace fact.", "Granted repo fact."}
        bare = await w.client.get(f"{URL}/preview", headers=w.h("editor_a", "ws-a"))
        assert {m["text"] for m in bare.json()["used"]} == {"Workspace fact."}


async def test_a_memory_of_an_unreadable_repository_is_a_404_for_every_write(tmp_path, monkeypatch):
    from src.db.models import ReviewMemory

    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        hid = ids["hidden"]
        h = w.h("editor_a", "ws-a")
        for method, url, body in (
            ("PATCH", f"{URL}/{hid}", {"status": "active"}),
            ("DELETE", f"{URL}/{hid}", None),
            ("POST", f"{URL}/bulk-status", {"ids": [hid], "status": "active"}),
            ("POST", f"{URL}/bulk-delete", {"ids": [hid]}),
            ("POST", f"{URL}/bulk-status", {"ids": [ids["ws"], hid], "status": "rejected"}),
        ):
            r = await w.client.request(method, url, json=body, headers=h)
            assert r.status_code == 404, f"{method} {url}: {r.status_code}"
        r = await w.client.post(URL, json={"text": "Planted.", "repo_slug": HIDDEN}, headers=h)
        assert r.status_code in (403, 404)
        async with w.factory() as s:
            assert (await s.get(ReviewMemory, hid)).status == "pending"
            assert (await s.get(ReviewMemory, ids["ws"])).status == "active"


async def test_read_access_alone_shows_a_repositorys_memories_but_does_not_let_one_edit_them(
    tmp_path, monkeypatch,
):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        h = w.h("editor_a", "ws-a")
        listed = await w.client.get(URL, params={"repo": READ_ONLY}, headers=h)
        assert listed.status_code == 200
        assert "Read-only repo fact." in listed.text
        for method, url, body in (
            ("PATCH", f"{URL}/{ids['ro']}", {"text": "Rewritten."}),
            ("DELETE", f"{URL}/{ids['ro']}", None),
            ("POST", f"{URL}/bulk-delete", {"ids": [ids["ro"]]}),
        ):
            assert (await w.client.request(method, url, json=body, headers=h)).status_code == 403
        made = await w.client.post(URL, json={"text": "New.", "repo_slug": READ_ONLY}, headers=h)
        assert made.status_code == 403
        # an admin holds every repository of the workspace
        r = await w.client.patch(f"{URL}/{ids['ro']}", json={"text": "Rewritten."},
                                 headers=w.h("admin_a", "ws-a"))
        assert r.status_code == 200


async def test_the_prompt_preview_hides_memories_from_members_and_unreadable_repositories(
    tmp_path, monkeypatch,
):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)

        async def preview(who: str, repo: str) -> str:
            r = await w.client.get(f"/api/review-policies/{repo}/prompt-preview",
                                   params={"agent": "defect"}, headers=w.h(who, "ws-a"))
            assert r.status_code == 200, f"{who} {repo}: {r.text}"
            return json.dumps(r.json())

        seen = await preview("editor_a", A_REPO)
        assert "Granted repo fact." in seen and "Workspace fact." in seen
        for who in REFUSED:
            flat = await preview(who, A_REPO)
            assert "Granted repo fact." not in flat and "Workspace fact." not in flat
        assert "Hidden repo fact." not in await preview("editor_a", HIDDEN)
        assert "Workspace fact." not in await preview("editor_a", HIDDEN)
        assert "Workspace fact." in await preview("admin_a", HIDDEN)
        wide = await w.client.get("/api/review-policies/prompt-preview",
                                  params={"agent": "defect"}, headers=w.h("member_a", "ws-a"))
        assert wide.status_code == 200 and "Workspace fact." not in wide.text


@pytest.mark.parametrize("who, sees_source", [
    ("editor_a", False), ("admin_a", True), ("owner_a", True),
])
async def test_a_workspace_memory_does_not_say_which_unreadable_repository_it_came_from(
    tmp_path, monkeypatch, who, sees_source,
):
    from src.db.models import ReviewMemory

    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        async with w.factory() as s:
            s.add(ReviewMemory(
                workspace_id=w.ws["ws-a"], text="Taught in a private repository.",
                status="active", source_provider="github", source_repo="aco/hidden",
                source_pr=12, source_comment_id="991",
                source_url="https://github.com/aco/hidden/pull/12#c991"))
            await s.commit()
        r = await w.client.get(URL, headers=w.h(who, "ws-a"))
        row = next(m for m in r.json()["memories"]
                   if m["text"] == "Taught in a private repository.")
        assert (row["source_repo"] == "aco/hidden") is sees_source
        assert (row["source_url"] is not None) is sees_source
        assert (row["source_pr"] == 12) is sees_source
        assert ("aco/hidden" in r.text) is sees_source
