"""Code search and generated docs answer through the research-access rules.

The rules (src/access/resolver.py) were enforced in Q&A and MCP. `/api/search`
and `/api/docs` skipped them, so a neighbouring team restricted to "Metadata
only" — or one whose rule denies `secrets/**` — read symbol names, file paths
and links into the source through search, and the notes about denied files
through the documentation pages and their exports.

The cast (workspace A, one repository):

  * `member_a` — team "neighbours": visibility `metadata`, deny `secrets/**`;
  * `editor_a` — team "alpha-team": visibility `code`, deny `secrets/**`;
  * `viewer_a` — in no team; a rule exists for the repository, so `none`;
  * `su` — global admin, bypasses the rules;
  * and the same requests under single_tenant with NO rules at all, which
    must read exactly as they did before.

Real routers, real `current_workspace_id`, real resolver over a SQLite file;
only the graph lookup and the vector store are stood in for, because what
they return is the input under test, not the subject.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event

from tests.api.rbac_world import A_REPO, _sqlite_booleans, world

pytestmark = pytest.mark.asyncio

SECRET_FILE = "secrets/keys.py"
OPEN_FILE = "src/api.py"


@pytest.fixture
def stand_ins(monkeypatch):
    """Graph symbols and vault hits for A's repository: one in the open, one
    under the denied directory."""
    def find_symbol(name, repo_slug, limit=20, exact=False, **_kw):  # noqa: ARG001
        return [
            {"repo_slug": repo_slug, "name": "handler", "kind": "function",
             "file": OPEN_FILE, "start_line": 3, "language": "python"},
            {"repo_slug": repo_slug, "name": "load_master_key", "kind": "function",
             "file": SECRET_FILE, "start_line": 9, "language": "python"},
        ]

    monkeypatch.setattr("src.mcp_server.tools.find_symbol", find_symbol)

    def hit(note: str, path: str | None, repo: str = A_REPO):
        return SimpleNamespace(note_path=note, score=0.9, type="module",
                               module=note, repo=repo, keywords=[], path=path)

    class FakeRetriever:
        calls = 0

        def __init__(self, *_a, **_kw):
            pass

        def search(self, q, repo=None, top_k=10):  # noqa: ARG002
            FakeRetriever.calls += 1
            return [hit("modules/api.md", OPEN_FILE),
                    hit("modules/keys.md", SECRET_FILE),
                    hit("overview.md", None)]

    monkeypatch.setattr("src.retrieval.tier1_vault.VaultRetriever", FakeRetriever)
    return FakeRetriever


def _vault():
    """A's generated documentation: a note per source file plus an overview."""
    from src.config import get_settings

    root = get_settings().repo_vault_path(A_REPO)
    (root / "modules").mkdir(parents=True, exist_ok=True)
    (root / "modules" / "api.md").write_text(
        f"---\ntitle: API\npath: {OPEN_FILE}\n---\n# API\nPUBLIC_BODY\n")
    (root / "modules" / "keys.md").write_text(
        f"---\ntitle: Keys\npath: {SECRET_FILE}\n---\n# Keys\nKEY_BODY_SECRET\n")
    (root / "overview.md").write_text("---\ntitle: Overview\n---\n# Overview\nOVERVIEW_BODY\n")


async def _rules(w, tmp_path, monkeypatch):
    """The neighbour team and both rules; point the resolver at the world's DB."""
    from src.access import resolver
    from src.db.models import RepoAccessRule, Team, TeamMember

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    async with w.factory() as s:
        s.add(Team(id="team-n", name="neighbours", workspace_id=w.ws["ws-a"]))
        s.add(TeamMember(team_id="team-n", user_id=w.uid("member_a")))
        s.add(RepoAccessRule(id="rule-n", workspace_id=w.ws["ws-a"], team_id="team-n",
                             repo_slug=A_REPO, visibility="metadata",
                             deny_globs=["secrets/**"]))
        s.add(RepoAccessRule(id="rule-a", workspace_id=w.ws["ws-a"],
                             team_id=w.ids["team_a"], repo_slug=A_REPO,
                             visibility="code", deny_globs=["secrets/**"]))
        await s.commit()
    return engine


@pytest.fixture(autouse=True)
def _fresh_settings():
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _single_tenant_no_rules(tmp_path, monkeypatch, *, unruled="open"):
    """``unruled="open"`` is the operator's opt back into the pre-upgrade
    behaviour; the default is closed, which the sibling tests below assert."""
    from src.access import resolver
    from src.config import get_settings
    from src.deployment import reset_mode_cache

    if unruled:
        monkeypatch.setenv("CELMIS_UNRULED_REPO_ACCESS", unruled)
    else:
        monkeypatch.delenv("CELMIS_UNRULED_REPO_ACCESS", raising=False)
    get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    from src.db.models import Base
    Base.metadata.create_all(engine)   # empty tables: "no rules", not "no database"
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "single_tenant")
    reset_mode_cache()
    return engine


def _extra():
    from src.api.routers import docs, search

    return (search.search_router, docs.router)


