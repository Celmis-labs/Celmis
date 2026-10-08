"""A project's include/exclude patterns, and Q&A over code with no graph.

Two things share this file because they meet in the retriever:

  * the file scope of a project repo (`ProjectRepo.include_globs` /
    `exclude_globs`): exclude wins, an empty include means every file, and a
    file outside the scope is absent from every tier — the prompt, the files
    read, the "hidden files" notice;
  * an uploaded repository in a language the graph has no extractor for
    (here: `.bsl`, with Cyrillic names and identifiers) is still found
    by the question's own words.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from src.access.file_scope import (
    FileScope,
    apply_scopes,
    normalize_globs,
    scope_of,
    scopes_for_links,
)
from src.access.resolver import RepoAccessDecision
from src.qa import text_search

SLUG = "legacy-code"


# ─── the scope itself ────────────────────────────────────────────────


def test_an_empty_scope_allows_everything():
    s = FileScope()
    assert s.is_empty and s.allows("any/where.txt")


def test_include_is_exhaustive_when_present():
    s = scope_of(["src/**", "docs"], [])
    assert s.allows("src/a/b.py") and s.allows("docs/readme.md") and s.allows("docs")
    assert not s.allows("tests/a.py") and not s.allows("README.md")


def test_exclude_wins_over_include():
    s = scope_of(["src/**"], ["src/generated/**", "**/*.min.js"])
    assert s.allows("src/app.py")
    assert not s.allows("src/generated/x.py") and not s.allows("src/static/app.min.js")


def test_exclude_alone_removes_only_what_it_names():
    s = scope_of([], ["secrets", "*.log"])
    assert s.allows("src/a.py") and not s.allows("secrets/key") and not s.allows("run.log")


def test_a_plain_prefix_pattern_covers_the_folder():
    assert not scope_of([], ["vendor"]).allows("vendor/lib/a.py")
    assert scope_of([], ["vendor"]).allows("vendors/a.py")


def test_unicode_paths_match():
    s = scope_of(["Конфігурація/**"], ["**/Тести/**"])
    assert s.allows("Конфігурація/Модуль.bsl") and not s.allows("Конфігурація/Тести/а.bsl")


def test_normalize_globs_trims_dedupes_and_caps():
    assert normalize_globs([" a/** ", "a/**", "", "b\\c"]) == ["a/**", "b/c"]
    with pytest.raises(ValueError):
        normalize_globs(["x"] * 51 + [str(i) for i in range(60)])
    with pytest.raises(ValueError):
        normalize_globs(["y" * 201])


def test_scopes_for_links_leaves_unnarrowed_repos_out():
    links = [types.SimpleNamespace(repo_slug="a", include_globs=["src"], exclude_globs=[]),
             types.SimpleNamespace(repo_slug="b", include_globs=[], exclude_globs=[])]
    assert set(scopes_for_links(links)) == {"a"}


def test_the_scope_only_narrows_a_research_decision():
    full = RepoAccessDecision.full("r")
    narrowed = apply_scopes({"r": full}, {"r": scope_of(["src/**"], ["src/x/**"])})["r"]
    assert narrowed.path_visible("src/a.py") and not narrowed.path_visible("src/x/a.py")
    assert not narrowed.path_visible("docs/a.md")
    # the notes tier asks path_denied: out of scope counts as concealed
    assert narrowed.path_denied("docs/a.md") and not narrowed.path_denied("src/a.py")
    assert full.path_visible("docs/a.md"), "the original decision is untouched"
    denied = apply_scopes({"r": RepoAccessDecision.denied("r")}, {"r": scope_of(["src"], [])})["r"]
    assert not denied.researchable


# ─── text search over code the graph cannot read ─────────────────────


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    files = {
        "Документи/РеалізаціяТоварів.bsl": "Процедура ПровестиДокумент(Відмова)\n  ЗаписатиЖурнал();\nКонецПроцедури\n",
        "Довідники/Контрагенти.bsl": "Функція ОтриматиКонтрагента(Код)\nКонецФункції\n",
        "config/secrets/Ключ.bsl": "Пароль = \"x\"; // ПровестиДокумент\n",
        "Помилка.xml": "<Файл><Поле>ПровестиДокумент</Поле></Файл>",
        "img/logo.png": "ПровестиДокумент\x00\x01binary",
        "blob.bin": "ПровестиДокумент",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    (root / "legacy1251.bsl").write_bytes("Процедура СтараПроцедура()".encode("cp1251"))
    return root


def test_the_question_s_own_words_find_files_the_graph_cannot_index(tree):
    hits, complete = text_search.search_text(tree, "ПровестиДокумент")
    paths = {h.path for h in hits}
    assert complete
    assert "Документи/РеалізаціяТоварів.bsl" in paths and "Помилка.xml" in paths
    assert not any(p.endswith((".png", ".bin")) for p in paths), "binaries are skipped"
    first = next(h for h in hits if h.path == "Документи/РеалізаціяТоварів.bsl")
    assert first.line == 1 and "ПровестиДокумент" in first.snippet


def test_matching_ignores_case_and_a_legacy_codepage_is_read(tree):
    assert text_search.search_text(tree, "провестидокумент")[0]
    hits, _ = text_search.search_text(tree, "СтараПроцедура")
    assert [h.path for h in hits] == ["legacy1251.bsl"]


def test_a_file_name_match_alone_is_a_hit(tree):
    hits, _ = text_search.search_text(tree, "Контрагенти")
    assert "Довідники/Контрагенти.bsl" in {h.path for h in hits}


def test_the_scope_removes_files_from_search_and_ranking(tree):
    scope = scope_of([], ["config/**"])
    hits, _ = text_search.search_text(tree, "ПровестиДокумент", scope=scope)
    assert "config/secrets/Ключ.bsl" not in {h.path for h in hits}
    with_scope = text_search.rank_files(tree, "ПровестиДокумент", scope=scope)
    assert "config/secrets/Ключ.bsl" not in with_scope
    assert "config/secrets/Ключ.bsl" in text_search.rank_files(tree, "ПровестиДокумент")


def test_the_time_budget_and_the_limits_bound_a_search(tree):
    hits, complete = text_search.search_text(tree, "ПровестиДокумент", limit=1, time_budget=0.0)
    assert complete is False and len(hits) <= 1
    assert len(text_search.search_text(tree, "ПровестиДокумент", limit=2)[0]) <= 2


# ─── the retriever: scope + text tier together ───────────────────────


@pytest.fixture
def retriever(tree, tmp_path, monkeypatch):
    from src.api import auto_review as ar
    from src.qa import multi_repo_retriever as mrr

    repos_root = tmp_path / "ws" / "repos"
    repos_root.mkdir(parents=True)
    (repos_root / SLUG).symlink_to(tree)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    from src.config import get_settings

    get_settings.cache_clear()
    store = ar.AutoReviewStore(tmp_path / "ar.db")
    store.upsert(ar.RepoConfig(user_id="u", repo_slug=SLUG, provider="upload",
                               full_name=f"upload/{SLUG}", url=f"upload:{SLUG}",
                               workspace_id="w1"))
    monkeypatch.setattr(ar, "_default_store", store)
    monkeypatch.setattr(mrr, "resolve_access",
                        lambda **kw: {r: RepoAccessDecision.full(r) for r in kw["repos"]})
    monkeypatch.setattr(mrr.MultiRepoRetriever, "_collection_exists", lambda self: False)
    r = mrr.MultiRepoRetriever(settings=get_settings(),
                               vault_ret=types.SimpleNamespace(qdrant=None))
    yield r
    get_settings.cache_clear()


async def _ask(r, question, **kw):
    return await r.retrieve(question=question, repos=[SLUG], workspace_id="w1", **kw)


async def test_an_uploaded_repo_without_a_graph_is_answered_from_its_files(retriever):
    ctx = await _ask(retriever, "Де проводиться ПровестиДокумент?")
    assert f"{SLUG}/Документи/РеалізаціяТоварів.bsl" in ctx.files_read
    assert "ЗаписатиЖурнал" in ctx.prompt


async def test_excluded_files_are_not_read_and_are_not_named_as_hidden(retriever):
    scopes = {SLUG: scope_of([], ["config/**"])}
    ctx = await _ask(retriever, "ПровестиДокумент", file_scopes=scopes)
    assert not any("config/" in f for f in ctx.files_read)
    assert "Пароль" not in ctx.prompt
    assert not any("config/" in f for f in ctx.hidden_files)


async def test_an_include_list_restricts_reading_to_it(retriever):
    scopes = {SLUG: scope_of(["Довідники/**"], [])}
    ctx = await _ask(retriever, "ПровестиДокумент ОтриматиКонтрагента", file_scopes=scopes)
    assert ctx.files_read and all("/Довідники/" in f for f in ctx.files_read)


async def test_exclude_wins_inside_the_include(retriever):
    scopes = {SLUG: scope_of(["Документи/**", "Довідники/**"], ["Документи/**"])}
    ctx = await _ask(retriever, "ПровестиДокумент ОтриматиКонтрагента", file_scopes=scopes)
    assert all("/Документи/" not in f for f in ctx.files_read)
    assert any("Довідники" in f for f in ctx.files_read)
