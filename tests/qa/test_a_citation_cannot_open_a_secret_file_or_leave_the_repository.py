"""A citation link is text the model wrote; it must not become a file oracle.

THE DEFECT. `verify_citations` opened whatever a link named: it counted the
lines of a `.env` (so `#L3` against `#L999` told a stranger how long the file
was, `bad_line` against `ok` told them a line existed), and with `..` or an
absolute path after the repository name it did the same to any file the server
could read. A prompt-injected answer only has to write the link.

THE CONTRACT.

  * a secret file (`.env`, a key, a credentials store, a data file whose head
    is a service-account key), a path with a `..` segment, an absolute path and
    a symlink are all `missing_file` with EXACTLY the detail of a file that does
    not exist, whatever line the link asks for: no `bad_line`, no `ok`, no
    line count;
  * the same for a path with no repository prefix: `unknown_repo`, as for a
    file that is not there;
  * an ordinary file is verified as before, and `./` and `//` in the link are
    only normalised away (`repo//./a.py` is `repo/a.py`; only a path with NO
    repository in front keeps its leading `/`, and is refused);
  * a file that exists but cannot be read answers EXACTLY like a file that is not
    there ("file does not exist in the repo"), with or without a line number or a
    quoted snippet: a different sentence ("could not be read") would be an
    existence oracle for names behind a permission, and the operating system's
    message carries the absolute path of the clone on the server.
"""

from __future__ import annotations

import os

import pytest

from src.config import Settings
from src.qa.citations import verify_citations

REPO = "acme-app"
OUTSIDE_TEXT = "alpha\nbeta\ngamma\n"


@pytest.fixture()
def world(tmp_path):
    settings = Settings(workspace_dir=tmp_path)
    root = settings.repos_dir / REPO
    (root / "src").mkdir(parents=True)
    (root / ".github").mkdir()
    (root / "src" / "app.py").write_text("def run():\n    return 1\n\n\nx = 2\n")
    (root / ".github" / "ci.yml").write_text("name: ci\non: push\n")
    (root / ".env").write_text("API_KEY=not-a-real-key\nB=1\nC=2\n")
    (root / "sa.json").write_text('{"type": "service_account", "project_id": "acme"}\n')
    outside = settings.repos_dir / "outside.txt"          # a sibling of the clone
    outside.write_text(OUTSIDE_TEXT)
    (root / "src" / "link.txt").symlink_to(outside)         # a link leaving the repository
    (root / "src" / "to_env.txt").symlink_to(root / ".env")  # a link to a secret inside it
    return settings, outside


def _verdict(world, target: str):
    settings, _outside = world
    report = verify_citations(f"see [x]({target})", repos=[REPO], settings=settings)
    assert report.total == 1, target
    c = report.citations[0]
    return c.status, c.detail


def _missing(world):
    return _verdict(world, f"{REPO}/src/nothing.py#L1")


def test_a_normal_file_is_still_verified(world):
    assert _verdict(world, f"{REPO}/src/app.py#L2") == ("ok", "")
    status, detail = _verdict(world, f"{REPO}/src/app.py#L99")
    assert status == "bad_line" and "outside file" in detail


@pytest.mark.parametrize("target", [
    f"{REPO}/./src//app.py#L1",
    f"./{REPO}/src/app.py#L1",
    f"{REPO}//./src/app.py#L1",        # the doubled slash after the repo name is only a separator
    f"{REPO}///src/app.py#L1",
    f"{REPO}/.github/ci.yml#L1",       # the dot of a dot-directory is not "./"
])
def test_dots_and_double_slashes_are_normalised_not_refused(world, target):
    assert _verdict(world, target) == ("ok", "")


@pytest.mark.parametrize("line", ["", "#L1", "#L3", "#L999"])
def test_a_secret_file_reads_exactly_like_a_file_that_is_not_there(world, line):
    """`.env` has 3 lines: `#L1` would be ok and `#L999` bad_line if it were opened."""
    status, detail = _verdict(world, f"{REPO}/.env{line}")
    assert (status, detail) == _missing(world) == ("missing_file", "file does not exist in the repo")


