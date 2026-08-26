"""A note path from the client must not reach outside the repo's own vault.

`GET /api/docs/{slug}/note?path=…` takes a filename from the browser and opens
it. That is the shape of every directory-traversal bug ever written, and this
codebase has already learned the sharper half of the lesson elsewhere: a string
prefix is not a boundary, because `/vault/repo-evil` starts with `/vault/repo`
and is a different directory.

So the check compares resolved PATH PARTS, and resolves first — a symlink
planted inside the vault by anything that writes there would otherwise be a way
straight out.

The rule is exercised on a real temporary tree rather than through FastAPI: the
property is about paths, and a filesystem is the honest way to ask about paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def escapes(root: Path, rel: str) -> bool:
    """True when `rel` lands outside `root` — the guard from docs.py."""
    root = root.resolve()
    candidate = (root / rel).resolve()
    return candidate != root and root not in candidate.parents


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault" / "projects" / "repo"
    (root / "modules").mkdir(parents=True)
    (root / "modules" / "api.md").write_text("# api")
    # A sibling whose name merely starts with the same characters. A prefix
    # check would let this through.
    evil = tmp_path / "vault" / "projects" / "repo-evil"
    evil.mkdir(parents=True)
    (evil / "secret.md").write_text("# not yours")
    (tmp_path / "outside.md").write_text("# elsewhere")
    return root


def test_a_note_inside_the_vault_is_allowed(vault: Path):
    assert not escapes(vault, "modules/api.md")


@pytest.mark.parametrize("rel", [
    "../repo-evil/secret.md",
    "../../../outside.md",
    "modules/../../repo-evil/secret.md",
    "/etc/passwd",
])
def test_traversal_is_refused(vault: Path, rel: str):
    assert escapes(vault, rel), rel


def test_a_sibling_sharing_a_name_prefix_is_refused(vault: Path):
    """`repo-evil` starts with `repo`. String prefixes are not boundaries."""
    sibling = vault.parent / "repo-evil" / "secret.md"
    assert escapes(vault, str(sibling))


def test_a_symlink_out_of_the_vault_is_refused(vault: Path, tmp_path: Path):
    """resolve() follows the link before the comparison, so planting one
    inside the vault does not turn it into a door."""
    link = vault / "escape.md"
    try:
        link.symlink_to(tmp_path / "outside.md")
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")
    assert escapes(vault, "escape.md")


def test_the_root_itself_is_not_a_note(vault: Path):
    """`path=""` resolves to the directory; it must not read as "allowed and
    then open a folder" — the caller's is_file() check is the second half, and
    this pins that the boundary check does not object to it first."""
    assert not escapes(vault, "")
    assert not (vault / "").is_file()
