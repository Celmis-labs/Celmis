"""`/api/kb/{slug}/processes`: who may change the list and queue the notes, who may read it.

A viewer reads; a member saves, uploads, deletes and generates; a repository of
another workspace is 404. A list that breaks a limit is refused whole, with the
problem per process. Cards of a knowledge-base repository bound to a code
repository show up as back-links on the code repository's note.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, B_REPO, world
from tests.kb.conftest import VEC, card

pytestmark = pytest.mark.asyncio

KB_REPO = "github_aco-kb"


def _extra():
    from src.api.routers import kb, processes

    return (kb.router, processes.router)


@pytest.fixture(autouse=True)
def _vault_and_vectors(tmp_path, monkeypatch):
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


@pytest.fixture
def queued(monkeypatch):
    jobs: list[dict] = []

    def enqueue(**kw):
        jobs.append(kw)
        return f"job-{len(jobs)}"

    monkeypatch.setattr("src.sync.queue.enqueue", enqueue)
    return jobs


LIST = {"processes": [
    {"name": "Резервування замовлення", "description": "Резерв залишків.",
     "objects": ["Документ.ЗаказКлиента", "РегистрНакопления.ТоварыВРезерве"]},
    {"name": "Відвантаження", "objects": ["Documents.Реализация"], "domain": "sales"},
]}


async def _readers(w) -> None:
    from src.db.models import RepoAccessRule, Team, TeamMember

    async with w.factory() as s:
        s.add(Team(id="team-pr", name="readers", workspace_id=w.ws["ws-a"]))
        for who in ("member_a", "viewer_a"):
            s.add(TeamMember(team_id="team-pr", user_id=w.uid(who)))
        s.add(RepoAccessRule(id="rule-pr", workspace_id=w.ws["ws-a"], team_id="team-pr",
                             repo_slug=A_REPO, visibility="code", deny_globs=[]))
        await s.commit()


URL = f"/api/kb/{A_REPO}/processes"


async def test_a_viewer_reads_the_list_but_cannot_change_it_and_a_member_can(tmp_path, monkeypatch, queued):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        refused = await w.client.put(URL, json=LIST, headers=w.h("viewer_a", "ws-a"))
        saved = await w.client.put(URL, json=LIST, headers=w.h("member_a", "ws-a"))
        listed = await w.client.get(URL, headers=w.h("viewer_a", "ws-a"))
        gen_viewer = await w.client.post(f"{URL}/generate", json={}, headers=w.h("viewer_a", "ws-a"))
        del_viewer = await w.client.delete(f"{URL}/відвантаження", headers=w.h("viewer_a", "ws-a"))
    assert refused.status_code == 403 and gen_viewer.status_code == 403 and del_viewer.status_code == 403
    assert saved.status_code == 200, saved.text
    rows = listed.json()["processes"]
    assert [r["id"] for r in rows] == ["резервування-замовлення", "відвантаження"]
    assert rows[0]["canonical_objects"] == ["Documents.ЗаказКлиента", "AccumulationRegisters.ТоварыВРезерве"]
    assert listed.json()["limits"]["processes"] == 200
    assert queued == []


async def test_another_workspaces_repository_is_404_for_every_route(tmp_path, monkeypatch, queued):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        h = w.h("admin_a", "ws-a")
        base = f"/api/kb/{B_REPO}/processes"
        got = [await w.client.get(base, headers=h), await w.client.put(base, json=LIST, headers=h),
               await w.client.post(f"{base}/generate", json={}, headers=h),
               await w.client.delete(f"{base}/x", headers=h)]
    assert [r.status_code for r in got] == [404, 404, 404, 404]
    assert queued == []


async def test_a_list_over_a_limit_is_refused_whole_with_the_problem_per_process(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        h = w.h("admin_a", "ws-a")
        bad = {"processes": [{"name": "Добре"}, {"name": "", "description": "x"},
                             {"name": "Третій", "id": "../x"}]}
        res = await w.client.put(URL, json=bad, headers=h)
        after = await w.client.get(URL, headers=h)
    assert res.status_code == 400
    problems = res.json()["detail"]["problems"]
    assert any(p.startswith("#2") for p in problems) and any(p.startswith("#3") for p in problems)
    assert after.json()["processes"] == []  # nothing was saved


async def test_an_upload_of_yaml_json_or_csv_replaces_or_merges(tmp_path, monkeypatch):
    yaml_text = "processes:\n  - name: Резерв\n    objects: [Документ.А]\n"
    csv_text = "name;objects\nОблік;Документ.Б\n"
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        h = w.h("admin_a", "ws-a")
        first = await w.client.post(f"{URL}/upload", headers=h,
                                    files={"file": ("l.yaml", yaml_text.encode(), "text/yaml")})
        merged = await w.client.post(f"{URL}/upload", headers=h, data={"replace": "false"},
                                     files={"file": ("l.csv", csv_text.encode(), "text/csv")})
        broken = await w.client.post(f"{URL}/upload", headers=h,
                                     files={"file": ("l.json", b"{nope", "application/json")})
        viewer = await w.client.post(f"{URL}/upload", headers=w.h("viewer_a", "ws-a"),
                                     files={"file": ("l.yaml", yaml_text.encode(), "text/yaml")})
    assert [r["id"] for r in first.json()["processes"]] == ["резерв"]
    assert [r["id"] for r in merged.json()["processes"]] == ["резерв", "облік"]
    assert broken.status_code == 400 and "cannot read" in broken.json()["detail"]["message"]
    assert viewer.status_code == 403


async def test_generate_queues_one_job_for_all_or_the_chosen_processes_and_checks_the_choice(
        tmp_path, monkeypatch, queued):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        h = w.h("member_a", "ws-a")
        empty = await w.client.post(f"{URL}/generate", json={}, headers=h)
        await w.client.put(URL, json=LIST, headers=h)
        everything = await w.client.post(f"{URL}/generate", json={"language": "uk", "engine": "api"}, headers=h)
        one = await w.client.post(f"{URL}/generate", json={"ids": ["відвантаження"]}, headers=h)
        unknown = await w.client.post(f"{URL}/generate", json={"ids": ["нема"]}, headers=h)
        bad_engine = await w.client.post(f"{URL}/generate", json={"engine": "magic"}, headers=h)
        bad_lang = await w.client.post(f"{URL}/generate", json={"language": "xx"}, headers=h)
    assert empty.status_code == 400
    assert everything.status_code == 202 and everything.json()["job_id"] == "job-1"
    assert everything.json()["count"] == 2
    assert one.status_code == 202
    assert unknown.status_code == 400 and bad_engine.status_code == 400 and bad_lang.status_code == 400
    assert [j["kind"] for j in queued] == ["generate_processes", "generate_processes"]
    first = queued[0]
    assert first["payload"]["repo_slug"] == A_REPO and first["payload"]["ids"] is None
    assert first["payload"]["language"] == "uk" and first["payload"]["engine"] == "api"
    assert queued[1]["payload"]["ids"] == ["відвантаження"]
    assert first["dedup_key"].startswith("generate_processes:") and first["dedup_key"].endswith(f":{A_REPO}:all")
    assert queued[1]["dedup_key"] != first["dedup_key"]


async def test_a_run_already_queued_is_reported_not_repeated(tmp_path, monkeypatch):
    monkeypatch.setattr("src.sync.queue.enqueue", lambda **kw: None)
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        h = w.h("member_a", "ws-a")
        await w.client.put(URL, json=LIST, headers=h)
        res = await w.client.post(f"{URL}/generate", json={}, headers=h)
    assert res.status_code == 202 and res.json()["queued"] is False


async def test_deleting_a_process_removes_it_and_its_generated_note(tmp_path, monkeypatch):
    from src.config import get_settings
    from src.vault.writer import NoteMetadata, VaultWriter

    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        h = w.h("member_a", "ws-a")
        await w.client.put(URL, json=LIST, headers=h)
        VaultWriter(workspace_id=None).write(
            repo=A_REPO, relative_path="processes/відвантаження.md", content="# В\n",
            metadata=NoteMetadata(type="process", repo=A_REPO, extra={"kb": {
                "origin": "generate", "id": "відвантаження", "status": "draft"}}), index_in_qdrant=False)
        note = get_settings().repo_vault_path(A_REPO) / "processes" / "відвантаження.md"
        assert note.exists()
        gone = await w.client.delete(f"{URL}/відвантаження", headers=h)
        again = await w.client.delete(f"{URL}/відвантаження", headers=h)
        listed = await w.client.get(URL, headers=h)
    assert gone.status_code == 200 and gone.json()["note_removed"] is True and not note.exists()
    assert again.status_code == 404
    assert [r["id"] for r in listed.json()["processes"]] == ["резервування-замовлення"]


async def test_the_list_shows_each_processes_note_and_the_last_result(tmp_path, monkeypatch):
    from src.kb import processes
    from src.vault.writer import NoteMetadata, VaultWriter

    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        await _readers(w)
        h = w.h("member_a", "ws-a")
        await w.client.put(URL, json=LIST, headers=h)
        VaultWriter(workspace_id=None).write(
            repo=A_REPO, relative_path="processes/відвантаження.md", content="# В\n",
            metadata=NoteMetadata(type="process", repo=A_REPO, extra={"kb": {
                "origin": "generate", "id": "відвантаження", "status": "draft", "effective_status": "needs-review",
                "staleness": {"state": "stale"}, "targets_missing": ["Documents.Нема"]}}), index_in_qdrant=False)
        processes.save_run(A_REPO, {"job_id": "j", "results": {
            "резервування-замовлення": {"status": "failed", "error": "RuntimeError: x"}}})
        rows = {r["id"]: r for r in (await w.client.get(URL, headers=h)).json()["processes"]}
    note = rows["відвантаження"]["note"]
    assert note["path"] == "processes/відвантаження.md" and note["stale"] is True
    assert note["effective_status"] == "needs-review" and note["objects_missing"] == ["Documents.Нема"]
    assert rows["резервування-замовлення"]["result"]["status"] == "failed"
    assert "note" not in rows["резервування-замовлення"]


async def test_a_card_of_a_bound_repository_is_a_back_link_and_an_external_graph_node(tmp_path, monkeypatch):
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.kb import store

    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        h = w.h("admin_a", "ws-a")
        get_auto_review_store().upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=KB_REPO, provider="github", full_name="aco/kb",
            url="https://github.com/aco/kb", workspace_id=w.ws["ws-a"]))
        assert (await w.client.put(URL, json=LIST, headers=h)).status_code == 200
        from src.vault.writer import NoteMetadata, VaultWriter

        VaultWriter(workspace_id=None).write(
            repo=A_REPO, relative_path="processes/відвантаження.md", content="# Відвантаження\n",
            metadata=NoteMetadata(type="process", repo=A_REPO, extra={"title": "Відвантаження", "kb": {
                "origin": "generate", "id": "відвантаження", "status": "draft"}}), index_in_qdrant=False)
        store.import_cards(KB_REPO, [("ship.md", card(
            id="proc-ship", status="verified", extra="replaces: [processes/відвантаження]",
            body="# Відвантаження (перевірено)\n"))], workspace_id=w.ws["ws-a"], code_repo=A_REPO)
        note = await w.client.get(f"/api/kb/{A_REPO}/note", params={"path": "processes/відвантаження.md"},
                                  headers=h)
        graph = await w.client.get(f"/api/kb/{KB_REPO}/graph", headers=h)
        listed = await w.client.get(f"/api/kb/{A_REPO}", params={"type": "process", "status": "draft"}, headers=h)
    back = [b for b in note.json()["links"]["back"] if b.get("repo")]
    assert back == [{"path": "kb/ship.md", "title": "Відвантаження (перевірено)", "kind": "replaces",
                     "repo": KB_REPO}]
    assert note.json()["origin"] == "generate" and note.json()["status"] == "draft"
    nodes = {n["path"]: n for n in graph.json()["nodes"]}
    ext = nodes[f"{A_REPO}:processes/відвантаження.md"]
    assert ext["external"] is True and ext["repo"] == A_REPO and ext["title"] == "Відвантаження"
    assert {"source": "kb/ship.md", "target": f"{A_REPO}:processes/відвантаження.md", "kind": "replaces",
            "repo": A_REPO} in graph.json()["edges"]
    assert [n["path"] for n in listed.json()["notes"]] == ["processes/відвантаження.md"]
