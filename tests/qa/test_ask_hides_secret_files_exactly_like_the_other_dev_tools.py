"""`ask` and the web chat never put a secret file in front of the model.

The dev tools (`find`, `outline`, `read_symbol`, `grep`, `map`) refuse a secret
file (`.env`, keys, credential stores) whoever asks: a floor under the access
rules, so an owner is refused too and a repository with no deny glob is not an
open door. The Q&A retriever behind `ask` did not have that floor: its gate
asked only the deny globs and the visibility, so a `.env` in a repository
without a rule could be read into the LLM prompt, where a prompt injection or
the provider's logs could carry it out. The model sees the prompt, so this is
checked at the prompt, with a stubbed LLM that records what it was sent.

Every tier that can turn a path into prompt text is covered: the identifier
grep, the vault notes (and the module walk behind them), the graph's symbols,
the no-vault fallback pick, the plain-text tier of uploaded repositories, and a
symlink or a data file that only the CONTENT gives away.
"""

from __future__ import annotations

import inspect
import types
from pathlib import Path

import pytest

from src.access.resolver import RepoAccessDecision
from src.config import Settings
from src.qa import multi_repo_retriever as mrr
from src.qa.multi_repo_retriever import MultiRepoRetriever

SLUG = "acme-api"
UPLOAD = "acme-legacy"
WS = "ws-test"

NORMAL = "NORMAL_MARKER_41"
IN_ENV = "ENV_MARKER_7f3a"
IN_CRED = "CRED_MARKER_9b21"
IN_DIR = "DIR_MARKER_55e0"
IN_NOTE = "NOTE_MARKER_c8d4"
IN_LINK = "LINK_MARKER_0a6e"
IN_SNIFF = "SNIFF_MARKER_d17b"
SECRETS = (IN_ENV, IN_CRED, IN_DIR, IN_NOTE, IN_LINK, IN_SNIFF)

QUESTION = "where is place_order defined and what does it return?"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _git_style_repo(root: Path) -> Path:
    repo = root / "repos" / SLUG
    _write(repo, {
        "README.md": "# acme-api\n\nOrders.\n",
        "src/orders.py": f"def place_order():\n    return '{NORMAL}'\n",
        # Each secret mentions the identifier, so the grep tier finds it.
        ".env": f"# place_order\nAPI_KEY={IN_ENV}\n",
        "config/credentials.yml": f"# place_order\ntoken: {IN_CRED}\n",
        "config/secrets/db.py": f"# place_order\nPASSWORD = '{IN_DIR}'\n",
    })
    # A harmless-looking name that points at the .env: only the target gives it away.
    (repo / "src" / "notes.py").symlink_to(repo / ".env")
    return repo


class _Qdrant:
    """A vector store with one module note, one note about a secret file."""

    def query_points(self, **_kw):
        def point(payload):
            return types.SimpleNamespace(score=0.99, payload=payload)

        return types.SimpleNamespace(points=[
            point({"note_path": "modules/src.md", "type": "module", "repo": SLUG,
                   "path": "src", "module": "src", "content": "the orders module",
                   "symbols": [], "keywords": [], "cross_refs": []}),
            point({"note_path": "modules/env.md", "type": "module", "repo": SLUG,
                   "path": ".env", "module": "env", "content": f"secrets: {IN_NOTE}",
                   "symbols": [], "keywords": [], "cross_refs": []}),
            point({"note_path": "modules/cfg.md", "type": "module", "repo": SLUG,
                   "path": "config/secrets", "module": "cfg", "content": f"db: {IN_NOTE}",
                   "symbols": [], "keywords": [], "cross_refs": []}),
        ])


def _fake_graph(monkeypatch, secret_files: list[str]) -> None:
    """A graph that points at the secret files from every direction: as the
    densest files, as a definition, and as a caller."""
    import src.mcp_server.tools as tools

    def query_graph(_cypher, repo_slug=None, params=None, **_kw):
        rows = [{"file": f, "n": 99} for f in secret_files] + [{"file": "src/orders.py", "n": 1}]
        return {"ok": True, "rows": rows, "row_count": len(rows)}

    def find_symbol(name, repo_slug, limit=20, **_kw):
        if name != "place_order":
            return []
        return [{"id": f"sym:{f}", "name": name, "kind": "function", "file": f,
                 "start_line": 1,
                 "signature": "def place_order()" + (f": {IN_SIGNATURE}" if f in secret_files else "")}
                for f in [*secret_files, "src/orders.py"]]

    monkeypatch.setattr(tools, "query_graph", query_graph)
    monkeypatch.setattr(tools, "find_symbol", find_symbol)
    monkeypatch.setattr(tools, "find_callers", lambda **kw: {"callers": [
        {"name": "leaky", "file": f, "start_line": 1} for f in secret_files]})


