"""GDPR and the feedback signals: the export lists what a person said, the
erasure keeps the knowledge but cuts the link to the person. A repository
removed from the workspace takes its signals and its comment map with it, and
only its own, in its own workspace.
"""

from __future__ import annotations

import json

from src.review.learning import signals as sig
from tests.api.rbac_world import A_REPO, world

PR = sig.PRRef("github", "aco/app", 3)


def _routers() -> tuple:
    from src.api.routers import gdpr

    return (gdpr.router,)


def _snap(title: str) -> sig.FindingSnapshot:
    return sig.FindingSnapshot(title=title, file_path="src/a.py", rule_id="defect.x")


def _say(ws: str, actor: str, title: str, repo: str = A_REPO) -> None:
    assert sig.record_verdict(ws, repo, _snap(title), "dismissed", "reply", pr=PR,
                              actor=actor) == "created"


async def test_the_export_lists_the_signals_a_person_gave_and_nobody_elses(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        email = w.users["editor_a"].email
        _say(w.ws["ws-a"], email, "Mine")
        _say(w.ws["ws-a"], "other@acme-corp.io", "Somebody else's")
        r = await w.client.get(f"/api/gdpr/export/{w.uid('editor_a')}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert [s["title"] for s in r.json()["feedback_signals"]] == ["Mine"]
        assert "Somebody else" not in json.dumps(r.json())


async def test_an_erased_person_keeps_no_name_on_a_signal_but_the_signal_stays(
        tmp_path, monkeypatch):
    from sqlalchemy import select

    from src.db.models import FindingSignal

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        user = w.users["editor_a"]
        _say(w.ws["ws-a"], user.email, "Mine")
        _say(w.ws["ws-a"], "other@acme-corp.io", "Somebody else's")
        r = await w.client.delete(f"/api/gdpr/user/{user.id}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert r.json()["signals_unlinked"] == 1
        async with w.factory() as s:
            rows = {x.title: x.actor for x in (await s.scalars(select(FindingSignal))).all()}
        assert rows["Mine"] == f"deleted-{user.id[:12]}@erased.local"
        assert rows["Somebody else's"] == "other@acme-corp.io"


async def test_erasing_a_person_who_signalled_under_email_and_id_does_not_collide(
        tmp_path, monkeypatch):
    from sqlalchemy import select

    from src.db.models import FindingSignal

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        user = w.users["editor_a"]
        _say(w.ws["ws-a"], user.email, "Same finding")
        _say(w.ws["ws-a"], user.id, "Same finding")
        _say(w.ws["ws-a"], user.id, "Another finding")
        r = await w.client.delete(f"/api/gdpr/user/{user.id}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        async with w.factory() as s:
            rows = sorted((x.title, x.actor) for x in (await s.scalars(select(FindingSignal))).all())
        anon = f"deleted-{user.id[:12]}@erased.local"
        assert rows == [("Another finding", anon), ("Same finding", anon)]


async def test_removing_a_repository_removes_its_signals_and_comment_map_in_its_workspace(
        tmp_path, monkeypatch):
    from sqlalchemy import select

    from src.db.models import FindingSignal, PostedFindingComment
    from src.repos.purge import PurgeReport, _purge_postgres

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        a, b = w.ws["ws-a"], w.ws["ws-b"]
        _say(a, "jane", "Gone")
        _say(a, "jane", "Kept: another repository", repo="github_aco-other")
        _say(b, "jane", "Kept: another workspace")
        f = _snap("Gone")
        sig.record_posted(a, PR, [f], [{"comment_id": 1, "fingerprint":
                                        sig.snapshot_of(f).fingerprint[:16]}], repo_slug=A_REPO)
        async with w.factory() as s:
            await _purge_postgres(A_REPO, s, PurgeReport(slug=A_REPO), workspace_id=a)
        async with w.factory() as s:
            left = sorted(x.title for x in (await s.scalars(select(FindingSignal))).all())
            posted = (await s.scalars(select(PostedFindingComment))).all()
        assert left == ["Kept: another repository", "Kept: another workspace"]
        assert posted == []


async def test_removing_a_repository_removes_its_vectors_too(tmp_path, monkeypatch):
    from src.repos import purge
    from src.review.learning import similarity
    from tests.review.learning_db import FakeQdrant, hash_embed

    q = FakeQdrant()
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        a = w.ws["ws-a"]
        _say(a, "jane", "Gone")
        monkeypatch.setattr(similarity, "_qdrant", lambda client=None: q)
        monkeypatch.setattr("src.retrieval.vector_store.get_vector_client", lambda: q)
        assert similarity.index_pending(a, A_REPO, client=q, embed=hash_embed) == 1
        assert len(q.points) == 1
        async with w.factory() as s:
            report = await purge.purge_repo(A_REPO, session=s, workspace_id=a,
                                            skip_qdrant=False, skip_disk=True)
        assert q.points == {}, report.errors
