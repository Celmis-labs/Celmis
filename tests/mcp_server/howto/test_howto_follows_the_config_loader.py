"""The connection code reads ``config.databaseUrl``; the environment name is in the loader.

A TypeScript and a Go service whose pattern code calls into a config loader in
another file: the answer must add the loader slice and list the variable names
it reads, so an agent knows what to ask for.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.access import RepoAccessDecision
from src.mcp_server.howto import engine, run_howto

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dev_mcp"


def _commit(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "develop")
    git("add", "-A", "-f")
    git("-c", "user.email=a@example.com", "-c", "user.name=a", "commit", "-q", "-m", "init")


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    import src.groups.manager as gm
    from src.config import get_settings

    get_settings.cache_clear()
    gm._default_manager = None
    repos = get_settings().repos_dir
    slugs = {}
    for name in ("acme-billing", "acme-gateway"):
        dest = repos / name
        shutil.copytree(FIXTURES / name, dest)
        _commit(dest)
        slugs[name] = name
    monkeypatch.setattr(
        engine, "accessible_code_repos",
        lambda: {s: RepoAccessDecision.full(s) for s in slugs},
    )
    monkeypatch.setattr(
        engine, "_idx_provider",
        lambda slug, path: engine.IdxInfo(slug=slug, branch="develop", sha="3f2a91c8d7e6b5a4", age="2h", state="fresh"),
    )
    yield slugs
    get_settings.cache_clear()
    gm._default_manager = None


def _inputs(out: str) -> str:
    return out.split("inputs (names only", 1)[1]


def test_a_typescript_connection_lists_the_names_its_config_loader_reads(services):
    out = run_howto("db", "acme-billing")
    assert "src/db/pool.ts" in out and "src/config.ts" in out
    assert "DATABASE_URL" in out and "DB_POOL_SIZE" in out
    assert "STRIPE_API_KEY" not in _inputs(out)  # not a database name
    assert ".env.example:2" in out and "bitbucket-pipelines.yml" in out


def test_a_go_auth_pattern_lists_the_names_and_the_secret_store_of_its_config(services):
    out = run_howto("auth", "acme-gateway")
    assert "internal/auth/middleware.go" in out and "internal/config/config.go" in out
    assert "JWT_KEY_PATH" in out and "JWT_ISSUER" in out
    assert "vault secret/data/acme/gateway/jwt" in out
    assert "GATEWAY_LISTEN" not in _inputs(out)  # not an auth name


def test_a_file_the_pattern_never_mentions_is_not_pulled_in(services, tmp_path):
    from src.config import get_settings

    repo = get_settings().repo_path("acme-billing")
    (repo / "src" / "settings_unrelated.ts").write_text(
        'export const x = process.env.DB_UNRELATED_NAME;\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@example.com", "-c", "user.name=a",
                    "commit", "-q", "-m", "more"], check=True)
    out = run_howto("db", "acme-billing")
    assert "DB_UNRELATED_NAME" not in out
