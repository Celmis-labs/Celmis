"""A team's list of processes becomes one draft description note per process.

The model is faked; the graph is real (a small 1C dump indexed for the test), so
what is checked is what Celmis hands the model and what it writes down.
"""

from __future__ import annotations

import frontmatter
import pytest

from src.generation.process_doc import generate_processes
from src.kb import processes
from src.sync.queue import JobCancelled
from tests.kb.conftest import WS, kb_world  # noqa: F401
from tests.kb.process_world import COMMON_MODULE, OBJECT_MODULE, FakeEngine, make_acme_repo

SLUG = "acme"


@pytest.fixture
def world(kb_world):  # noqa: F811
    head = make_acme_repo(kb_world.repo(SLUG), SLUG)
    kb_world.head = head
    processes.save(SLUG, processes.normalise([
        {"name": "Резервування замовлення", "description": "Замовлення резервує залишки.",
         "objects": ["Документ.ЗаказАкме", "РегистрНакопления.НетТакогоАкме"], "domain": "sales"},
        {"name": "Оформлення замовлення", "objects": ["Documents.ЗаказАкме"]},
        {"name": "Облік залишків", "objects": ["РегистрНакопления.ОстаткиАкме"]},
    ]))
    return kb_world


def _note(world, pid):
    post = frontmatter.load(world.vault(SLUG) / "processes" / f"{pid}.md")
    return post.metadata, post.content


def test_one_draft_note_is_written_for_each_process_of_the_list(world):
    engine = FakeEngine()
    res = generate_processes(SLUG, workspace_id=WS, engine_obj=engine, language="uk")
    assert sorted(res.ok) == sorted(["оформлення-замовлення", "облік-залишків", "резервування-замовлення"])
    assert len(engine.calls) == 3
    meta, body = _note(world, "резервування-замовлення")
    kb = meta["kb"]
    assert meta["type"] == "process" and meta["title"] == "Резервування замовлення"
    assert body.startswith("# Процес")
    assert kb["origin"] == "generate" and kb["status"] == "draft" and kb["effective_status"] == "draft"
    assert kb["id"] == "резервування-замовлення" and kb["code_repo"] == SLUG and kb["domain"] == "sales"
    assert kb["source"] == f"{SLUG}@{world.head}" and kb["source_sha"] == world.head
    assert kb["objects"] == ["Documents.ЗаказАкме", "AccumulationRegisters.НетТакогоАкме"]
    assert kb["process"]["name"] == "Резервування замовлення"
    assert kb["staleness"]["state"] == "fresh"
    # never mistaken for a card a person checked: nothing here says "verified"
    assert "verified" not in str(kb["status"]) and meta["commit"] == world.head


def test_the_model_is_given_the_code_of_the_objects_module_and_of_a_method_that_writes_it(world):
    engine = FakeEngine()
    generate_processes(SLUG, workspace_id=WS, ids=["резервування-замовлення"], engine_obj=engine)
    call = engine.calls[0]
    code = call["code_context"]
    assert "ОбработкаПроведения" in code and OBJECT_MODULE in code  # the object's own module
    assert "СоздатьДокумент" in code and COMMON_MODULE in code  # a method that writes the document
    assert "ПрочитатьЗаказы" in code  # and one that reads it
    meta = call["metadata_context"]
    assert meta["objects_found"] == ["Documents.ЗаказАкме"]
    assert meta["objects_missing"] == ["AccumulationRegisters.НетТакогоАкме"]
    assert any(w["method"].endswith("СоздатьЗаказ") for w in meta["writers"])
    assert [o["id"] for o in meta["other_processes"]] == ["оформлення-замовлення", "облік-залишків"]
    assert call["operation"] == "generate_process_doc"


