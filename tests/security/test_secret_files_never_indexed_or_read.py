"""A committed ``.env``, key or credential store is invisible to Celmis.

The classifier decides once; the walker, the code reader and the exploration
agent all ask it. Paths that leave the repository are refused the same way, so
a refusal says nothing about what exists.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

from src.security import secret_files as sf
from src.security.secret_files import SecretPathRefused, classify, mask_env_values, safe_join

DENY = [
    ".env", ".env.local", ".env.production", "api/.env", "deploy/prod.env",
    "certs/server.pem", "certs/tls.key", "ci/id_rsa", "ci/id_ed25519", "ci/id_rsa.pub",
    "store.p12", "store.pfx", "keys.jks", "app.keystore", "vault.kdbx",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".htpasswd",
    "config/secrets/db.yml", "a/.secrets/x.txt", "home/.ssh/config", "x/.aws/credentials",
    "credentials.json", "gcp/service-account-prod.json", "infra/prod.tfvars",
    "infra/terraform.tfstate", "infra/terraform.tfstate.backup", "kubeconfig", "vpn/client.ovpn",
    "app/db.enc.yaml", ".docker/config.json", "secrets.yml",
]
OK = [
    "src/app.py", "README.md", "docker-compose.yml", "Dockerfile", "src/secrets.py",
    "src/keys/index.ts", "docs/env.md", "src/environment.ts", "tests/test_env.py",
    ".github/workflows/ci.yml", "package.json", "src/config.py",
]
KEYS_ONLY = [".env.example", ".env.sample", ".env.template", ".env.dist", "api/.env.example", "env.example"]


@pytest.mark.parametrize("rel", DENY)
def test_a_secret_file_is_denied(rel):
    assert classify(rel) == "deny"


@pytest.mark.parametrize("rel", OK)
def test_an_ordinary_file_is_ok(rel):
    assert classify(rel) == "ok"


@pytest.mark.parametrize("rel", KEYS_ONLY)
def test_an_example_env_file_is_keys_only(rel):
    assert classify(rel) == "keys_only"


def test_the_content_sniff_denies_a_private_key_and_a_kubernetes_secret():
    assert classify("notes.txt", b"-----BEGIN RSA PRIVATE KEY-----\nabc\n") == "deny"
    assert classify("m.yaml", b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n") == "deny"
    assert classify("m.yaml", b"apiVersion: v1\nkind: ConfigMap\n") == "ok"


def test_extra_globs_add_to_the_list_and_cannot_remove_from_it(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("SECRET_PATH_GLOBS_EXTRA", '["**/internal-*.txt"]')
    get_settings.cache_clear()
    try:
        assert classify("a/internal-notes.txt") == "deny"
        assert classify(".env") == "deny"
    finally:
        get_settings.cache_clear()


def test_values_in_an_example_file_are_withheld_unless_they_are_placeholders():
    c = secrets.token_urlsafe(12)
    out = mask_env_values(
        f"DATABASE_URL=\nDB_POOL_SIZE=5\nAPI_TOKEN={c}\nHOST=${{HOST}}\nPW=changeme\n# KEY={c}\nURL=postgres://u:{c}@h/d"
    )
    assert c not in out.replace("# KEY=" + c, "")
    assert "DATABASE_URL=" in out and "DB_POOL_SIZE=5" in out and "PW=changeme" in out
    assert "API_TOKEN=[value withheld]" in out


# ─── safe_join ───────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "config").mkdir(parents=True)
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("x = 1\n")
    (r / ".env").write_text("A=1\n")
    (r / "config" / "ok.yml").write_text("a: 1\n")
    (tmp_path / "outside.txt").write_text("outside\n")
    return r


@pytest.mark.parametrize("rel", [".env", "config/../.env", "./.env", "src/../.env", "../outside.txt",
                                 "../../etc/passwd", "src/../../outside.txt", "/etc/passwd"])
def test_safe_join_refuses_secrets_and_paths_that_leave_the_repo_with_one_message(repo, rel):
    with pytest.raises(SecretPathRefused) as exc:
        safe_join(repo, rel)
    assert str(exc.value) == sf.REFUSED


def test_safe_join_resolves_an_ordinary_path(repo):
    assert safe_join(repo, "src/app.py") == (repo / "src" / "app.py").resolve()
    assert safe_join(repo, "config/../src/app.py") == (repo / "src" / "app.py").resolve()


def test_safe_join_refuses_a_symlink_to_a_secret_or_out_of_the_repo(repo, tmp_path):
    os.symlink(repo / ".env", repo / "innocent.txt")
    os.symlink(tmp_path / "outside.txt", repo / "link.txt")
    for rel in ("innocent.txt", "link.txt"):
        with pytest.raises(SecretPathRefused):
            safe_join(repo, rel)


def test_a_missing_file_and_a_secret_file_are_refused_differently_only_by_the_caller(repo):
    """safe_join answers for the path, not for existence: a path that does not
    exist is joined (the reader then finds nothing), a secret one is refused
    whether or not it exists."""
    assert safe_join(repo, "src/nope.py").name == "nope.py"
    with pytest.raises(SecretPathRefused):
        safe_join(repo, "nothere/.env")


# ─── the readers ─────────────────────────────────────────────────────


def _repo_with_secrets(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    r = tmp_path / "r"
    r.mkdir()
    k = {n: secrets.token_urlsafe(12) for n in ("env", "pem", "tfvars", "ex", "app")}
    (r / ".env").write_text(f"DATABASE_URL=postgresql://u:{k['env']}@h/d\n")
    (r / "server.pem").write_text(f"-----BEGIN PRIVATE KEY-----\n{k['pem']}\n-----END PRIVATE KEY-----\n")
    (r / "prod.tfvars").write_text(f'db_password = "{k["tfvars"]}"\n')
    (r / ".env.example").write_text(f"DATABASE_URL=\nAPI_TOKEN={k['ex']}\n")
    (r / "app.py").write_text("import os\n\ndef run():\n    return os.getenv('DATABASE_URL')\n")
    return r, k


def test_the_walker_never_returns_a_secret_file(tmp_path):
    from src.indexing.graph.languages.factory import walk_repo_files

    r, _ = _repo_with_secrets(tmp_path)
    (r / "k8s").mkdir()
    (r / "k8s" / "s.yaml").write_text("kind: Secret\ndata:\n  a: Yg==\n")
    names = {p.relative_to(r.resolve()).as_posix() for p in walk_repo_files(r)}
    assert names == {"app.py", ".env.example"}


def test_the_code_reader_refuses_secret_files_and_masks_example_values(tmp_path):
    from src.retrieval.tier3_code import CodeReader

    r, k = _repo_with_secrets(tmp_path)
    reader = CodeReader()
    for rel in (".env", "server.pem", "prod.tfvars", "../r/.env", "nothere/../.env"):
        assert reader.read_full_file(r, rel) is None, rel
        assert reader.read_locations(r, [(rel, 1, 3)]).snippets == [], rel
    ex = reader.read_full_file(r, ".env.example", redact_content=False)
    assert ex is not None and k["ex"] not in ex.content and "DATABASE_URL=" in ex.content
    assert reader.read_full_file(r, "app.py") is not None


def test_the_code_reader_cannot_be_walked_out_of_the_repository(tmp_path):
    from src.retrieval.tier3_code import CodeReader

    r, _ = _repo_with_secrets(tmp_path)
    (tmp_path / "outside.py").write_text("SECRET_OUTSIDE = 1\n")
    assert CodeReader().read_full_file(r, "../outside.py") is None


def test_the_exploration_agents_grep_refuses_secret_files_and_redacts_lines(tmp_path):
    from src.qa.exploration_agent import ExplorationAgent

    r, k = _repo_with_secrets(tmp_path)
    (r / "settings.py").write_text(f'DB = "postgresql://app:{k["app"]}@db/x"\nNAME = "x"\n')
    agent = ExplorationAgent.__new__(ExplorationAgent)
    agent.repo_path = r
    refused = agent._tool_grep_in_file({"file": ".env", "pattern": "DATABASE"})
    assert refused == {"error": "not_found: .env"}
    hit = agent._tool_grep_in_file({"file": "settings.py", "pattern": "postgresql"})
    assert hit["count"] == 1 and k["app"] not in str(hit) and "[REDACTED" in str(hit)
    ex = agent._tool_grep_in_file({"file": ".env.example", "pattern": "TOKEN"})
    assert k["ex"] not in str(ex)
