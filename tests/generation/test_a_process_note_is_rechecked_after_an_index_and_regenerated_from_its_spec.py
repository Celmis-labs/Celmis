"""After the code moves: the note is marked for review, and a regeneration redoes it from its spec."""

from __future__ import annotations

import asyncio

import frontmatter
import pytest

from src.kb import catalog, changes, processes, store
from src.sync import handlers
from tests.kb.conftest import WS, git, kb_world  # noqa: F401
from tests.kb.process_world import COMMON_MODULE, FakeEngine, make_acme_repo

SLUG = "acme"
RESERVE = "резервування"
LEDGER = "облік"


@pytest.fixture
def world(kb_world):  # noqa: F811
    kb_world.head = make_acme_repo(kb_world.repo(SLUG), SLUG)
    processes.save(SLUG, processes.normalise([
        {"id": RESERVE, "name": "Резервування", "objects": ["Документ.ЗаказАкме"]},
        {"id": LEDGER, "name": "Облік", "description": "стара", "objects": ["РегистрНакопления.ОстаткиАкме"]},
    ]))
    from src.generation.process_doc import generate_processes

    generate_processes(SLUG, workspace_id=WS, engine_obj=FakeEngine())
    return kb_world


def _kb(world, pid):
    return frontmatter.load(world.vault(SLUG) / "processes" / f"{pid}.md").metadata["kb"]


def _change(world, rel, text):
    path = world.repo(SLUG) / rel
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
    git(world.repo(SLUG), "commit", "-q", "-am", "change")
    return git(world.repo(SLUG), "rev-parse", "HEAD")


def test_a_process_note_whose_code_changed_is_marked_for_review_after_an_index(world):
    assert _kb(world, RESERVE)["staleness"]["state"] == "fresh"
    _change(world, COMMON_MODULE, "// touched\n")
    store.after_index(SLUG)
    kb = _kb(world, RESERVE)
    assert kb["staleness"]["state"] == "stale" and kb["effective_status"] == "needs-review"
    assert kb["status"] == "draft"  # what it is is unchanged; only the verdict moved
    assert COMMON_MODULE in kb["staleness"]["changed"]
    info = next(n for n in catalog.scan(world.vault(SLUG)) if n.path == f"processes/{RESERVE}.md")
    assert info.status == "needs-review" and info.type == "process" and info.generated
    log = [e for e in changes.read(world.vault(SLUG), since=None, limit=50) if e["path"] == info.path]
    assert log[0]["change"] == "stale" and log[0]["by"] == "sync"


def test_a_change_to_code_the_process_does_not_use_leaves_it_fresh(world):
    _change(world, "Catalogs/ТоварыАкме.xml", "<!-- touched -->\n")
    store.after_index(SLUG)
    assert _kb(world, LEDGER)["staleness"]["state"] == "fresh"


def test_a_regeneration_asked_for_redoes_the_note_from_the_list_spec(world, monkeypatch):
    engine = FakeEngine(text="# Новий текст\n")
    monkeypatch.setattr("src.generation.engines.build_engine", lambda *a, **k: engine)
    processes.save(SLUG, processes.normalise([
        {"id": LEDGER, "name": "Облік", "description": "нова", "objects": ["РегистрНакопления.ОстаткиАкме"]}]))
    handlers._regenerate_notes_sync(SLUG, [f"processes/{LEDGER}.md"], "api", "uk", WS, None)
    assert [c["metadata_context"]["process"]["description"] for c in engine.calls] == ["нова"]
    post = frontmatter.load(world.vault(SLUG) / "processes" / f"{LEDGER}.md")
    assert post.content.startswith("# Новий текст") and post.metadata["kb"]["process"]["description"] == "нова"


def test_a_note_whose_process_left_the_list_is_regenerated_from_the_spec_it_carries(world, monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr("src.generation.engines.build_engine", lambda *a, **k: engine)
    processes.save(SLUG, [])
    handlers._regenerate_notes_sync(SLUG, [f"processes/{LEDGER}.md"], "api", "uk", WS, None)
    assert engine.calls[0]["metadata_context"]["process"]["description"] == "стара"


def test_the_sync_after_a_push_re_embeds_a_process_note_and_spends_no_model_call(world, monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr("src.generation.engines.build_engine", lambda *a, **k: engine)
    handlers._regenerate_notes_sync(SLUG, [f"processes/{LEDGER}.md"], None, None, WS, None)
    assert engine.calls == []
    assert any(p.get("note_path") == f"processes/{LEDGER}.md" for p in world.payloads())


def test_the_job_writes_the_notes_and_fails_only_when_not_one_was_written(world, monkeypatch):
    monkeypatch.setattr("src.sync.queue.is_cancel_requested", lambda job_id: False)
    engine = FakeEngine()
    monkeypatch.setattr("src.generation.engines.build_engine", lambda *a, **k: engine)
    job = {"id": "j1", "payload": {"repo_slug": SLUG, "workspace_id": WS, "ids": [LEDGER], "engine": "api"}}
    asyncio.run(handlers.handle_generate_processes(job))
    assert len(engine.calls) == 1 and processes.last_run(SLUG)["job_id"] == "j1"

    broken = FakeEngine(fail_on=LEDGER)
    monkeypatch.setattr("src.generation.engines.build_engine", lambda *a, **k: broken)
    with pytest.raises(RuntimeError, match="no process note was written"):
        asyncio.run(handlers.handle_generate_processes(job))


def test_the_job_kind_is_registered_with_the_worker():
    import inspect

    from src.sync import queue, worker

    assert "register(jq.KIND_GENERATE_PROCESSES, h.handle_generate_processes)" in inspect.getsource(worker)
    assert queue.KIND_GENERATE_PROCESSES == "generate_processes"
