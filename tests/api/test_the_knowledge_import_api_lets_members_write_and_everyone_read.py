"""`/api/kb`: who may import cards, who may read them, and what a reader gets.

A viewer reads the knowledge base and never changes it; a member imports; a
repository of another workspace is 404 — for the cards and for the
configuration they are checked against.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.api.rbac_world import A_REPO, B_REPO, world
from tests.kb.conftest import VEC, card

pytestmark = pytest.mark.asyncio


def _extra():
    from src.api.routers import kb

    return (kb.router,)


@pytest.fixture(autouse=True)
def _vault_and_vectors(tmp_path, monkeypatch):
    """The vault in tmp, an in-memory Qdrant, a free embedder, and the access
    resolver reading the world's database (no rules: members read everything)."""
    from qdrant_client import QdrantClient
    from sqlalchemy import create_engine, event

    from src.access import resolver
    from src.llm import completion
    from tests.api.rbac_world import _sqlite_booleans

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    monkeypatch.setattr(resolver, "_ENGINE", engine)

    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault_of_this_test"))
    client = QdrantClient(":memory:")
    monkeypatch.setattr("src.retrieval.tier1_vault._build_qdrant_client", lambda settings: client)
    monkeypatch.setattr(completion, "embed", lambda *a, **k: list(VEC), raising=False)
    monkeypatch.setattr(completion, "embed_batch", lambda texts, *a, **k: [list(VEC) for _ in texts],
                        raising=False)


def _zip(**files: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


CARDS = _zip(**{"sales/reserve.md": card(id="proc-reserve", status="verified", objects="Документ.Заказ",
                                         extra="domain: sales", body="# Резерв\n\nДив. [[proc-ship]].\n"),
                "sales/ship.md": card(id="proc-ship", body="# Відвантаження\n\nЧернетка.\n")})


async def _readers(w) -> None:
    """A team holding the member and the viewer, with code access to A_REPO: the
    role, not a missing rule, is what decides who may import."""
    from src.db.models import RepoAccessRule, Team, TeamMember

    async with w.factory() as s:
        s.add(Team(id="team-kb", name="kb readers", workspace_id=w.ws["ws-a"]))
        for who in ("member_a", "viewer_a"):
            s.add(TeamMember(team_id="team-kb", user_id=w.uid(who)))
        s.add(RepoAccessRule(id="rule-kb", workspace_id=w.ws["ws-a"], team_id="team-kb",
                             repo_slug=A_REPO, visibility="code", deny_globs=[]))
        await s.commit()


async def _upload(w, who: str, slug: str = A_REPO, **form):
    return await w.client.post(f"/api/kb/{slug}/import", headers=w.h(who, "ws-a"),
                               files=[("files", ("cards.zip", CARDS, "application/zip"))], data=form)


async def test_a_viewer_reads_but_cannot_import_and_a_member_can(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        refused = await _upload(w, "viewer_a")
        done = await _upload(w, "member_a")
        listed = await w.client.get(f"/api/kb/{A_REPO}", headers=w.h("viewer_a", "ws-a"))
        removed_by_viewer = await w.client.delete(f"/api/kb/{A_REPO}", headers=w.h("viewer_a", "ws-a"))
    assert refused.status_code == 403, refused.text
    assert done.status_code == 200, done.text
    assert sorted(done.json()["created"]) == ["kb/sales/reserve.md", "kb/sales/ship.md"]
    assert listed.status_code == 200
    notes = {n["path"]: n for n in listed.json()["notes"]}
    assert notes["kb/sales/reserve.md"]["status"] == "verified"
    assert notes["kb/sales/ship.md"]["status"] == "draft"
    assert removed_by_viewer.status_code == 403


async def test_another_workspaces_repository_is_404_as_target_and_as_configuration(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        foreign_target = await _upload(w, "admin_a", slug=B_REPO)
        foreign_config = await _upload(w, "admin_a", code_repo=B_REPO)
        foreign_read = await w.client.get(f"/api/kb/{B_REPO}", headers=w.h("admin_a", "ws-a"))
    assert foreign_target.status_code == 404, foreign_target.text
    assert foreign_config.status_code == 404, foreign_config.text
    assert foreign_read.status_code == 404


async def test_filters_one_card_with_its_links_and_the_change_log(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        assert (await _upload(w, "member_a")).status_code == 200
        h = w.h("viewer_a", "ws-a")
        by_object = await w.client.get(f"/api/kb/{A_REPO}", params={"object": "Document.Заказ"}, headers=h)
        by_where = await w.client.get(f"/api/kb/{A_REPO}", params={"where": "domain=sales"}, headers=h)
        note = await w.client.get(f"/api/kb/{A_REPO}/note", params={"path": "kb/sales/ship.md"}, headers=h)
        graph = await w.client.get(f"/api/kb/{A_REPO}/graph", headers=h)
        log = await w.client.get(f"/api/kb/{A_REPO}/changes", params={"since": "1d"}, headers=h)
        escape = await w.client.get(f"/api/kb/{A_REPO}/note", params={"path": "../x.md"}, headers=h)
    assert [n["path"] for n in by_object.json()["notes"]] == ["kb/sales/reserve.md"]
    assert [n["path"] for n in by_where.json()["notes"]] == ["kb/sales/reserve.md"]
    body = note.json()
    assert body["kb"]["id"] == "proc-ship"
    assert [b["path"] for b in body["links"]["back"]] == ["kb/sales/reserve.md"]
    edges = graph.json()["edges"]
    assert {"source": "kb/sales/reserve.md", "target": "kb/sales/ship.md", "kind": "link"} in edges \
        or any(e["source"] == "kb/sales/reserve.md" and e["target"] == "kb/sales/ship.md" for e in edges)
    assert {e["path"] for e in log.json()["changes"]} == {"kb/sales/reserve.md", "kb/sales/ship.md"}
    assert escape.status_code == 404


async def test_an_upload_adds_cards_and_removes_others_only_when_asked(tmp_path, monkeypatch):
    """Dropping one card used to remove every other imported card: the form
    defaulted to "the upload is the whole knowledge base"."""
    one = _zip(**{"sales/extra.md": card(id="proc-extra", body="# Додаткова\n\nЩе одна.\n")})

    async def drop(w, **form):
        return await w.client.post(f"/api/kb/{A_REPO}/import", headers=w.h("member_a", "ws-a"),
                                   files=[("files", ("one.zip", one, "application/zip"))], data=form)

    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        assert (await _upload(w, "member_a")).status_code == 200
        added = await drop(w)
        snapshot = await drop(w, replace="true")
    assert added.status_code == 200 and added.json()["created"] == ["kb/sales/extra.md"]
    assert added.json()["deleted"] == []
    assert sorted(snapshot.json()["deleted"]) == ["kb/sales/reserve.md", "kb/sales/ship.md"]
