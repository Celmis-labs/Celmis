"""A module, a page or a package that happens to be called `credentials` or `secret` is not a secret file.

THE DEFECT. Unifying the floor into the one deny list made `credentials.*` and the
directories `secret/` and `private_keys/` hide EVERYTHING that matched, for every
reader at once (the indexing walker, the code reader, `howto`, ingest, drift, `ask`,
citations): `src/auth/credentials.py`, `credentials.ts`, `Credentials.java`,
`docs/credentials.md`, Go's `internal/secret/secret.go`, `private_keys/README.md`.
An auth module that is the first thing a question about login reaches for was
invisible, and the answer was built without it.

THE CONTRACT.
  * `credentials.<data>` (json, yml, toml, ini, txt, csv, xml, env, a backup or a
    template spelling, ...) and the bare `credentials` stay secret; `credentials`
    with a source or prose extension does not;
  * under `secret/` and `private_keys/` the files that are not source code or prose
    are secret, the source and the prose are ordinary, and every NAME rule still
    applies inside (a `secret/id_rsa.py` is a key);
  * every reader agrees, because they all ask `classify`.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.security import secret_files as sf

ORDINARY = [
    "src/auth/credentials.py", "src/auth/credentials.ts", "src/auth/credentials.tsx",
    "src/auth/Credentials.java", "src/auth/Credentials.cs", "src/auth/credentials.go",
    "src/auth/credentials.rb", "pkg/credentials/credentials.rs", "docs/credentials.md",
    "docs/credentials.rst", "internal/secret/secret.go", "internal/secret/store.go",
    "src/secret/vault.py", "src/secret/README.md", "private_keys/README.md",
    "private_keys/rotate.py", "docs/private_keys/howto.md", "secret/handler.ts",
]
STILL_SECRET = [
    "credentials", "credentials.json", "Credentials.JSON", "credentials.yml", "credentials.yaml",
    "credentials.toml", "credentials.ini", "credentials.cfg", "credentials.conf", "credentials.txt",
    "credentials.csv", "credentials.xml", "credentials.properties", "credentials.env",
    "credentials.prod.yml", "credentials.yml.enc", "credentials.json.bak", "credentials.bak",
    "credentials.example", "config/credentials.yml.enc", ".cargo/credentials.toml",
    # a data file below the two directories, however it is named
    "secret/prod.yml", "secret/data.json", "secret/key.txt", "secret/db-password",
    "secret/notes.bin", "private_keys/a.txt", "private_keys/a.pem", "config/secret/.env",
    # the name rules still hold inside a soft directory
    "secret/id_rsa.py", "private_keys/server.key", "secret/credentials",
]


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str, text: str = "x = 1\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.parametrize("rel", ORDINARY)
def test_an_ordinary_file_with_a_secret_sounding_name_is_not_hidden_from_any_reader(repo, rel):
    from src.indexing.graph.languages.factory import walk_repo_files
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel)

    assert sf.classify(rel) == "ok" and sf.is_secret_path(rel) is False
    assert sf.plain_file(repo, rel) is not None
    assert sf.classify_file(repo, rel) == "ok"
    assert CodeReader().read_full_file(repo, rel) is not None, "the code reader"
    assert engine._read(repo, rel) is not None, "howto"
    assert rel in {p.relative_to(repo.resolve()).as_posix() for p in walk_repo_files(repo)}, "the indexer"


@pytest.mark.parametrize("rel", STILL_SECRET)
def test_a_credentials_or_secret_data_file_is_still_hidden_from_every_reader(repo, rel):
    from src.indexing.graph.languages.factory import walk_repo_files
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel, "token: not-a-real-value\n")

    assert sf.classify(rel) == "deny" and sf.is_secret_path(rel) is True
    assert sf.plain_file(repo, rel) is None
    assert CodeReader().read_full_file(repo, rel) is None
    assert engine._read(repo, rel) is None
    assert rel not in {p.relative_to(repo.resolve()).as_posix() for p in walk_repo_files(repo)}


def test_the_two_soft_directories_are_the_ones_that_are_also_ordinary_package_names():
    assert {"secret", "private_keys"} == sf.SOFT_DENY_DIRS
    assert not sf.SOFT_DENY_DIRS & sf.FLOOR_DIR_NAMES, "a directory is hard or soft, not both"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_grep_with_the_exclusions_finds_the_source_and_never_opens_the_data_file(tmp_path):
    """The real pathspecs, against a real `git grep`: the belt over the braces."""
    from src.mcp_server.dev_profile.paths import git_excludes

    root = tmp_path / "r"
    _put(root, "internal/secret/secret.go", "package secret // NEEDLE\n")
    _put(root, "src/auth/credentials.py", "# NEEDLE\n")
    _put(root, "secret/db.yml", "password: NEEDLE\n")
    _put(root, "credentials.yml", "password: NEEDLE\n")
    _put(root, ".git-credentials", "https://u:NEEDLE@h\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env, capture_output=True)
    out = subprocess.run(
        ["git", "-C", str(root), "grep", "-l", "-F", "-e", "NEEDLE", "HEAD", "--", ".", *git_excludes()],
        check=True, env=env, capture_output=True, text=True).stdout.split()
    found = {line.split(":", 1)[1] for line in out}

    assert found == {"internal/secret/secret.go", "src/auth/credentials.py"}
