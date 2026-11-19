"""The MCP `docs` tool over a vault holding one generated note and a few cards.

What an agent must never get wrong: a draft read as a fact, a generated note
shown when a verified card replaces it, or a note whose source path the
caller's rules deny.
"""

from __future__ import annotations

import pytest

from src.kb import store
from src.mcp_server.dev_profile import tools_docs
from tests.kb.conftest import WS, card, git, make_config_repo

SLUG = "kb-repo"


@pytest.fixture
def world(kb_world, monkeypatch):
    from src.access.resolver import RepoAccessDecision, _RuleView
    from src.mcp_server.dev_profile import access, common
    from src.vault.writer import NoteMetadata, VaultWriter

    monkeypatch.setattr(store, "graph_lookup",
                        lambda repo: ({"Documents.Заказ": {"kind": "Document", "path": "x"}}.get,
                                      lambda: None))
    monkeypatch.setattr(common, "fresh_entries", lambda slugs: [])
    w = VaultWriter(workspace_id=WS)
    w.write(repo=SLUG, relative_path="modules/sales.md", content="# Продажі\n\nМодуль продажів.\n",
            metadata=NoteMetadata(type="module", repo=SLUG, path="src/sales"))
    w.write(repo=SLUG, relative_path="modules/secret.md", content="# Секрет\n\nЗакрите.\n",
            metadata=NoteMetadata(type="module", repo=SLUG, path="private/secret"))
    cfg = kb_world.repo("erp")
    make_config_repo(cfg)
    head = git(cfg, "rev-parse", "HEAD")
    store.import_cards(SLUG, [
        ("reserve.md", card(id="proc-reserve", status="verified", objects="Документ.Заказ",
                            source=f"erp@{head}", extra="replaces: [modules/sales]\ndomain: sales",
                            body="# Резерв\n\nЯк резервується товар. Див. [[proc-ship]].\n")),
        ("ship.md", card(id="proc-ship", objects="Справочник.Товары, Документ.Нема",
                         body="# Відвантаження\n\nЧернетка процесу.\n")),
    ], workspace_id=WS, code_repo="erp")

    deny = ("private/**",)
    dec = RepoAccessDecision(repo_slug=SLUG, visibility="code", open_default=False,
                             rules=(_RuleView("code", (), deny, ()),), deny_globs=deny)
    monkeypatch.setattr(access, "mcp_scope", lambda candidates=None: access.DevScope(
        decisions={SLUG: dec}, user_id="u", workspace_id=WS))
    return kb_world


def test_the_list_puts_cards_first_and_says_each_status(world):
    out = tools_docs.run()
    rows = [line for line in out.splitlines() if line.startswith(SLUG)]
    assert rows[0].startswith(f"{SLUG} kb/reserve.md") and "verified" in rows[0]
    assert "draft" in rows[1] and "kb/ship.md" in rows[1]
    assert any("modules/sales.md" in r for r in rows)
    assert "secret" not in out  # its source path is denied to this caller


def test_where_filters_by_1c_object_in_any_spelling_and_by_own_keys(world):
    for spelling in ("Документ.Заказ", "Document.Заказ", "Documents.Заказ"):
        out = tools_docs.run(where=f"object={spelling}")
        assert "kb/reserve.md" in out and "kb/ship.md" not in out, spelling
    assert "kb/reserve.md" in tools_docs.run(where="domain=sales")
    assert "kb/reserve.md" not in tools_docs.run(where="status=draft")
    assert "kb/ship.md" in tools_docs.run(where="status=чернетка")


def test_opening_a_card_by_id_shows_its_objects_links_and_back_links(world):
    out = tools_docs.run(path="proc-ship")
    assert "Catalogs.Товары" in out and "Documents.Нема" in out and "not in graph" in out
    assert "kb/reserve.md" in out  # linked from the reserve card
    assert "Чернетка процесу." in out
    assert "no note" in tools_docs.run(path="modules/secret.md")


def test_search_hides_the_generated_note_a_verified_card_replaces(world):
    out = tools_docs.run(query="продажі резерв")
    assert "matched words" not in out  # the vector index answered, not the fallback
    assert "kb/reserve.md" in out
    assert "modules/sales.md" not in out
    assert "secret" not in out


def test_changed_since_lists_the_imported_cards(world):
    out = tools_docs.run(changed_since="1d")
    assert "kb/reserve.md" in out and "kb/ship.md" in out and "created" in out
    assert "secret" not in out
    assert "changed_since" in tools_docs.run(changed_since="last tuesday")


def test_the_vector_filters_reach_the_cards_front_matter(world):
    from src.retrieval.tier1_vault import VaultRetriever

    rt = VaultRetriever(workspace_id=WS)

    def paths(**f):
        return sorted(h.note_path for h in rt.search("процес", repos=[SLUG], kb_filters=f))

    assert paths(card="1") == ["kb/reserve.md", "kb/ship.md"]
    assert paths(object="Документ.Заказ") == ["kb/reserve.md"]
    assert paths(status="draft") == ["kb/ship.md"]
    assert paths(domain="sales") == ["kb/reserve.md"]
    assert paths(id="proc-ship") == ["kb/ship.md"]
