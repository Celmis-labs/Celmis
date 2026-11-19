"""A scratch vault, a real in-memory Qdrant and an embedder that costs nothing.

``kb_world`` points the settings at a temporary vault and workspace, answers
"whose repository is this" with one workspace, and replaces the Qdrant client
with ``QdrantClient(":memory:")`` — the real filter engine, so a test of a
front matter filter tests what production runs. Every text embeds to the
same vector: search order is then decided by the filters and the ranking on
top, which is what these tests are about.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

WS = "ws-kb"
VEC = [0.1] * 8


@dataclass
class KbWorld:
    tmp: Path
    qdrant: object

    @property
    def settings(self):  # noqa: ANN201
        from src.config import get_settings

        return get_settings()

    def vault(self, slug: str) -> Path:
        return self.settings.repo_vault_path(slug)

    def repo(self, slug: str) -> Path:
        return self.settings.repo_path(slug)

    def payloads(self) -> list[dict]:
        from src.config import get_settings

        name = get_settings().qdrant_collection
        if not self.qdrant.collection_exists(name):
            return []
        points, _ = self.qdrant.scroll(name, limit=1000, with_payload=True)
        return [p.payload for p in points]


@pytest.fixture
def kb_world(tmp_path, monkeypatch):
    from qdrant_client import QdrantClient

    from src.config import get_settings
    from src.llm import completion

    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()
    client = QdrantClient(":memory:")
    monkeypatch.setattr("src.retrieval.tier1_vault._build_qdrant_client", lambda settings: client)
    monkeypatch.setattr(completion, "embed", lambda *a, **k: list(VEC), raising=False)
    monkeypatch.setattr(completion, "embed_batch", lambda texts, *a, **k: [list(VEC) for _ in texts],
                        raising=False)
    monkeypatch.setattr("src.api.auto_review.workspace_for_repo_slug", lambda slug: WS)
    yield KbWorld(tmp_path, client)
    get_settings.cache_clear()


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                         env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                              "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    return out.stdout.strip()


def make_config_repo(root: Path) -> tuple[str, str]:
    """A tiny 1C dump under git: (first commit, second commit). The second
    changes only the order document's object module."""
    (root / "Documents" / "Заказ" / "Ext").mkdir(parents=True)
    (root / "Catalogs").mkdir(parents=True)
    (root / "Documents" / "Заказ.xml").write_text("<x/>", encoding="utf-8")
    (root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения()\nКонецПроцедуры\n", encoding="utf-8")
    (root / "Catalogs" / "Товары.xml").write_text("<x/>", encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    first = git(root, "rev-parse", "HEAD")
    (root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl").write_text(
        "Процедура ОбработкаПроведения()\n\tДвижения.Записать();\nКонецПроцедуры\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "two")
    return first, git(root, "rev-parse", "HEAD")


def card(*, id: str, status: str = "draft", type: str = "process", objects: str = "",  # noqa: A002
         source: str = "", extra: str = "", body: str = "") -> str:
    lines = ["---", f"type: {type}", f"id: {id}", f"status: {status}"]
    if objects:
        lines.append(f"objects_1c: [{objects}]")
    if source:
        lines.append(f"source: {source}")
    if extra:
        lines.append(extra)
    lines.append("---")
    return "\n".join(lines) + "\n" + (body or f"# Картка {id}\n\nОпис процесу {id}.\n")
