"""An import end to end: cards on disk, points in Qdrant, the change log, and
a configuration repository that moves on under them."""

from __future__ import annotations

import io
import zipfile

import frontmatter
import pytest

from src.kb import changes, store
from tests.kb.conftest import WS, card, git, make_config_repo

GRAPH = {"Documents.Заказ": {"kind": "Document", "path": "Documents/Заказ.xml"},
         "Catalogs.Товары": {"kind": "Catalog", "path": "Catalogs/Товары.xml"}}


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(store, "graph_lookup", lambda repo: (GRAPH.get, lambda: None))


def _import(files, **kw):
    return store.import_cards("kb-repo", files, workspace_id=WS, **kw)


def test_cards_land_under_kb_with_their_front_matter_and_reading(kb_world, graph):
    first, _ = make_config_repo(kb_world.repo("erp"))
    rep = _import([
        ("sales/reserve.md", card(id="proc-reserve", status="verified",
                                  objects="Документ.Заказ, Справочник.Товары, Документ.Нема",
                                  source=f"erp@{first[:9]}", extra="owner: ops")),
        ("sales/ship.md", card(id="proc-ship", objects="Справочник.Товары")),
    ], code_repo="erp")
    assert sorted(rep.created) == ["kb/sales/reserve.md", "kb/sales/ship.md"]
    assert rep.missing_objects == [{"path": "kb/sales/reserve.md", "objects": ["Documents.Нема"]}]
    assert rep.stale == ["kb/sales/reserve.md"]

    post = frontmatter.load(kb_world.vault("kb-repo") / "kb" / "sales" / "reserve.md")
    assert post["fm"]["owner"] == "ops" and post["type"] == "process"
    kb = post["kb"]
    assert kb["objects"] == ["Documents.Заказ", "Catalogs.Товары", "Documents.Нема"]
    assert kb["objects_missing"] == ["Documents.Нема"]
    assert kb["status"] == "verified" and kb["effective_status"] == "needs-review"
    assert kb["staleness"]["changed"] == ["Documents.Заказ"]

    payloads = {p["note_path"]: p for p in kb_world.payloads()}
    assert set(payloads) == {"kb/sales/reserve.md", "kb/sales/ship.md"}
    assert payloads["kb/sales/reserve.md"]["kb"]["effective_status"] == "needs-review"
    assert payloads["kb/sales/reserve.md"]["fm"]["owner"] == "ops"
    assert all(p["workspace_id"] == WS for p in payloads.values())


def test_the_same_import_again_rewrites_nothing_and_a_snapshot_removes_what_it_lost(kb_world, graph):
    a, b = card(id="a"), card(id="b")
    _import([("a.md", a), ("b.md", b)])
    log_before = len(changes.read(store.vault_dir("kb-repo")))
    again = _import([("a.md", a), ("b.md", b)])
    assert again.unchanged == 2 and not again.created and not again.updated
    assert len(changes.read(store.vault_dir("kb-repo"))) == log_before

    third = _import([("a.md", card(id="a", status="verified"))])
    assert third.updated == ["kb/a.md"] and third.deleted == ["kb/b.md"]
    assert not (kb_world.vault("kb-repo") / "kb" / "b.md").exists()
    assert {p["note_path"] for p in kb_world.payloads()} == {"kb/a.md"}
    kinds = {e["path"]: e["change"] for e in changes.read(store.vault_dir("kb-repo"))}
    assert kinds == {"kb/a.md": "updated", "kb/b.md": "deleted"}


def test_a_broken_card_and_a_second_card_with_the_same_id_are_skipped_not_fatal(kb_world, graph):
    _import([("keep.md", card(id="keep"))])
    rep = _import([("keep.md", "---\nid: [unclosed\n---\n"), ("one.md", card(id="dup")),
                   ("two.md", card(id="dup"))])
    assert [s["path"] for s in rep.skipped] == ["kb/keep.md", "kb/two.md"]
    assert rep.created == ["kb/one.md"]
    # a card that failed to parse this time is kept, not deleted by the snapshot
    assert (kb_world.vault("kb-repo") / "kb" / "keep.md").exists() and not rep.deleted