@pytest.mark.parametrize("line", ["#L1", "#L999"])
def test_a_data_file_whose_head_is_a_key_reads_like_a_missing_file(world, line):
    """The name `sa.json` is ordinary; its CONTENT (a service-account key) is not."""
    assert _verdict(world, f"{REPO}/sa.json{line}") == _missing(world)


@pytest.mark.parametrize("line", ["#L1", "#L999"])
def test_a_dotdot_path_cannot_reach_a_file_outside_the_clone(world, line):
    assert _verdict(world, f"{REPO}/../outside.txt{line}") == _missing(world)
    assert _verdict(world, f"{REPO}/src/../../outside.txt{line}") == _missing(world)


@pytest.mark.parametrize("line", ["#L1", "#L999"])
def test_an_absolute_path_after_the_repo_name_cannot_reach_the_server(world, line):
    _settings, outside = world
    assert str(outside).startswith("/")
    assert _verdict(world, f"{REPO}/{outside}{line}") == _missing(world)      # `repo//abs/path`


def test_a_bare_absolute_or_dotdot_path_is_an_unknown_repo_like_any_other_miss(world):
    _settings, outside = world
    miss = _verdict(world, "nowhere/nothing.py#L1")
    assert miss[0] == "unknown_repo"
    assert _verdict(world, f"{outside}#L1") == miss
    assert _verdict(world, "../outside.txt#L1") == miss
    assert _verdict(world, ".env#L1") == miss


@pytest.mark.parametrize("name", ["link.txt", "to_env.txt"])
@pytest.mark.parametrize("line", ["#L1", "#L999"])
def test_a_symlink_is_not_followed_out_of_the_repository_or_to_a_secret(world, name, line):
    assert _verdict(world, f"{REPO}/src/{name}{line}") == _missing(world)


def test_a_normalised_path_is_the_one_that_is_reported(world):
    settings, _outside = world
    report = verify_citations(f"see [x]({REPO}//./src//app.py#L1)", repos=[REPO], settings=settings)
    assert report.citations[0].path == "src/app.py" and report.citations[0].repo == REPO


def test_an_unreadable_file_does_not_leak_the_servers_path_in_the_detail(world, monkeypatch):
    """`OSError` text is `[Errno 13] Permission denied: '/srv/celmis/ws/repos/acme-app/src/app.py'`."""
    import pathlib

    settings, _outside = world
    real_open = pathlib.Path.open

    def refuse(self, *args, **kwargs):
        if self.name == "app.py":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", refuse)

    status, detail = _verdict(world, f"{REPO}/src/app.py#L2")

    assert (status, detail) == _missing(world) == ("missing_file", "file does not exist in the repo")
    assert str(settings.repos_dir) not in detail and "Errno" not in detail


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root can open a file whatever its mode")
@pytest.mark.parametrize("suffix", ["", "#L1", "#L999"])
def test_a_file_the_server_may_not_open_is_not_told_apart_from_a_missing_one(world, suffix):
    """A real mode-000 file, cited without a line, with a line in range and with one past the end."""
    settings, _outside = world
    locked = settings.repos_dir / REPO / "src" / "locked.py"
    locked.write_text("def secret_thing():\n    return 1\n")
    locked.chmod(0)
    try:
        assert _verdict(world, f"{REPO}/src/locked.py{suffix}") == _missing(world)
    finally:
        locked.chmod(0o600)


def test_an_unreadable_file_with_a_quoted_snippet_is_missing_too(world, monkeypatch):
    """Not `ok`: the snippet check cannot run on a file that cannot be opened, and a
    missing file never reaches it, so the two must not part ways here either."""
    import pathlib

    settings, _outside = world
    real_open = pathlib.Path.open

    def refuse(self, *args, **kwargs):
        if self.name == "app.py":
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", refuse)
    text = f"see [x]({REPO}/src/app.py)\n```python\ndef invented_function():\n    return 42\n```\n"

    report = verify_citations(text, repos=[REPO], settings=settings)

    assert [(c.status, c.detail) for c in report.citations] == [_missing(world)]
