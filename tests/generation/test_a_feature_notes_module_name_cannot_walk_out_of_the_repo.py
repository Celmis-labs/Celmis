"""A module name from feature detection is one plain folder of the repo, never `..`.

THE DEFECT. `_module_file_locations` turned each module name into
`repo_path / name` and walked it. The names are produced by a model, and `..`
is a name: `repo_path / ".."` is the folder holding EVERY clone, so the feature
note's fallback read source files of other repositories (other tenants' on a
shared server) and put them in the prompt. A link in the repo and a secret
file were walked as well.

THE CONTRACT. `..`, `.`, a NUL and a backslash in a name select nothing; a link
that leaves the clone and a secret file are not listed; a real module still is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.generation.feature_doc import _module_file_locations


@pytest.fixture()
def clone(tmp_path: Path) -> Path:
    repos = tmp_path / "repos"
    repo = repos / "acme-app"
    (repo / "air").mkdir(parents=True)
    (repo / "air" / "views.py").write_text("x = 1\n")
    (repo / "air" / "secrets.yaml").write_text("token: not-a-real-token\n")
    other = repos / "other-tenant"
    other.mkdir()
    (other / "billing.py").write_text("y = 2\n")
    (repo / "air" / "peek.py").symlink_to(other / "billing.py")
    return repo


def test_a_real_module_is_still_listed(clone):
    assert [f for f, _l, _e in _module_file_locations(clone, ["air"])] == ["air/views.py"]


@pytest.mark.parametrize("name", ["..", "modules/..", ".", "../other-tenant", "air\\..\\..", "air\x00"])
def test_a_name_that_is_not_a_plain_folder_selects_nothing(clone, name):
    assert _module_file_locations(clone, [name]) == [], name


def test_a_link_out_of_the_clone_and_a_secret_file_are_not_listed(clone):
    listed = [f for f, _l, _e in _module_file_locations(clone, ["air"])]
    assert "air/peek.py" not in listed and "air/secrets.yaml" not in listed