def test_a_card_cannot_take_a_generator_type(kb_world, graph):
    _import([("m.md", card(id="m", type="module"))])
    assert frontmatter.load(kb_world.vault("kb-repo") / "kb" / "m.md")["type"] == "kb-module"


def test_indexing_the_configuration_marks_a_card_stale_and_logs_it(kb_world, graph):
    cfg = kb_world.repo("erp")
    make_config_repo(cfg)
    head = git(cfg, "rev-parse", "HEAD")
    _import([("r.md", card(id="r", status="verified", objects="Документ.Заказ", source=f"erp@{head}"))],
            code_repo="erp")
    assert frontmatter.load(kb_world.vault("kb-repo") / "kb" / "r.md")["kb"]["effective_status"] == "verified"

    (cfg / "Documents" / "Заказ.xml").write_text("<x changed='1'/>", encoding="utf-8")
    git(cfg, "commit", "-q", "-am", "three")
    store.after_index("erp")

    kb = frontmatter.load(kb_world.vault("kb-repo") / "kb" / "r.md")["kb"]
    assert kb["effective_status"] == "needs-review" and kb["staleness"]["changed"] == ["Documents.Заказ"]
    log = changes.read(store.vault_dir("kb-repo"))
    assert log[0]["path"] == "kb/r.md" and log[0]["change"] == "stale"


def test_a_followed_repository_is_imported_again_after_its_index(kb_world, graph):
    root = kb_world.repo("kb-repo")
    (root / "docs" / "kb").mkdir(parents=True)
    (root / "docs" / "kb" / "one.md").write_text(card(id="one"), encoding="utf-8")
    (root / "README.md").write_text("# not a card\n", encoding="utf-8")
    rep = store.import_from_checkout("kb-repo", workspace_id=WS, prefix="docs/kb", follow=True)
    assert rep.created == ["kb/one.md"]

    (root / "docs" / "kb" / "two.md").write_text(card(id="two"), encoding="utf-8")
    store.after_index("kb-repo")
    assert (kb_world.vault("kb-repo") / "kb" / "two.md").exists()
    assert store.load_binding("kb-repo")["by"] == "sync"


def test_a_binding_never_reaches_into_another_workspaces_configuration(kb_world, graph, monkeypatch):
    make_config_repo(kb_world.repo("erp"))
    _import([("r.md", card(id="r", objects="Документ.Заказ"))], code_repo="erp")
    calls = []
    monkeypatch.setattr(store, "recheck", lambda slug, **kw: calls.append(slug))
    monkeypatch.setattr("src.api.auto_review.workspace_for_repo_slug",
                        lambda slug: "ws-other" if slug == "erp" else WS)
    store.after_index("erp")
    assert calls == []


@pytest.mark.parametrize("name", ["../etc/passwd.md", "a/../../b.md", ".git/x.md", "__MACOSX/a.md",
                                  "/abs.md", "pic.png", "C:\\x.md", "secrets/db.md", ".env.md"])
def test_a_path_that_escapes_or_is_not_markdown_is_refused(name):
    assert store.safe_rel(name) is None


def test_an_upload_reads_zips_skips_links_and_odd_files(kb_world):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("kb/a.md", card(id="a"))
        z.writestr("kb/../../evil.md", "x")
        z.writestr("kb/pic.png", b"\x89PNG")
        link = zipfile.ZipInfo("kb/link.md")
        link.external_attr = (0o120777 << 16)
        z.writestr(link, "/etc/passwd")
    rep = store.ImportReport(repo="kb-repo")
    files = store.read_upload([("cards.zip", buf.getvalue()), ("b.markdown", card(id="b").encode())], rep)
    assert sorted(p for p, _ in files) == ["b.md", "kb/a.md"]
    assert {s["path"] for s in rep.skipped} >= {"kb/link.md"}