#: A marker the graph tier would put in the prompt through a secret file's symbol.
IN_SIGNATURE = "SIG_MARKER_3e9f"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace_dir=tmp_path)


@pytest.fixture()
def prompts(monkeypatch, settings, tmp_path):
    """The prompts a stubbed model was sent, with the pipeline otherwise real."""
    _git_style_repo(tmp_path)
    sent: list[str] = []

    async def stream_chat(*, prompt, **_kw):
        sent.append(prompt)
        yield "an answer"

    monkeypatch.setattr("src.llm.completion.stream_chat", stream_chat)
    monkeypatch.setattr("src.llm.completion.embed", lambda *a, **k: [0.01] * 768)
    # Fully open access and no deny glob anywhere: the floor is not a rule.
    monkeypatch.setattr(mrr, "resolve_access",
                        lambda **kw: {r: RepoAccessDecision.full(r) for r in kw["repos"]})
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    monkeypatch.setattr(MultiRepoRetriever, "_collection_exists", lambda self: True)
    _fake_graph(monkeypatch, [".env", "config/credentials.yml", "config/secrets/db.py"])
    return sent


def _retriever(settings: Settings, qdrant=None) -> MultiRepoRetriever:
    return MultiRepoRetriever(
        settings=settings, vault_ret=types.SimpleNamespace(qdrant=qdrant or _Qdrant()))


async def _answer(settings, monkeypatch, repos, question=QUESTION, **kw):
    """The non-streaming path `ask` takes (`qa._generate_full`)."""
    from src.api.routers import qa

    monkeypatch.setattr(mrr, "MultiRepoRetriever", lambda: _retriever(settings))
    return await qa._generate_full(
        target_repos=repos, question=question, history=[], user_id="u1",
        is_admin=True, workspace_id=WS, name_free_notice=True, **kw)


# ─── the model's prompt ──────────────────────────────────────────────


async def test_no_secret_file_reaches_the_prompt_and_a_normal_file_still_does(
        settings, prompts, monkeypatch):
    text, meta = await _answer(settings, monkeypatch, [SLUG])

    prompt = prompts[-1]
    for marker in SECRETS:
        assert marker not in prompt, marker
    assert IN_SIGNATURE not in prompt, "a symbol of a secret file is not graph context"
    assert NORMAL in prompt and f"{SLUG}/src/orders.py" in meta["files_read"]
    assert text == "an answer"


async def test_the_answers_files_never_name_a_secret_file(settings, prompts, monkeypatch):
    _text, meta = await _answer(settings, monkeypatch, [SLUG])

    listed = " ".join([*meta["files_read"], *meta["hidden_files"]])
    for needle in (".env", "credentials.yml", "config/secrets", "notes.py"):
        assert needle not in listed, needle
    # The boundary notice reaches the prompt: the name of a credentials file is
    # not for the model either.
    assert "credentials.yml" not in prompts[-1] and "/.env" not in prompts[-1]


async def test_an_owner_is_refused_too_it_is_a_floor_not_a_rule(settings, prompts, monkeypatch):
    for is_admin in (True, False):
        monkeypatch.setattr(
            mrr, "resolve_access",
            lambda **kw: {r: RepoAccessDecision.full(r) for r in kw["repos"]})
        ctx = await _retriever(settings).retrieve(
            question=QUESTION, repos=[SLUG], workspace_id=WS, is_admin=is_admin)
        assert not any(m in ctx.prompt for m in SECRETS)
        assert not [f for f in ctx.files_read if f.endswith(("/.env", "credentials.yml"))]


async def test_a_symlink_to_a_secret_file_is_refused_by_its_target(
        settings, prompts, monkeypatch):
    """`src/notes.py` is a perfectly ordinary name; what it points at is not."""
    ctx = await _retriever(settings).retrieve(
        question="notes", repos=[SLUG], workspace_id=WS, is_admin=True)
    assert IN_LINK not in ctx.prompt and IN_ENV not in ctx.prompt
    assert f"{SLUG}/src/notes.py" not in ctx.files_read


async def test_the_no_vault_fallback_does_not_spend_its_picks_on_secret_files(
        settings, prompts, monkeypatch):
    monkeypatch.setattr(MultiRepoRetriever, "_collection_exists", lambda self: False)
    ctx = await _retriever(settings).retrieve(
        question="what are these services", repos=[SLUG], workspace_id=WS, is_admin=True)
    assert not any(m in ctx.prompt for m in SECRETS)
    assert f"{SLUG}/src/orders.py" in ctx.files_read or f"{SLUG}/README.md" in ctx.files_read


