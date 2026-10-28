"""`read_symbol` and `refs` find a 1C method by the name a reader gives.

THE CONTRACT. A 1C symbol's name carries its container
(``Documents.Acme.ObjectModule.Post``), so the last segment alone is never a
whole name. `GraphStore.find_named` — the lookup both tools use — takes the
dotted name whole first, then the last segment as a whole name or as the last
segment of one. Before it, both tools looked the last segment up as a whole
name and answered "no symbol" for every 1C method, listing the very name they
had missed under "similar".

Fixtures are synthetic: an `acme` configuration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.indexing.graph.graph_store import make_graph_store
from src.indexing.graph.pipeline import index_repo_graph

SLUG = "acme-erp"
DOC = "Documents/РаскройАкме/Ext/ObjectModule.bsl"
COMMON = "CommonModules/ОбщийАкме/Ext/Module.bsl"

_MODULE = """\
Процедура ВыполнитьРаскрой(Лист) Экспорт
    Результат = Лист;
КонецПроцедуры
"""


@pytest.fixture()
def store(tmp_path: Path):
    repo = tmp_path / "repos" / SLUG
    for rel in (DOC, COMMON):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_MODULE, encoding="utf-8")
    cfg = Settings(workspace_dir=tmp_path)
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    s = make_graph_store(cfg.repo_graph_path(SLUG))
    try:
        yield s
    finally:
        s.close()


def _files(rows: list[dict]) -> list[str]:
    return sorted(str(r["file"]) for r in rows)


def test_the_bare_method_name_finds_it_in_every_module(store):
    assert _files(store.find_named("ВыполнитьРаскрой")) == sorted([DOC, COMMON])


@pytest.mark.parametrize("name, file", [
    ("Documents.РаскройАкме.ObjectModule.ВыполнитьРаскрой", DOC),
    ("ОбщийАкме.ВыполнитьРаскрой", COMMON),
    ("ОбщийАкме::ВыполнитьРаскрой", COMMON),
])
def test_the_full_name_finds_that_one_method(store, name, file):
    assert _files(store.find_named(name)) == [file]


def test_a_name_that_is_nowhere_finds_nothing(store):
    assert store.find_named("ВыполнитьПриход") == []
    assert store.find_named("") == []
