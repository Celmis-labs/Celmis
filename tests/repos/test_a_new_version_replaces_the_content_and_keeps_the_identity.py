"""A new archive for an uploaded repository: same name, new content, swapped last.

The new version is unpacked and indexed in a staging workspace; the served files,
graph, grep index and archive record are replaced only when that worked. A
refusal or a failed index leaves the old version exactly as it was. Nothing of
the old version that the new one drops is left to be found.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from src.repos import upload, upload_versions
from src.repos.upload import StoredArchive
from src.repos.upload_versions import UpdateError, apply_new_version

SLUG = "billing"
OLD_ONLY = "legacy_total_marker_81f2"
NEW_ONLY = "fresh_total_marker_4c09"

V1 = {
    "app/main.py": f"def {OLD_ONLY}():\n    return 1\n\n\ndef shared_entry():\n    return 2\n",
    "app/legacy.py": "def legacy_helper():\n    return 3\n",
    "README.txt": "same in both\n",
}
V2 = {
    "app/main.py": f"def {NEW_ONLY}():\n    return 10\n\n\ndef shared_entry():\n    return 2\n",
    "app/extra.py": "def extra_helper():\n    return 4\n",
    "README.txt": "same in both\n",
}


def _archive(tmp_path: Path, name: str, files: dict[str, str]) -> StoredArchive:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(f"{SLUG}-src/{rel}", text)
    data = path.read_bytes()
    return StoredArchive(path, hashlib.sha256(data).hexdigest(), len(data))


@pytest.fixture
def served(tmp_path, monkeypatch):
    """An uploaded repository at version 1, indexed the way the worker does."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    from src.config import get_settings

    get_settings.cache_clear()
    first = _archive(tmp_path, "v1.zip", V1)
    upload.install_archive(first, "v1.zip", SLUG, uploaded_by="owner@acme.example")
    _index_live(SLUG)
    yield get_settings()
    get_settings.cache_clear()


def _index_live(slug: str) -> None:
    from src.config import get_settings
    from src.groups.indexer import GroupIndexer
    from src.groups.models import RepoGroup

    GroupIndexer(group=RepoGroup(name="_t", repos=[f"upload:{slug}"]),
                 settings=get_settings())._index_one_repo(f"upload:{slug}", None)


def _names(slug: str) -> set[str]:
    from src.config import get_settings
    from src.indexing.graph.graph_store import make_graph_store

    store = make_graph_store(get_settings().repo_graph_path(slug))
    try:
        return {r["n"] for r in store.query("MATCH (s:Symbol) RETURN s.name AS n")}
    finally:
        store.close()


def _grep(slug: str, needle: str) -> list[str]:
    from src.mcp_server.dev_profile import git_io

    sha = upload.upload_revision(slug)
    return [h.path for h in git_io.grep(slug, sha, needle)]


def test_the_first_upload_is_version_one_with_its_facts(served):
    (v1,) = upload_versions.read_versions(SLUG)
    assert (v1["version"], v1["uploaded_by"], v1["filename"], v1["files"]) == (
        1, "owner@acme.example", "v1.zip", 3)
    assert v1["sha256"] == upload.upload_revision(SLUG) and v1["size"] > 0


def test_a_new_version_replaces_files_graph_and_search_index(served, tmp_path):
    assert OLD_ONLY in _names(SLUG) and "legacy_helper" in _names(SLUG)
    assert _grep(SLUG, OLD_ONLY) == ["app/main.py"]

    new = _archive(tmp_path, "v2.zip", V2)
    out = apply_new_version(new, "v2.zip", SLUG, uploaded_by="owner@acme.example")

    assert out.version["version"] == 2
    assert (out.added, out.removed, out.changed) == (1, 1, 1)
    assert out.removed_paths == ["app/legacy.py"]
    tree = served.repo_path(SLUG)
    assert not (tree / "app" / "legacy.py").exists() and (tree / "app" / "extra.py").is_file()
    # What the old version had and the new one lacks cannot be found any more.
    names = _names(SLUG)
    assert OLD_ONLY not in names and "legacy_helper" not in names
    assert NEW_ONLY in names and "extra_helper" in names
    assert _grep(SLUG, OLD_ONLY) == [] and _grep(SLUG, "legacy_helper") == []
    assert _grep(SLUG, NEW_ONLY) == ["app/main.py"]
    # The record names the new archive; the history keeps both.
    assert upload.upload_revision(SLUG) == new.sha256
    assert [v["version"] for v in upload_versions.read_versions(SLUG)] == [1, 2]
    assert upload_versions.current_version(SLUG)["files"] == 3
    # Nothing is left behind in the work area.
    assert not any((served.workspace_dir / "tmp").iterdir())


def test_the_display_name_survives_a_new_version(served, tmp_path):
    upload.write_display_name(SLUG, "Billing service")
    apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    assert upload.read_upload_meta(SLUG)["name"] == "Billing service"
    assert upload.read_upload_meta(SLUG)["version"] == 2