async def test_a_vault_note_about_a_secret_file_is_dropped(settings, prompts, monkeypatch):
    ctx = await _retriever(settings).retrieve(
        question=QUESTION, repos=[SLUG], workspace_id=WS, is_admin=True,
        name_free_notice=True)
    assert IN_NOTE not in ctx.prompt
    assert {h.note_path for h in ctx.vault_hits} == {"modules/src.md"}


# ─── repositories the graph cannot read: the plain-text tier ─────────


@pytest.fixture()
def upload_repo(monkeypatch, settings, tmp_path):
    from src.api import auto_review as ar

    repo = tmp_path / "repos" / UPLOAD
    _write(repo, {
        "Модуль.bsl": f"Процедура place_order()\n  Возврат \"{NORMAL}\";\nКонецПроцедуры\n",
        ".env": f"place_order\nKEY={IN_ENV}\n",
        "keys/server.pem": f"place_order\n{IN_CRED}\n",
        "ops/settings.yaml": ("place_order\n-----BEGIN RSA PRIVATE KEY-----\n"
                              f"{IN_SNIFF}\n-----END RSA PRIVATE KEY-----\n"),
    })
    store = ar.AutoReviewStore(tmp_path / "ar.db")
    store.upsert(ar.RepoConfig(user_id="u", repo_slug=UPLOAD, provider="upload",
                               full_name=f"upload/{UPLOAD}", url=f"upload:{UPLOAD}",
                               workspace_id=WS))
    monkeypatch.setattr(ar, "_default_store", store)
    monkeypatch.setattr(MultiRepoRetriever, "_collection_exists", lambda self: False)
    _fake_graph(monkeypatch, [])
    return repo


async def test_the_plain_text_tier_of_an_uploaded_repo_skips_secret_files(
        settings, prompts, upload_repo, monkeypatch):
    ctx = await _retriever(settings).retrieve(
        question=QUESTION, repos=[UPLOAD], workspace_id=WS, is_admin=True)

    assert NORMAL in ctx.prompt, "the ordinary file of an uploaded repo is still found"
    for marker in (IN_ENV, IN_CRED, IN_SNIFF):
        assert marker not in ctx.prompt, marker
    assert not [f for f in ctx.files_read if f.endswith((".env", ".pem"))]


def test_the_search_primitive_never_yields_a_secret_file(upload_repo):
    from src.qa import text_search

    paths = {rel for rel, _p in text_search.iter_text_files(upload_repo)}
    assert "Модуль.bsl" in paths
    assert not paths & {".env", "keys/server.pem"}
    hits, _done = text_search.search_text(upload_repo, "place_order")
    assert not {h.path for h in hits} & {".env", "keys/server.pem"}
    assert ".env" not in text_search.rank_files(upload_repo, "place_order")


# ─── one rule for every tool ─────────────────────────────────────────


@pytest.mark.parametrize("rel", [
    ".env", "svc/.env.production", "deploy/server.pem", "credentials.yml", "config/credentials.yaml",
    "config/secrets/db.py", "private_keys/a.txt", "terraform.tfstate", "_netrc",
    ".env.example",  # keys_only to the code reader; a caller that cannot mask hides it
    "a/./../.env", ".env ",
])
def test_the_retriever_and_the_dev_tools_ask_the_same_predicate(rel):
    from src.mcp_server.dev_profile import paths
    from src.security.secret_files import is_secret_path

    assert is_secret_path(rel) is True
    assert paths.is_secret_path(rel) is True
    assert mrr._secret_prefixed(f"{SLUG}/{rel}") is True


@pytest.mark.parametrize("rel", ["src/orders.py", "README.md", "docs/secrets-policy.md",
                                 "src/envelope.py", "environment.ts", "",
                                 # a module or a page NAMED credentials / secret is not a credentials file
                                 "src/credentials.py", "src/auth/Credentials.java", "docs/credentials.md",
                                 "internal/secret/secret.go", "private_keys/README.md"])
def test_an_ordinary_path_is_not_taken_for_a_secret(rel):
    from src.security.secret_files import is_secret_path

    assert is_secret_path(rel) is False


def test_the_dev_tools_gate_is_the_shared_predicate():
    from src.mcp_server.dev_profile import paths
    from src.security import secret_files

    assert paths.is_secret_path is secret_files.is_secret_path
    assert set(secret_files.FLOOR_NAME_GLOBS) <= {
        g.removeprefix(":(exclude,glob,icase)**/") for g in paths.git_excludes()}


def test_the_web_chat_and_ask_build_their_prompt_through_the_one_retriever():
    """The floor is in the retriever, so it protects both only while both use
    it. A second way to turn a path into prompt text would need its own."""
    from src.api.routers import qa

    for fn in (qa._generate_full, qa._ask_stream):
        source = inspect.getsource(fn)
        assert "MultiRepoRetriever()" in source and ".retrieve(" in source, fn.__name__
        assert "read_text" not in source and "read_bytes" not in source, fn.__name__