async def _search(w, who: str):
    r = await w.client.get("/api/search", params={"q": "key"}, headers=w.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    return r.json()


# ─── search ──────────────────────────────────────────────────────────


async def test_metadata_only_gets_no_code_through_search(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        body = await _search(w, "member_a")
        engine.dispose()
    assert body["symbols"] == [], "metadata-only still returned code symbols"
    files = [n["path"] for n in body["notes"]]
    assert OPEN_FILE in files                 # docs are the metadata level
    assert SECRET_FILE not in files           # the deny glob still subtracts
    assert "load_master_key" not in str(body)


async def test_deny_globs_drop_symbols_and_notes_at_code_level(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        body = await _search(w, "editor_a")
        engine.dispose()
    assert [s["file"] for s in body["symbols"]] == [OPEN_FILE]
    assert SECRET_FILE not in str(body)
    assert "load_master_key" not in str(body)


async def test_a_team_without_a_rule_gets_nothing_and_costs_nothing(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        stand_ins.calls = 0
        body = await _search(w, "viewer_a")
        engine.dispose()
    assert body["symbols"] == [] and body["notes"] == []
    assert stand_ins.calls == 0, "embedded a query whose answer is empty by rule"


async def test_naming_the_repo_does_not_bypass_the_rule(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        r = await w.client.get("/api/search", params={"q": "key", "repo": A_REPO},
                               headers=w.h("viewer_a", "ws-a"))
        engine.dispose()
    assert r.status_code == 200
    assert r.json()["symbols"] == [] and r.json()["notes"] == []


async def test_a_global_admin_still_sees_everything(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        body = await _search(w, "su")
        engine.dispose()
    assert {s["file"] for s in body["symbols"]} == {OPEN_FILE, SECRET_FILE}
    assert len(body["notes"]) == 3


async def test_single_tenant_without_rules_is_closed_by_default(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = _single_tenant_no_rules(tmp_path, monkeypatch, unruled=None)
        body = await _search(w, "viewer_a")
        engine.dispose()
    assert body["symbols"] == []
    assert body["notes"] == []


async def test_single_tenant_without_rules_is_unchanged_when_opened(tmp_path, monkeypatch, stand_ins):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = _single_tenant_no_rules(tmp_path, monkeypatch)
        body = await _search(w, "viewer_a")
        engine.dispose()
    assert {s["file"] for s in body["symbols"]} == {OPEN_FILE, SECRET_FILE}
    assert len(body["notes"]) == 3


# ─── docs ────────────────────────────────────────────────────────────


async def _docs(w, who: str):
    h = w.h(who, "ws-a")
    listing = await w.client.get(f"/api/docs/{A_REPO}", headers=h)
    secret = await w.client.get(f"/api/docs/{A_REPO}/note",
                                params={"path": "modules/keys.md"}, headers=h)
    export = await w.client.get(f"/api/docs/{A_REPO}/export", headers=h)
    everything = await w.client.get("/api/docs/export-all", headers=h)
    return listing, secret, export, everything


def _zip_text(response) -> str:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return "\n".join(zf.read(n).decode() for n in zf.namelist())


async def test_metadata_only_reads_docs_but_not_the_denied_notes(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        _vault()
        listing, secret, export, everything = await _docs(w, "member_a")
        engine.dispose()
    assert listing.status_code == 200
    paths = {n["path"] for n in listing.json()["notes"]}
    assert paths == {"modules/api.md", "overview.md"}
    assert secret.status_code == 404
    assert "KEY_BODY_SECRET" not in secret.text
    assert export.status_code == 200
    assert "PUBLIC_BODY" in export.text and "KEY_BODY_SECRET" not in export.text
    assert everything.status_code == 200
    text = _zip_text(everything)
    assert "PUBLIC_BODY" in text and "KEY_BODY_SECRET" not in text


async def test_a_team_without_a_rule_is_refused_the_docs(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        _vault()
        responses = await _docs(w, "viewer_a")
        engine.dispose()
    for r in responses:
        assert r.status_code == 403, r.text
        assert "BODY" not in r.text
    assert "access rules" in responses[0].json()["detail"]


async def test_a_traversal_does_not_reach_a_denied_note_either(tmp_path, monkeypatch):
    """The concealment is decided on the note's own frontmatter after the path
    is resolved, so spelling the path differently changes nothing."""
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        _vault()
        r = await w.client.get(f"/api/docs/{A_REPO}/note",
                               params={"path": "modules/../modules/./keys.md"},
                               headers=w.h("member_a", "ws-a"))
        engine.dispose()
    assert r.status_code == 404 and "KEY_BODY_SECRET" not in r.text


async def test_full_code_team_reads_every_note_but_the_denied_one(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = await _rules(w, tmp_path, monkeypatch)
        _vault()
        listing, secret, export, _all = await _docs(w, "editor_a")
        engine.dispose()
    assert {n["path"] for n in listing.json()["notes"]} == {"modules/api.md", "overview.md"}
    assert secret.status_code == 404
    assert "KEY_BODY_SECRET" not in export.text


async def test_docs_single_tenant_without_rules_is_closed_by_default(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = _single_tenant_no_rules(tmp_path, monkeypatch, unruled=None)
        _vault()
        listing, secret, export, everything = await _docs(w, "viewer_a")
        engine.dispose()
    assert listing.status_code in (403, 404) or listing.json().get("notes") == []
    assert secret.status_code in (403, 404)
    assert "KEY_BODY_SECRET" not in export.text
    assert everything.status_code in (403, 404) or (
        "KEY_BODY_SECRET" not in _zip_text(everything))


async def test_docs_single_tenant_without_rules_is_unchanged_when_opened(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        engine = _single_tenant_no_rules(tmp_path, monkeypatch)
        _vault()
        listing, secret, export, everything = await _docs(w, "viewer_a")
        engine.dispose()
    assert {n["path"] for n in listing.json()["notes"]} == {
        "modules/api.md", "modules/keys.md", "overview.md"}
    assert secret.status_code == 200 and "KEY_BODY_SECRET" in secret.json()["body"]
    assert "KEY_BODY_SECRET" in export.text
    assert "KEY_BODY_SECRET" in _zip_text(everything)
    assert "NOT-INCLUDED.txt" not in zipfile.ZipFile(io.BytesIO(everything.content)).namelist()
