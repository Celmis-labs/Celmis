"""Who may read what the team's feedback taught, and who may forget a signal.

The gate is the memories one: viewer and member get 403 on every endpoint;
editor, admin, owner, a global admin and the superadmin pass. A repository one
may not read is never named (its signals are not listed, counted or
forgettable: 404), only workspace admins see who gave a signal, and another
workspace's signals do not exist here.
"""

from __future__ import annotations

import pytest

from src.review.learning import signals as sig
from tests.api.rbac_world import A_REPO, world

URL = "/api/learning"
HIDDEN = "github_aco-hidden"
READ_ONLY = "github_aco-readonly"
REFUSED = ("viewer_a", "member_a")
PASS = ("editor_a", "admin_a", "owner_a", "gadmin", "su")


def _routers() -> tuple:
    from src.api.routers import learning

    return (learning.router,)


def _world(tmp_path, monkeypatch):
    return world(tmp_path, monkeypatch, extra_routers=_routers())


def _snap(title: str) -> sig.FindingSnapshot:
    return sig.FindingSnapshot(title=title, file_path="src/a.py", rule_id="defect.x")


async def _seed(w) -> dict[str, str]:
    from sqlalchemy import select

    from src.db.models import FindingSignal, RepoTeamAccess, Team

    ws = w.ws["ws-a"]
    async with w.factory() as s:
        s.add(Team(id="team-empty", name="nobody", description="", workspace_id=ws))
        s.add(RepoTeamAccess(repo_slug=HIDDEN, team_id="team-empty", permission="admin"))
        s.add(RepoTeamAccess(repo_slug=READ_ONLY, team_id=w.ids["team_a"], permission="read"))
        await s.commit()
    from src.api.auto_review import RepoConfig, get_auto_review_store

    for slug in (HIDDEN, READ_ONLY):
        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=slug, provider="github",
            full_name=slug.replace("github_", "").replace("-", "/", 1),
            url="https://github.com/aco/x", workspace_id=ws))
    pr = sig.PRRef("github", "aco/x", 1)
    for repo, title in ((A_REPO, "Mine"), (HIDDEN, "Hidden"), (READ_ONLY, "Readonly")):
        assert sig.record_verdict(ws, repo, _snap(title), "dismissed", "reply", pr=pr,
                                  actor="jane@example.com") == "created"
    assert sig.record_verdict(w.ws["ws-b"], A_REPO, _snap("Other workspace"), "dismissed",
                              "reply", pr=pr, actor="x") == "created"
    async with w.factory() as s:
        rows = (await s.scalars(select(FindingSignal))).all()
        return {r.title: r.id for r in rows}


@pytest.mark.parametrize("who", REFUSED)
async def test_viewers_and_members_are_refused_on_every_endpoint(tmp_path, monkeypatch, who):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        h = w.h(who, "ws-a")
        for method, url in (("GET", f"{URL}/summary"), ("GET", f"{URL}/signals"),
                            ("DELETE", f"{URL}/signals/{ids['Mine']}")):
            r = await w.client.request(method, url, headers=h)
            assert r.status_code == 403, f"{who} {method} {url}: {r.status_code}"
            assert "Mine" not in r.text


@pytest.mark.parametrize("who", PASS)
async def test_editors_admins_owners_and_global_admins_pass_the_gate(tmp_path, monkeypatch, who):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        for url in (f"{URL}/summary", f"{URL}/signals"):
            r = await w.client.get(url, headers=w.h(who, "ws-a"))
            assert r.status_code == 200, f"{who} {url}: {r.status_code} {r.text}"


@pytest.mark.parametrize("who, sees_hidden", [
    ("editor_a", False), ("admin_a", True), ("owner_a", True)])
async def test_the_lists_and_counts_leave_out_repositories_one_may_not_read(
        tmp_path, monkeypatch, who, sees_hidden):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        h = w.h(who, "ws-a")
        listed = (await w.client.get(f"{URL}/signals", headers=h)).json()
        titles = {s["title"] for s in listed["signals"]}
        assert ("Hidden" in titles) is sees_hidden
        assert {"Mine", "Readonly"} <= titles and "Other workspace" not in titles
        assert listed["total"] == (3 if sees_hidden else 2)
        summary = (await w.client.get(f"{URL}/summary", headers=h)).json()
        assert summary["total"] == listed["total"]
        assert ("Hidden" in str(summary["top_dismissed"])) is sees_hidden
        assert (HIDDEN in str(summary) + str(listed)) is sees_hidden


async def test_asking_for_an_unreadable_repository_is_a_404_for_an_editor(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        for url in (f"{URL}/summary", f"{URL}/signals"):
            r = await w.client.get(url, params={"repo": HIDDEN}, headers=w.h("editor_a", "ws-a"))
            assert r.status_code == 404 and "Hidden" not in r.text
        ok = await w.client.get(f"{URL}/signals", params={"repo": HIDDEN},
                                headers=w.h("admin_a", "ws-a"))
        assert [s["title"] for s in ok.json()["signals"]] == ["Hidden"]


async def test_only_a_workspace_admin_sees_who_gave_a_signal(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        editor = (await w.client.get(f"{URL}/signals", headers=w.h("editor_a", "ws-a"))).json()
        admin = (await w.client.get(f"{URL}/signals", headers=w.h("admin_a", "ws-a"))).json()
        assert "jane@example.com" not in str(editor)
        assert "jane@example.com" in str(admin)


async def test_the_list_can_be_narrowed_and_a_bad_filter_is_refused(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        await _seed(w)
        h = w.h("admin_a", "ws-a")
        none = await w.client.get(f"{URL}/signals", params={"signal": "accepted"}, headers=h)
        assert none.json()["signals"] == []
        page = await w.client.get(f"{URL}/signals", params={"limit": 1, "offset": 1}, headers=h)
        assert len(page.json()["signals"]) == 1 and page.json()["total"] == 3
        bad = await w.client.get(f"{URL}/signals", params={"signal": "loved"}, headers=h)
        assert bad.status_code == 422


async def test_forgetting_a_signal_removes_it(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        r = await w.client.delete(f"{URL}/signals/{ids['Mine']}", headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 204
        left = (await w.client.get(f"{URL}/signals", headers=w.h("admin_a", "ws-a"))).json()
        assert "Mine" not in {s["title"] for s in left["signals"]}
        again = await w.client.delete(f"{URL}/signals/{ids['Mine']}",
                                      headers=w.h("editor_a", "ws-a"))
        assert again.status_code == 404


async def test_a_signal_of_another_workspace_or_an_unreadable_repository_is_a_404(
        tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        h = w.h("editor_a", "ws-a")
        for title in ("Other workspace", "Hidden"):
            r = await w.client.delete(f"{URL}/signals/{ids[title]}", headers=h)
            assert r.status_code == 404, title
        assert (await w.client.delete(f"{URL}/signals/nope", headers=h)).status_code == 404
        left = (await w.client.get(f"{URL}/signals", headers=w.h("admin_a", "ws-a"))).json()
        assert "Hidden" in {s["title"] for s in left["signals"]}


async def test_read_access_alone_lets_one_see_a_signal_but_not_forget_it(tmp_path, monkeypatch):
    async with _world(tmp_path, monkeypatch) as w:
        ids = await _seed(w)
        r = await w.client.delete(f"{URL}/signals/{ids['Readonly']}",
                                  headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 403