def test_a_missing_object_is_reported_on_the_note_and_does_not_stop_the_process(world):
    generate_processes(SLUG, workspace_id=WS, engine_obj=FakeEngine())
    kb = _note(world, "резервування-замовлення")[0]["kb"]
    assert kb["objects_missing"] == ["AccumulationRegisters.НетТакогоАкме"]
    assert kb["targets_missing"] == ["AccumulationRegisters.НетТакогоАкме"]
    run = processes.last_run(SLUG)
    assert run["results"]["резервування-замовлення"]["status"] == "ok"


def test_one_process_failing_does_not_stop_the_others_and_is_reported(world):
    res = generate_processes(SLUG, workspace_id=WS, engine_obj=FakeEngine(fail_on="оформлення-замовлення"))
    assert res.failed == ["оформлення-замовлення"]
    assert sorted(res.ok) == ["облік-залишків", "резервування-замовлення"]
    assert not (world.vault(SLUG) / "processes" / "оформлення-замовлення.md").exists()
    run = processes.last_run(SLUG)
    assert "the model refused" in run["results"]["оформлення-замовлення"]["error"]


def test_the_note_is_mapped_to_the_files_it_was_written_from(world):
    generate_processes(SLUG, workspace_id=WS, ids=["резервування-замовлення"], engine_obj=FakeEngine())
    meta, _ = _note(world, "резервування-замовлення")
    assert meta["path"] == OBJECT_MODULE
    assert any(s.startswith(f"{COMMON_MODULE}:") for s in meta["symbols"])
    assert {OBJECT_MODULE, COMMON_MODULE} <= set(meta["kb"]["files"])
    from src.vault.reverse_index import build_reverse_index

    reverse = build_reverse_index(SLUG)
    assert "processes/резервування-замовлення.md" in reverse[COMMON_MODULE]


def test_related_processes_are_those_sharing_an_object_or_linked_by_the_model(world):
    engine = FakeEngine(text="# P\n\nПередає далі: [[облік-залишків]] і [[нема-такого]].\n")
    generate_processes(SLUG, workspace_id=WS, ids=["резервування-замовлення"], engine_obj=engine)
    kb = _note(world, "резервування-замовлення")[0]["kb"]
    assert kb["related"] == ["облік-залишків", "оформлення-замовлення"]


def test_the_change_log_says_generate_and_a_rerun_with_the_same_text_is_no_change(world):
    from src.kb import changes

    generate_processes(SLUG, workspace_id=WS, ids=["облік-залишків"], engine_obj=FakeEngine())
    generate_processes(SLUG, workspace_id=WS, ids=["облік-залишків"], engine_obj=FakeEngine())
    rows = [e for e in changes.read(world.vault(SLUG), since=None, limit=50)
            if e["path"] == "processes/облік-залишків.md"]
    assert [(e["change"], e["by"]) for e in rows] == [("created", "generate")]


def test_a_cancelled_run_stops_between_processes(world):
    with pytest.raises(JobCancelled):
        generate_processes(SLUG, workspace_id=WS, engine_obj=FakeEngine(), cancel_check=lambda: True)
    assert not (world.vault(SLUG) / "processes").exists()


def test_an_unknown_id_is_a_failure_not_a_crash(world):
    res = generate_processes(SLUG, workspace_id=WS, ids=["нема"], engine_obj=FakeEngine())
    assert res.failed == ["нема"] and res.ok == []


def test_a_process_of_plain_code_starts_from_a_symbol_or_a_path(world):
    processes.save(SLUG, processes.normalise([
        {"name": "Кошик", "objects": ["CommonModules/РезервАкме", "ЗаказАкме.НетТакого", "Нема/Файлу.bsl"]}]))
    engine = FakeEngine()
    generate_processes(SLUG, workspace_id=WS, engine_obj=engine)
    meta = engine.calls[0]["metadata_context"]
    assert "CommonModules/РезервАкме" in meta["objects_found"]
    assert set(meta["objects_missing"]) == {"ЗаказАкме.НетТакого", "Нема/Файлу.bsl"}
    assert "СоздатьЗаказ" in engine.calls[0]["code_context"]
    kb = _note(world, "кошик")[0]["kb"]
    assert kb["objects"] == [] and kb["source_sha"] == world.head
