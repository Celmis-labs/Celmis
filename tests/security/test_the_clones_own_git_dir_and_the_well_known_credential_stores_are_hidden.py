"""The clone's `.git` and the credential stores that tools drop into a repository are not files to read.

THE DEFECT. The floor knew `.env`, keys and state files but not `.git/` (its `config` can hold a remote URL with a
token, its objects every secret ever committed), `.dockerconfigjson` (a registry login), `*.jceks` / `keystore`
(Java key stores), `.boto`, `.azure/accessTokens.json`, `.config/gh/hosts.yml`, `local.settings.json` (Azure
Functions connection strings), `sftp-config.json` (an SFTP password), nor the package-manager files that carry
registry or server logins (`auth.json`, `.yarnrc.yml`, `.m2/settings.xml`).

THE CONTRACT. They are denied for every reader, as the rest of the floor. `auth.json` and `.m2/settings.xml`
only where they ARE the credential file (the repository root / the Maven home): `locales/en/auth.json` is a
translation file and `ci/settings.xml` a build setting. The package-manager files are denied rather than shown
masked because the example-file masker understands flat `KEY=value` lines and these are YAML, XML, JSON and
space-separated ones: a value could stand on a line the masker keeps.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.security import secret_files as sf

DENIED = [
    ".git/config", ".git/HEAD", ".git/hooks/pre-commit", "vendor/lib/.git/config", ".GIT/config", ".git",
    ".dockerconfigjson", "k8s/.dockerconfigjson", "dockerconfigjson", "k8s/registry.dockerconfigjson",
    "app.jceks", "app.bks", "android/app/keystore", "release.keystore", "app.jks", ".boto",
    ".azure/accessTokens.json", ".azure/msal_token_cache.json", ".azure/service_principal_entries.json",
    ".config/gh/hosts.yml", "local.settings.json", "functions/local.settings.json", "sftp-config.json",
    "sftp-config-alt.json", ".ftpconfig",
    "auth.json", ".composer/auth.json", ".yarnrc.yml", ".yarnrc", "web/.yarnrc.yml",
    ".m2/settings.xml", ".m2/settings-security.xml", "ci/settings-security.xml",
]
ORDINARY = [
    ".gitignore", ".gitattributes", ".gitmodules", ".github/workflows/ci.yml", ".github/CODEOWNERS",
    "src/git.py", "docs/git/config.md", "src/dockerconfig.py", "src/keystore.py", "docs/keystore.md",
    "locales/en/auth.json", "src/auth.json", "ci/settings.xml", ".mvn/settings.xml", "boto.cfg",
    ".yarn/releases/yarn-4.cjs", ".config/gh/config.yml", ".azure/azureProfile.json", "sftp-config.md",
    "functions/host.json",
]


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("password = not-a-real-value\n")


@pytest.mark.parametrize("rel", DENIED)
def test_a_git_dir_or_a_credential_store_is_denied_for_every_reader(repo, rel):
    from src.indexing.graph.languages.factory import walk_repo_files
    from src.mcp_server.howto import engine
    from src.retrieval.tier3_code import CodeReader

    _put(repo, rel)

    assert sf.classify(rel) == "deny" and sf.is_secret_path(rel) is True
    assert sf.plain_file(repo, rel) is None
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, rel)
    assert CodeReader().read_full_file(repo, rel) is None
    assert engine._read(repo, rel) is None
    assert not [p for p in walk_repo_files(repo) if p.relative_to(repo.resolve()).as_posix() == rel]


@pytest.mark.parametrize("rel", ORDINARY)
def test_a_file_with_a_similar_name_that_is_not_a_credential_store_stays_readable(repo, rel):
    _put(repo, rel)

    assert sf.classify(rel) == "ok" and sf.plain_file(repo, rel) is not None


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_real_clones_git_directory_is_not_readable_through_the_code_reader_or_the_search(repo):
    from src.qa import text_search
    from src.retrieval.tier3_code import CodeReader

    _put(repo, "src/app.py")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["init", "-q"], ["remote", "add", "origin", "https://user:TOKEN-a91c@example.com/acme/app.git"],
                ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)

    reader = CodeReader()
    assert reader.read_full_file(repo, ".git/config") is None
    assert reader.read_full_file(repo, ".git/HEAD") is None
    assert reader.read_full_file(repo, "src/app.py") is not None
    assert not [rel for rel, _ in text_search.iter_text_files(repo) if rel.startswith(".git/")]
    hits, _ = text_search.search_text(repo, "TOKEN-a91c")
    assert hits == []
