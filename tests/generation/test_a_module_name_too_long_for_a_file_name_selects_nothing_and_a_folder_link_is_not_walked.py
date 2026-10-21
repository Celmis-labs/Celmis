"""A module name a model made up must not crash the feature note, nor name the server's path.

THE DEFECT. `_module_file_locations` asked `is_dir()` of `repo_path / name` for each module
name feature detection produced. A name longer than a file name can be (300 characters) makes
`stat` raise `OSError: [Errno 63] File name too long: '<the clone's absolute path>'`, which
escaped the function: the feature note failed, and the message carried a server path. A module
name that is a LINK to a folder was walked as well, listing the folder's files a second time
under the link's name (or the files of a folder outside the clone, if `plain_file` had let them in).

THE CONTRACT. A name the operating system refuses selects nothing and raises nothing; a name that
is a link selects nothing; a real module still lists its files, once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.generation.feature_doc import _module_file_locations


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "acme-app"
    (repo / "air").mkdir(parents=True)
    (repo / "air" / "views.py").write_text("x = 1\n")
    elsewhere = tmp_path / "repos" / "other-tenant"
    elsewhere.mkdir()
    (elsewhere / "billing.py").write_text("y = 2\n")
    try:
        (repo / "linked_air").symlink_to(repo / "air", target_is_directory=True)
        (repo / "linked_out").symlink_to(elsewhere, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")
    return repo


def _files(repo: Path, names: list[str]) -> list[str]:
    return [f for f, _line, _end in _module_file_locations(repo, names)]


@pytest.mark.parametrize("name", [
    "a" * 300, "x" * 5000, "modules/" + "b" * 300, "air/" + "c" * 400,
], ids=["300", "5000", "300 after a slash", "400 after a folder"])
def test_a_module_name_longer_than_a_file_name_selects_nothing_and_raises_nothing(clone, name):
    assert _files(clone, [name]) == []


def test_an_over_long_name_does_not_hide_the_modules_next_to_it(clone):
    assert _files(clone, ["a" * 300, "air", "x" * 5000]) == ["air/views.py"]


@pytest.mark.parametrize("name", ["linked_air", "linked_out"])
def test_a_module_name_that_is_a_link_to_a_folder_selects_nothing(clone, name):
    assert _files(clone, [name]) == []


def test_a_real_module_is_listed_once_even_when_a_link_points_at_it(clone):
    assert _files(clone, ["air", "linked_air"]) == ["air/views.py"]
