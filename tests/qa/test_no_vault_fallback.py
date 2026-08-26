"""Tier 3 must produce code when there is no vault yet.

Production bug this covers: three indexed repos, no vault generated, question
asked in prose ("що це за сервіси") — `hits` empty, no identifiers to grep for,
so the bundle went out as "(no code read)" and the model answered "ви не надали
вихідний код", blaming the user for an attachment they never had to send.

Everything external is faked (embed, Qdrant, graph, access resolution); the
file selection, permission gating and prompt assembly under test are real.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.access.resolver import RepoAccessDecision, _RuleView
from src.config import Settings
from src.qa import multi_repo_retriever as mrr
from src.qa.multi_repo_retriever import MultiRepoRetriever

ETL = "acme-etl"
API = "acme-api"
PROSE_Q = "що це за сервіси та що їх об'єднує"


# ─── fixtures ────────────────────────────────────────────────────────


def _make_repo(root: Path, slug: str) -> Path:
    """A repo shaped like a real service: entry points at the root, code in
    src/, plus noise the fallback is expected to leave alone."""
    repo = root / "repos" / slug
    (repo / "src" / "services").mkdir(parents=True)
    (repo / "node_modules" / "junk").mkdir(parents=True)
    (repo / "tests").mkdir()

    (repo / "README.md").write_text(f"# {slug}\n\nIngests orders.\n")
    (repo / "docker-compose.yml").write_text("services:\n  api:\n    image: x\n")
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{slug}"\n')
    (repo / "src" / "main.py").write_text("def main():\n    return 'run'\n")
    (repo / "src" / "main.css").write_text("body { color: red }\n")  # not source
    (repo / "src" / "services" / "orders.py").write_text(
        "class OrderService:\n    def place(self):\n        return 1\n"
    )
    (repo / "src" / "credentials.py").write_text("TOKEN = 'sekrit'\n")
    (repo / "tests" / "test_orders.py").write_text("def test_x():\n    pass\n")
    (repo / "node_modules" / "junk" / "index.js").write_text("module.exports = 1\n")
    return repo


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace_dir=tmp_path)


@pytest.fixture()
def repos(settings: Settings, tmp_path: Path) -> list[str]:
    for slug in (ETL, API):
        _make_repo(tmp_path, slug)
    return [ETL, API]


class _DeadQdrant:
    """Qdrant with no collection — what a deployment looks like before anyone
    clicks "Generate vault"."""

    def query_points(self, **_kw):
        raise RuntimeError("Collection `celmis` doesn't exist!")


# Every `retrieve()` below states a tenant, because the signature no longer
# lets it not to — one collection holds every workspace's notes.
WS = "ws-test"


def _retriever(settings: Settings, qdrant=None) -> MultiRepoRetriever:
    # `vault_ret` is only read for its `.qdrant` handle — passing a stub keeps
    # the constructor off the network.
    return MultiRepoRetriever(
        settings=settings,
        vault_ret=SimpleNamespace(qdrant=qdrant or _DeadQdrant()),
    )


@pytest.fixture(autouse=True)
def _no_external_calls(monkeypatch, settings: Settings):
    """Embedding + access resolution are the only other things `retrieve()`
    reaches for. Access defaults to fully open; individual tests override."""
    monkeypatch.setattr(
        "src.llm.completion.embed", lambda *a, **k: [0.01] * 768,
    )
    monkeypatch.setattr(
        mrr, "resolve_access",
        lambda **kw: {r: RepoAccessDecision.full(r) for r in kw["repos"]},
    )
    # No graph on disk in most tests: the real tools return empty for a repo
    # that was never indexed, so let them.
    monkeypatch.setattr("src.config.get_settings", lambda: settings)


def _fake_graph(monkeypatch, *, hubs: dict[str, list[str]] | None = None,
                symbols: dict[str, list[dict]] | None = None) -> None:
    """Stand in for an indexed FalkorDB graph."""
    import src.mcp_server.tools as tools

    def query_graph(_cypher, repo_slug=None, params=None, **_kw):
        rows = [{"file": f, "n": 10 - i}
                for i, f in enumerate((hubs or {}).get(repo_slug, []))]
        return {"ok": True, "rows": rows, "row_count": len(rows)}

    def find_symbol(name, repo_slug, limit=20, **_kw):
        return (symbols or {}).get(f"{repo_slug}:{name}", [])

    monkeypatch.setattr(tools, "query_graph", query_graph)
    monkeypatch.setattr(tools, "find_symbol", find_symbol)
    monkeypatch.setattr(tools, "find_callers",
                        lambda **kw: {"callers": []})


# ─── the bug ─────────────────────────────────────────────────────────


async def test_no_vault_prose_question_still_reads_code(
    settings, repos, monkeypatch,
):
    """The production case: no vault, no identifiers in the question."""
    _fake_graph(monkeypatch, hubs={ETL: ["src/services/orders.py"]})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=repos, is_admin=True,
    )

    assert ctx.vault_unavailable, "precondition: Tier 1 is down"
    assert not ctx.vault_hits
    assert ctx.files_read, "no vault must not mean no code"
    assert ctx.code_fallback_used
    assert "(no code read)" not in ctx.prompt
    # Orientation material, not an arbitrary file.
    assert f"{ETL}/README.md" in ctx.files_read
    assert f"{ETL}/docker-compose.yml" in ctx.files_read
    # Every repo gets a share — the question is about all of them.
    assert any(f.startswith(f"{API}/") for f in ctx.files_read)
    # The code actually lands in the prompt, not just in the metadata.
    assert "Ingests orders." in ctx.prompt


async def test_fallback_skips_noise_and_non_source_entry_points(
    settings, repos, monkeypatch,
):
    _fake_graph(monkeypatch, hubs={
        ETL: ["tests/test_orders.py", "node_modules/junk/index.js",
              "src/services/orders.py"],
    })

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=True,
    )

    assert f"{ETL}/src/services/orders.py" in ctx.files_read
    assert f"{ETL}/tests/test_orders.py" not in ctx.files_read
    assert f"{ETL}/node_modules/junk/index.js" not in ctx.files_read
    # `main.*` glob matches main.css too; only source may come through.
    assert f"{ETL}/src/main.css" not in ctx.files_read
    assert f"{ETL}/src/main.py" in ctx.files_read


async def test_fallback_uses_graph_when_grep_cannot_see_the_language(
    settings, repos, monkeypatch,
):
    """A named symbol whose file grep's --include list does not cover still
    resolves, because the graph is asked by name too."""
    _fake_graph(monkeypatch, symbols={
        f"{ETL}:OrderService": [{"file": "src/services/orders.py"}],
    })

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question="де живе OrderService", repos=[ETL], is_admin=True,
    )

    assert f"{ETL}/src/services/orders.py" in ctx.files_read


async def test_fallback_is_bounded(settings, tmp_path, monkeypatch):
    """An unguided pick has no relevance signal to stop it — the caps must."""
    slugs = [f"repo-{i}" for i in range(6)]
    for slug in slugs:
        repo = _make_repo(tmp_path, slug)
        for i in range(40):
            (repo / "src" / f"mod{i}.py").write_text(f"X = {i}\n" * 200)
    _fake_graph(monkeypatch, hubs={
        s: [f"src/mod{i}.py" for i in range(40)] for s in slugs
    })

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=slugs, is_admin=True,
    )

    assert len(ctx.files_read) <= mrr._FALLBACK_MAX_FILES
    per_repo = {s: sum(f.startswith(s + "/") for f in ctx.files_read)
                for s in slugs}
    assert max(per_repo.values()) <= mrr._FALLBACK_MAX_FILES_PER_REPO
    # Round-robin: the budget must not disappear into repo #1.
    assert sum(1 for n in per_repo.values() if n) >= 4


# ─── permissions still bind on the new path ──────────────────────────


def _deny_decision(slug: str, deny: tuple[str, ...]) -> RepoAccessDecision:
    return RepoAccessDecision(
        repo_slug=slug, visibility="code", open_default=False,
        rules=(_RuleView("code", (), deny, ("secrets",)),),
        deny_globs=deny, sensitivity_tags=("secrets",),
    )


async def test_fallback_respects_deny_globs(settings, repos, monkeypatch):
    monkeypatch.setattr(
        mrr, "resolve_access",
        lambda **kw: {r: _deny_decision(r, ("**/credentials*", "README.md"))
                      for r in kw["repos"]},
    )
    _fake_graph(monkeypatch, hubs={ETL: ["src/credentials.py"]})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=False,
    )

    assert f"{ETL}/src/credentials.py" not in ctx.files_read
    assert f"{ETL}/README.md" not in ctx.files_read
    assert "sekrit" not in ctx.prompt
    # Hidden, not silently dropped — the user is told policy removed something.
    assert f"{ETL}/src/credentials.py" in ctx.hidden_files
    assert "hidden by the access policy" in ctx.access_notice
    # …and the rest of the repo is still readable.
    assert f"{ETL}/src/main.py" in ctx.files_read


async def test_fallback_reads_nothing_from_metadata_only_repo(
    settings, repos, monkeypatch,
):
    """`metadata` visibility means docs only — the fallback must not become a
    way to read source the caller cannot see."""
    meta = RepoAccessDecision(
        repo_slug=ETL, visibility="metadata", open_default=False,
        rules=(_RuleView("metadata", (), (), ()),),
    )
    monkeypatch.setattr(
        mrr, "resolve_access", lambda **kw: {r: meta for r in kw["repos"]},
    )
    _fake_graph(monkeypatch, hubs={ETL: ["src/services/orders.py"]})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=False,
    )

    assert ctx.files_read == []
    assert "Ingests orders." not in ctx.prompt


# ─── the vault path is untouched ─────────────────────────────────────


class _LiveQdrant:
    """One module hit, exactly as Tier 1 returns it."""

    def __init__(self, repo: str, path: str) -> None:
        self._repo, self._path = repo, path

    def query_points(self, **kw):
        if kw.get("query_filter") is None:
            return SimpleNamespace(points=[])
        return SimpleNamespace(points=[SimpleNamespace(score=0.9, payload={
            "note_path": "modules/services.md", "type": "module",
            "module": "services", "repo": self._repo, "symbols": [],
            "keywords": [], "content": "orders module", "path": self._path,
            "cross_refs": [],
        })])


async def test_vault_hits_selection_unchanged(settings, repos, monkeypatch):
    """With a vault present the fallback must not fire — same files, same
    cost as before the fix."""
    calls: list[str] = []
    monkeypatch.setattr(
        MultiRepoRetriever, "_fallback_files",
        lambda self, *a, **k: calls.append("called") or [],
    )
    qdrant = _LiveQdrant(ETL, "src/services")

    ctx = await _retriever(settings, qdrant).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=True,
    )

    assert calls == [], "fallback must stay dormant when Tier 1 answered"
    assert ctx.code_fallback_used is False
    assert ctx.files_read == [f"{ETL}/src/services/orders.py"]
    assert f"{ETL}/README.md" not in ctx.files_read
    assert ctx.vault_unavailable is None


async def test_code_toggle_off_still_wins(settings, repos, monkeypatch):
    _fake_graph(monkeypatch, hubs={ETL: ["src/services/orders.py"]})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=True, include_code=False,
    )

    assert ctx.files_read == []
    assert "turned off by the user" in ctx.prompt
    assert "Generate vault" not in ctx.prompt  # user chose this; not a fault


# ─── nothing readable → say what fixes it ────────────────────────────


async def test_nothing_readable_names_the_fix(settings, tmp_path, monkeypatch):
    """Empty repo, no vault, no graph: the prompt must forbid blaming the user
    and must name the action that resolves it."""
    (tmp_path / "repos" / "empty-repo").mkdir(parents=True)
    _fake_graph(monkeypatch, hubs={})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=["empty-repo"], is_admin=True,
    )

    assert ctx.files_read == []
    assert "Generate vault" in ctx.prompt
    assert "«Repositories»" in ctx.prompt
    # The notice must forbid blaming the user for not attaching code: the
    # repositories ARE connected and the code IS on the server, so "you did
    # not provide the source" is both wrong and unactionable.
    assert "Do NOT write «you did not provide the" in ctx.prompt
    assert "generate the vault" in ctx.prompt.lower()
    # The old bare marker must not survive anywhere in the prompt.
    assert "(no code read)" not in ctx.prompt


async def test_no_code_notice_absent_when_code_was_read(
    settings, repos, monkeypatch,
):
    _fake_graph(monkeypatch, hubs={ETL: ["src/services/orders.py"]})

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=True,
    )

    assert ctx.files_read
    assert "Context is incomplete" not in ctx.prompt


async def test_denied_repos_short_circuit_unchanged(settings, repos, monkeypatch):
    monkeypatch.setattr(
        mrr, "resolve_access",
        lambda **kw: {r: RepoAccessDecision.denied(r) for r in kw["repos"]},
    )

    ctx = await _retriever(settings).retrieve(
        workspace_id=WS,
        question=PROSE_Q, repos=[ETL], is_admin=False,
    )

    assert ctx.files_read == []
    assert ctx.blocked_repos == [ETL]
    assert "(no access)" in ctx.prompt
    assert "Generate vault" not in ctx.prompt  # access, not a setup gap


# ─── unit-level checks on the selection helpers ──────────────────────


def test_entry_points_prefer_docs_then_manifests(settings, tmp_path, repos):
    picks = _retriever(settings)._entry_point_files(tmp_path / "repos" / ETL)

    assert picks[0] == "README.md"
    assert "docker-compose.yml" in picks
    assert picks.index("docker-compose.yml") < picks.index("src/main.py")
    assert len(picks) <= mrr._ENTRY_ROOT_LIMIT + mrr._ENTRY_MODULE_LIMIT


def test_entry_points_never_include_dotenv(settings, tmp_path, repos):
    repo = tmp_path / "repos" / ETL
    (repo / ".env").write_text("SECRET=1\n")
    (repo / ".env.example").write_text("SECRET=\n")

    picks = _retriever(settings)._entry_point_files(repo)

    assert not any(p.startswith(".env") for p in picks)


@pytest.mark.parametrize("rel,noise", [
    ("src/app.py", False),
    ("src/tests/helpers.py", True),
    ("node_modules/x/index.js", True),
    ("pkg/handler_test.go", True),
    ("src/foo.spec.ts", True),
    ("src/foo.ts", False),
])
def test_noise_paths(rel, noise):
    assert MultiRepoRetriever._is_noise_path(rel) is noise


def test_graph_hub_files_degrade_when_graph_missing(settings, monkeypatch):
    import src.mcp_server.tools as tools
    monkeypatch.setattr(
        tools, "query_graph",
        lambda *a, **k: {"ok": False, "error": "Graph not found", "rows": []},
    )
    assert _retriever(settings)._graph_hub_files(tools, ETL) == []


def test_read_one_rejects_traversal_out_of_repo(settings, tmp_path, repos):
    """`priority_files` are strings from grep/graph output — a path escaping
    the repo must not be readable through them."""
    secret = tmp_path / "outside.txt"
    secret.write_text("nope\n")

    files, bundle, _hidden = _retriever(settings)._read_code_for_hits(
        [], priority_files=[f"{ETL}/../../outside.txt"],
        access={ETL: RepoAccessDecision.full(ETL)},
    )

    assert files == []
    assert "nope" not in bundle


def test_manifest_content_survives_into_bundle(settings, tmp_path, repos):
    """Manifests carry no source extension — the reader must not filter them
    out, since they are half the answer to "what are these services"."""
    files, bundle, _hidden = _retriever(settings)._read_code_for_hits(
        [], overview_files=[f"{ETL}/pyproject.toml"],
        access={ETL: RepoAccessDecision.full(ETL)},
    )

    assert files == [f"{ETL}/pyproject.toml"]
    assert f'name = "{ETL}"' in bundle


def test_the_vault_retriever_is_not_handed_a_model_client():
    """A type confusion that made `celmis ask` crash on every vault search.

    `VaultRetriever(settings, qdrant, workspace_id)` — and the orchestrator
    called `VaultRetriever(self.settings, self.gemini)`, putting a GeminiClient
    into the `qdrant` slot. It is truthy, so it won through
    `qdrant or _build_qdrant_client(...)`, and the next line to run was
    `self.qdrant.query_points(...)` on an object that has no such method.

    Reproduced before the fix: AttributeError, unwrapped, straight up the
    stack. The web Q&A path was unaffected — it goes through
    MultiRepoRetriever — so this only ever broke the CLI, which is why it
    survived: nothing in the API exercised it.

    Positional arguments are how this happened, so the guard is about the call
    shape and not the value.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "qa" / "orchestrator.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "VaultRetriever":
            assert len(node.args) <= 1, (
                "VaultRetriever is being given a second positional argument; "
                "that slot is `qdrant`, and the last thing put there was a "
                "model client"
            )
