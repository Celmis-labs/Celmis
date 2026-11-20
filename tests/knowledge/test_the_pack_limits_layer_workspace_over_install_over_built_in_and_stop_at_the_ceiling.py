"""The resolver of the pack limits, and what reads it.

The contract (src/knowledge/limits.py): workspace override > install variable > built-in,
every value clamped to a ceiling; the archive ceiling is the operator's
(`CELMIS_PACK_MAX_ARCHIVE_CEILING_BYTES`) and the pack-upload body cap follows it; ingest
holds an upload to the limits it is given.
"""

from __future__ import annotations

import pytest

from src.api import middleware
from src.db.models import WorkspacePackLimits
from src.knowledge import ingest, limits
from tests.knowledge.pack_rig import A, B

MB = 1024 * 1024


def test_with_nothing_set_the_limits_are_the_built_in_ones(monkeypatch):
    for name in (*limits.ENV_NAMES.values(), limits.CEILING_ENV):
        monkeypatch.delenv(name, raising=False)
    assert limits.install_defaults() == limits.BUILTIN
    assert limits.BUILTIN.as_dict() == {
        "archive_bytes": 5 * MB, "max_files": 400, "file_bytes": 512 * 1024,
        "total_bytes": 8 * MB, "packs_per_workspace": 200, "keep_versions": 20}


def test_an_install_variable_replaces_the_built_in_and_a_bad_one_is_ignored(monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_MAX_FILES", "1000")
    monkeypatch.setenv("CELMIS_PACK_KEEP_VERSIONS", "many")
    monkeypatch.setenv("CELMIS_PACK_MAX_PACKS", "0")
    got = limits.install_defaults()
    assert got.max_files == 1000
    assert got.keep_versions == limits.BUILTIN.keep_versions
    assert got.packs_per_workspace == limits.BUILTIN.packs_per_workspace


def test_no_layer_can_pass_the_ceiling(monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_MAX_FILES", "999999")
    assert limits.install_defaults().max_files == 5000
    with pytest.raises(limits.LimitError):
        limits.validate({"max_files": 5001})
    assert limits.validate({"max_files": 5000, "keep_versions": None}) == {
        "max_files": 5000, "keep_versions": None}


def test_the_operator_may_move_the_archive_ceiling_and_the_body_cap_follows(monkeypatch):
    monkeypatch.delenv(limits.CEILING_ENV, raising=False)
    assert limits.ceilings().archive_bytes == 5 * MB
    assert middleware._body_caps()["pack_upload"] == 5 * MB      # the large cap, as before
    monkeypatch.setenv(limits.CEILING_ENV, str(20 * MB))
    assert middleware._body_caps()["pack_upload"] == 21 * MB
    with pytest.raises(limits.LimitError):
        limits.validate({"archive_bytes": 21 * MB})


async def test_a_workspace_override_wins_over_the_install_and_reads_back(world, monkeypatch):
    monkeypatch.setenv("CELMIS_PACK_MAX_FILES", "600")
    async with world.session() as s:
        s.add(WorkspacePackLimits(workspace_id=A, max_files=50, keep_versions=3))
        await s.commit()
    got = limits.resolve_sync(None, A)
    assert (got.max_files, got.keep_versions, got.file_bytes) == (50, 3, 512 * 1024)
    other = limits.resolve_sync(None, B)
    assert (other.max_files, other.keep_versions) == (600, 20)
    async with world.session() as s:
        full = await limits.resolve_full(s, A)
    assert full.sources["max_files"] == "workspace" and full.sources["file_bytes"] == "builtin"


def test_ingest_holds_an_upload_to_the_limits_it_is_given():
    files = [("README.md", b"# a\n"), ("a.md", b"# a\n"), ("b.md", b"# b\n")]
    small = limits.PackLimits(archive_bytes=MB, max_files=2, file_bytes=1024, total_bytes=MB,
                              packs_per_workspace=5, keep_versions=2)
    with pytest.raises(ingest.IngestError) as raised:
        ingest.ingest_files(files, small)
    assert raised.value.code == "too_many_files"
    assert len(ingest.ingest_files(files).files) == 3                   # the built-in: fine
    big = [("README.md", b"# a\n"), ("big.md", b"# big\n" + b"x" * 2000)]
    with pytest.raises(ingest.IngestError) as raised:
        ingest.ingest_files(big, small)
    assert raised.value.code == "file_too_large"
    roomy = limits.PackLimits(**{**small.as_dict(), "file_bytes": 4096})
    assert len(ingest.ingest_files(big, roomy).files) == 2
