"""There is one list of secret names, and every reader of a repository asks it.

THE DEFECT. `secret_files` said every reader asks the same function, but it had
two lists. `classify`, `safe_join`, `plain_file` and the git pathspecs used
`DENY_NAME_GLOBS` / `DENY_DIRS`; `is_secret_path` (the dev tools, the Q&A
retriever, the plain-text search) added a "floor" on top: `credentials.yml`,
`_netrc`, anything under `secret/` or `private_keys/`. So `ask` and `grep` hid
`credentials.yml` while the code reader (`tier3_code`), `howto` and the
exploration agent, which ask `safe_join` / `plain_file`, read it.

THE CONTRACT. The floor is part of the deny lists. For every floor name:

  * `classify` is not `ok`, `safe_join` refuses it, `plain_file` is None, and
    the git pathspecs exclude it, so a reader cannot differ from another (the
    two directories that are also ordinary package names, `secret/` and
    `private_keys/`, hide their data files and keep their source: see
    test_a_module_or_page_called_credentials_or_secret_is_not_a_secret_file.py);
  * `is_secret_path` is still `classify` plus ONE thing, `keys_only`: the
    `.env.example` family stays showable (masked) to the code reader and stays
    hidden from a caller that cannot mask.
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf


def _sample(glob: str) -> str:
    """A concrete file name that the glob matches."""
    return glob.replace("*", "x")


FLOOR_FILES = [_sample(g) for g in sf.FLOOR_NAME_GLOBS]
FLOOR_DIRS = sorted(sf.FLOOR_DIR_NAMES)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    return root


def _put(root, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("TOKEN=not-a-real-value\n")


@pytest.mark.parametrize("name", FLOOR_FILES)
def test_every_floor_name_is_denied_by_the_lists_every_reader_uses(repo, name):
    _put(repo, f"svc/{name}")
    rel = f"svc/{name}"

    assert sf.classify(rel) != "ok", name
    assert sf.is_secret_path(rel) is True
    assert sf.plain_file(repo, rel) is None, "the reader that opens files is refused"
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, rel)


@pytest.mark.parametrize("directory", FLOOR_DIRS)
def test_nothing_below_a_floor_directory_is_readable_by_any_reader(repo, directory):
    rel = f"cfg/{directory}/settings.py"
    _put(repo, rel)

    assert sf.classify(rel) == "deny", directory
    assert sf.plain_file(repo, rel) is None
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, rel)
    assert f":(exclude,glob,icase)**/{directory}/**" in sf.GIT_PATHSPEC_EXCLUDES


@pytest.mark.parametrize("glob", sf.FLOOR_NAME_GLOBS)
def test_the_git_pathspecs_exclude_every_floor_name(glob):
    assert f":(exclude,glob,icase)**/{glob}" in sf.GIT_PATHSPEC_EXCLUDES


def test_the_floor_is_not_a_second_list_beside_the_deny_list():
    assert set(sf.FLOOR_NAME_GLOBS) <= set(sf.DENY_NAME_GLOBS)
    assert sf.FLOOR_DIR_NAMES <= sf.DENY_DIRS


def test_a_reader_that_asks_safe_join_no_longer_reads_what_the_dev_tools_hide(repo):
    """`credentials.yml` and `private_keys/` were the names only the floor knew; the code reader
    (`tier3_code._safe_path`) and `howto` read them. (Data files only: a `secret/db.py` or a
    `credentials.py` is source, see the sibling test.)"""
    from src.mcp_server.howto import engine
    from src.retrieval import tier3_code

    for rel in ("credentials.yml", "_netrc", "private_keys/a.txt", "secret/db.yml"):
        _put(repo, rel)
        assert sf.is_secret_path(rel) is True
        assert tier3_code._safe_path(repo, rel) is None, rel
        assert engine._read(repo, rel) is None, rel


def test_the_example_env_family_stays_showable_masked_and_hidden_from_a_caller_that_cannot_mask(repo):
    _put(repo, ".env.example")

    assert sf.classify(".env.example") == "keys_only"
    assert sf.plain_file(repo, ".env.example") is not None
    assert sf.is_secret_path(".env.example") is True


@pytest.mark.parametrize("rel", [
    "src/app.py", "README.md", "docs/secrets-policy.md", "src/credentials_test_helpers.py",
    "src/envelope.py", "lib/secretary/notes.py",
])
def test_ordinary_files_are_not_swept_up_by_the_floor(repo, rel):
    _put(repo, rel)

    assert sf.classify(rel) == "ok" and sf.is_secret_path(rel) is False
    assert sf.plain_file(repo, rel) is not None