def test_the_same_archive_twice_is_refused_not_counted_as_a_version(served, tmp_path):
    new = _archive(tmp_path, "v2.zip", V2)
    apply_new_version(new, "v2.zip", SLUG)
    again = _archive(tmp_path, "v2.zip", V2)
    with pytest.raises(UpdateError) as exc:
        apply_new_version(again, "v2.zip", SLUG)
    assert exc.value.status == 409
    assert len(upload_versions.read_versions(SLUG)) == 2


def test_the_same_files_packed_again_are_refused_too(served, tmp_path):
    apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    repacked = tmp_path / "v2-again.zip"
    with zipfile.ZipFile(repacked, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.comment = b"packed again"
        for rel, text in V2.items():
            zf.writestr(f"{SLUG}-src/{rel}", text)
    data = repacked.read_bytes()
    again = StoredArchive(repacked, hashlib.sha256(data).hexdigest(), len(data))
    assert again.sha256 != upload.upload_revision(SLUG)
    with pytest.raises(UpdateError) as exc:
        apply_new_version(again, "v2-again.zip", SLUG)
    assert exc.value.status == 409
    assert len(upload_versions.read_versions(SLUG)) == 2


def _unchanged(served, before_sha: str) -> None:
    assert upload.upload_revision(SLUG) == before_sha
    assert (served.repo_path(SLUG) / "app" / "legacy.py").is_file()
    assert OLD_ONLY in _names(SLUG)
    assert _grep(SLUG, OLD_ONLY) == ["app/main.py"]
    assert len(upload_versions.read_versions(SLUG)) == 1


def test_a_hostile_archive_is_refused_and_the_old_version_keeps_serving(served, tmp_path):
    before = upload.upload_revision(SLUG)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("ok/a.py", "x = 1\n")
        zf.writestr("../escape.py", "x = 2\n")
    stored = StoredArchive(bad, "ab" * 32, bad.stat().st_size)
    with pytest.raises(UpdateError):
        apply_new_version(stored, "bad.zip", SLUG)
    _unchanged(served, before)
    assert not (served.workspace_dir / "escape.py").exists()


def test_something_that_is_not_an_archive_is_refused(served, tmp_path):
    before = upload.upload_revision(SLUG)
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip")
    with pytest.raises(UpdateError):
        apply_new_version(StoredArchive(junk, "cd" * 32, 17), "junk.zip", SLUG)
    _unchanged(served, before)


def test_an_archive_over_the_size_caps_is_refused(served, tmp_path, monkeypatch):
    before = upload.upload_revision(SLUG)
    monkeypatch.setattr(upload, "MAX_FILES", 2)
    with pytest.raises(UpdateError):
        apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    _unchanged(served, before)


def test_a_failing_index_keeps_the_old_version_and_says_why(served, tmp_path, monkeypatch):
    before = upload.upload_revision(SLUG)
    from src.groups import indexer

    def boom(self, *a, **k):  # noqa: ANN001, ARG001
        raise RuntimeError("grammar missing")

    monkeypatch.setattr(indexer.GroupIndexer, "_index_one_repo", boom)
    with pytest.raises(UpdateError) as exc:
        apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    assert "grammar missing" in str(exc.value)
    _unchanged(served, before)


def test_an_archive_with_no_code_over_a_repository_with_code_is_refused(served, tmp_path):
    before = upload.upload_revision(SLUG)
    with pytest.raises(UpdateError) as exc:
        apply_new_version(_archive(tmp_path, "docs.zip", {"notes.txt": "hello\n"}),
                          "docs.zip", SLUG)
    assert "no code" in str(exc.value)
    _unchanged(served, before)


def test_a_failure_during_the_swap_puts_everything_back(served, tmp_path, monkeypatch):
    before = upload.upload_revision(SLUG)
    real = upload_versions.os.replace
    calls = {"n": 0}

    def flaky(src, dst):  # noqa: ANN001
        # let extraction and the tree swap through, break while the data is swapped
        if Path(dst).name == "upload.json" and "ws/data" in str(dst) and not calls["n"]:
            calls["n"] += 1
            raise OSError("disk went away")
        return real(src, dst)

    monkeypatch.setattr(upload_versions.os, "replace", flaky)
    with pytest.raises(OSError):
        apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    monkeypatch.undo()
    assert calls["n"] == 1
    _unchanged(served, before)


def test_the_vault_notes_of_a_removed_file_go_with_it(served, tmp_path):
    vault = served.repo_vault_path(SLUG)
    for rel, path in (("modules/legacy.md", "app/legacy.py"), ("modules/main.md", "app/main.py")):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(f"---\ntype: module\nrepo: {SLUG}\npath: {path}\n---\nbody\n",
                                 encoding="utf-8")
    out = apply_new_version(_archive(tmp_path, "v2.zip", V2), "v2.zip", SLUG)
    assert out.notes_removed == 1
    assert not (vault / "modules" / "legacy.md").exists()
    assert (vault / "modules" / "main.md").is_file()


def test_the_history_file_is_json_a_person_can_read(served):
    raw = json.loads((served.repo_data_path(SLUG) / "versions.json").read_text("utf-8"))
    assert raw[0]["version"] == 1
