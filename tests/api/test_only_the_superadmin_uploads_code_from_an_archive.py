"""Adding code from an archive over HTTP: who may, what is accepted, what is left behind.

`POST /api/repos/upload` is the superadmin's: a workspace admin, a platform
admin (`is_admin`) and everybody else get 403 BEFORE a byte is read. The rest
is the contract of the route — allowed extensions, safe unpacking, slug rules,
re-upload as a new version, and that an uploaded repository is indexed
without git and left alone by everything that assumes a remote.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.api.rbac_world import A_REPO, world

ROLES_DENIED = ["gadmin", "owner_a", "admin_a", "editor_a", "member_a", "viewer_a", "loner"]


def _zip_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


PROJECT = {
    "legacy-code/main.py": "def total(items):\n    return sum(items)\n",
    "legacy-code/Модуль.bsl": "Процедура ОбработатьДокумент()\nКонецПроцедуры\n",
}


@pytest.fixture
async def up(tmp_path, monkeypatch):
    from src.config import get_settings

    queued: list[dict] = []
    audited: list[dict] = []
    import src.api.routers.repos as repos_router
    import src.repos.indexing as indexing

    def _queue(slug, **kw):
        queued.append({"slug": slug, **kw})
        return indexing.INDEX_QUEUED

    monkeypatch.setattr(indexing, "queue_index_if_needed", _queue)
    monkeypatch.setattr(repos_router, "record_action", lambda **kw: audited.append(kw))
    async with world(tmp_path, monkeypatch) as w:
        w.queued, w.audited = queued, audited
        w.root = get_settings().repos_dir
        w.db = tmp_path / "celmis.db"
        yield w


async def _upload(w, who="su", *, filename="code.zip", data=None, slug="legacy-code",
                  name="Legacy code"):
    return await w.client.post(
        "/api/repos/upload",
        data={"slug": slug, "name": name},
        files={"file": (filename, data if data is not None else _zip_bytes(PROJECT),
                        "application/octet-stream")},
        headers=w.h(who, "ws-a"),
    )


# ─── who may ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ROLES_DENIED)
async def test_nobody_but_the_superadmin_may_upload(up, who):
    r = await _upload(up, who)
    assert r.status_code == 403, (who, r.text)
    assert not (up.root / "legacy-code").exists()
    assert up.queued == [] and up.audited == []


async def test_the_superadmin_uploads_and_the_repo_is_registered_as_an_upload(up):
    r = await _upload(up)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provider"] == "upload" and body["slug"] == "legacy-code"
    assert body["display_name"] == "Legacy code" and body["index_queued"] is True
    # The single top folder was stripped; Cyrillic file names survive.
    assert (up.root / "legacy-code" / "main.py").is_file()
    assert (up.root / "legacy-code" / "Модуль.bsl").is_file()
    [job] = up.queued
    assert job["slug"] == "legacy-code" and job["force"] is True
    [row] = up.audited
    assert row["action"] == "repo.uploaded" and len(row["detail"]["sha256"]) == 64

    listed = (await up.client.get("/api/repos", headers=up.h("su", "ws-a"))).json()
    [mine] = [x for x in listed if x["slug"] == "legacy-code"]
    assert mine["provider"] == "upload" and mine["display_name"] == "Legacy code"


# ─── what is accepted ────────────────────────────────────────────────


@pytest.mark.parametrize("filename", ["code.rar", "code.7z", "code.tar", "code.tar.zst", "code"])
async def test_other_archive_types_are_refused_with_the_allowed_list(up, filename):
    r = await _upload(up, filename=filename)
    assert r.status_code == 422
    assert all(ext in r.json()["detail"] for ext in (".zip", ".tar.gz", ".tgz"))
    assert not (up.root / "legacy-code").exists()


async def test_a_tgz_is_accepted(up):
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"print(1)\n"
        info = tarfile.TarInfo("proj/a.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    r = await _upload(up, filename="code.tgz", data=buf.getvalue())
    assert r.status_code == 201, r.text
    assert (up.root / "legacy-code" / "a.py").is_file()


async def test_zip_slip_over_http_is_refused_and_nothing_is_registered(up):
    r = await _upload(up, data=_zip_bytes({"ok.py": "1", "../../evil.py": "x"}))
    assert r.status_code == 422 and "leaves its folder" in r.json()["detail"]
    assert not (up.root / "legacy-code").exists()
    listed = (await up.client.get("/api/repos", headers=up.h("su", "ws-a"))).json()
    assert "legacy-code" not in [x["slug"] for x in listed]
    assert up.queued == []


async def test_an_oversized_upload_is_refused_with_413(up, monkeypatch):
    monkeypatch.setenv("CELMIS_MAX_UPLOAD_BYTES", "200")
    r = await _upload(up, data=_zip_bytes({"a.txt": "x" * 5000}))
    assert r.status_code == 413, r.text
    assert not (up.root / "legacy-code").exists()


@pytest.mark.parametrize("slug", ["../x", "a/b", "", ".", "..", "has space"])
async def test_a_slug_that_is_not_one_path_segment_is_refused(up, slug):
    r = await _upload(up, slug=slug)
    assert r.status_code == 422, (slug, r.text)


async def test_a_slug_of_a_git_repo_is_not_taken_over(up):
    from src.api.auto_review import RepoConfig, get_auto_review_store

    get_auto_review_store().upsert(RepoConfig(
        user_id="u", repo_slug="taken", provider="github", full_name="o/taken",
        url="github:o/taken", workspace_id="wsid-a"))
    r = await _upload(up, slug="taken")
    assert r.status_code == 409 and not (up.root / "taken").exists()


async def test_a_slug_used_in_another_workspace_is_refused(up):
    from src.api.auto_review import RepoConfig, get_auto_review_store

    get_auto_review_store().upsert(RepoConfig(
        user_id="u", repo_slug="theirs", provider="upload", full_name="upload/theirs",
        url="upload:theirs", workspace_id="wsid-b"))
    r = await _upload(up, slug="theirs")
    assert r.status_code == 409


async def test_uploading_again_under_the_same_slug_is_a_new_version(up):
    assert (await _upload(up)).status_code == 201
    r = await _upload(up, data=_zip_bytes({"v2/new.py": "x = 2\n"}))
    assert r.status_code == 201
    assert (up.root / "legacy-code" / "new.py").is_file()
    assert not (up.root / "legacy-code" / "main.py").exists()
    assert len(up.queued) == 2 and all(j["force"] for j in up.queued)
    assert [a["detail"]["replaced"] for a in up.audited] == [False, True]


# ─── an uploaded repo has no remote ──────────────────────────────────


async def test_an_uploaded_repo_indexes_without_git_and_records_the_archive_hash(up, monkeypatch):
    from sqlalchemy import create_engine

    import src.repos.index_state as index_state
    from src.config import get_settings
    from src.repos.index_state import read_index_state
    from src.repos.indexing import index_repo_sync

    monkeypatch.setattr(index_state, "_engine",
                        lambda: create_engine(f"sqlite:///{up.db}"))
    assert (await _upload(up)).status_code == 201
    sha = up.audited[0]["detail"]["sha256"]
    ws_id = up.ws["ws-a"]
    result = index_repo_sync("legacy-code", user_id=up.uid("su"), workspace_id=ws_id)
    assert result.symbols >= 1
    assert get_settings().repo_graph_path("legacy-code").exists()
    assert not (up.root / "legacy-code" / ".git").exists()
    state = read_index_state("legacy-code")
    assert state is not None and state.last_indexed_sha == sha


async def test_freshness_the_incremental_pass_and_webhooks_leave_an_upload_alone(up):
    from src.repos.freshness import check_repo
    from src.sync.incremental import run_index

    assert (await _upload(up)).status_code == 201
    ws_id = up.ws["ws-a"]
    check = check_repo("legacy-code", workspace_id=ws_id, user_id=up.uid("su"))
    assert check.state == "not_applicable" and check.known is False
    assert run_index("legacy-code")["status"] == "skipped"
    for method, path in (("post", "/api/repos/legacy-code/webhook"),
                         ("get", "/api/repos/legacy-code/webhook")):
        r = await getattr(up.client, method)(path, headers=up.h("su", "ws-a"))
        assert r.status_code == 400, (path, r.text)
    r = await up.client.get("/api/repos/legacy-code/pulls", headers=up.h("su", "ws-a"))
    assert r.status_code == 400


async def test_a_git_repo_still_reports_a_regular_freshness_state(up):
    """The upload shortcut must not swallow ordinary repositories."""
    from src.repos.freshness import _is_upload

    assert _is_upload(A_REPO, up.ws["ws-a"], up.uid("su")) is False
