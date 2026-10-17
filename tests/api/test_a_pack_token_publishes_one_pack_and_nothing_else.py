"""A pack publish token (`cpk_…`) lets a script publish ONE pack, and nothing else.

The contract (docs/CODEBASE.md, "Publishing from a laptop or a pipeline"):

  * a maintainer of a pack makes, lists and revokes its tokens (a reader gets the
    maintainer 403, a pack the caller cannot see is the same 404 as a missing
    one); the raw token is in the creating response and nowhere else; at most ten
    are active; the audit rows carry ids and the label, never the token;
  * `GET/POST /api/codebase/publish` and `GET /api/codebase/publish/archive` take
    that token and no other credential: a login, an MCP token, a project token,
    a pack invite or a made-up string all get the same 401 body;
  * every call asks again, so the token stops working the moment it is revoked or
    expires, the moment its maker loses the right to MAINTAIN the pack (removed
    from the workspace or the team, demoted, the audience changed under them,
    switched off), and every one of those is the identical 401 body;
  * the token opens one pack: publishing with it changes that pack and no other,
    and a pack of another workspace is out of its reach;
  * the publish upload is the pack page's upload (same checks, same 422 for a
    refusal, never a secret's value), plus a dry run that stores nothing and an
    "unchanged" answer that does not spend one of the 20 retained versions;
  * the token is accepted by no other route, by `get_current_user`, by the MCP
    servers or by the project-token path.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.db.models import KnowledgePackMember, KnowledgePackToken, KnowledgePackVersion
from tests.api.codebase_support import call, note_zip, upload_zip, zip_of
from tests.api.rbac_world import B_PACK_ID, B_PUBLISH_TOKEN, world

NOT_VALID = (401, b'{"detail":"invalid or expired pack token"}')
SKILL = ("---\nname: billing-api\ndescription: How the billing API is called\n---\n"
         "# Billing\n\nCall the endpoint with the key from the BILLING_KEY variable.\n")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def publish(w, token, method="GET", path="/api/codebase/publish", **kw):
    return await w.client.request(method, path, headers=bearer(token), **kw)


def skill_zip(extra: dict[str, str] | None = None) -> bytes:
    # a root file keeps the one top folder from being stripped as a wrapper
    return zip_of({"README.md": "# Ledger\n", "billing-api/SKILL.md": SKILL,
                   "billing-api/ref.md": "# Ref\n", **(extra or {})})


async def make_pack(w, who="editor_a", slug="ledger", audience="teams", teams=None) -> str:
    r = await call(w, who, "POST", "/api/codebase/packs", json={
        "slug": slug, "title": f"Pack {slug}", "audience": audience, "teams": teams or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_token(w, pid, who="editor_a", label="ci", days=None) -> tuple[str, dict]:
    body = {"label": label}
    if days is not None:
        body["expires_in_days"] = days
    r = await call(w, who, "POST", f"/api/codebase/packs/{pid}/tokens", json=body)
    assert r.status_code == 201, r.text
    return r.json()["token"], r.json()["token_info"]


async def ready(w, **kw) -> tuple[str, str]:
    """A pack with a token, as its creator `editor_a`."""
    pid = await make_pack(w, **kw)
    token, _ = await make_token(w, pid)
    return pid, token


async def versions(w, pid) -> list[int]:
    r = await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
    return [v["number"] for v in r.json()["versions"]]


# ─── Managing tokens ─────────────────────────────────────────────────


async def test_a_maintainer_makes_lists_and_revokes_tokens_and_sees_the_raw_one_once(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/tokens",
                       json={"label": "  nightly build  ", "expires_in_days": 30})
        assert r.status_code == 201
        body = r.json()
        token, info = body["token"], body["token_info"]
        assert token.startswith("cpk_") and len(token) >= 40
        assert set(info) == {"id", "label", "created_at", "expires_at", "last_used_at",
                             "revoked", "created_by"}
        assert (info["label"], info["revoked"], info["last_used_at"]) == ("nightly build", False, None)
        assert info["created_by"] == w.users["editor_a"].email
        days = (datetime.fromisoformat(info["expires_at"]).replace(tzinfo=UTC)
                - datetime.now(UTC)).total_seconds() / 86400
        assert 29 < days <= 30

        listed = await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/tokens")
        assert listed.status_code == 200
        assert [t["id"] for t in listed.json()["tokens"]] == [info["id"]]
        assert token not in listed.text and hashlib.sha256(token.encode()).hexdigest() not in listed.text
        assert list(listed.json()) == ["tokens"]

        gone = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}/tokens/{info['id']}")
        assert gone.status_code == 204 and gone.content == b""
        again = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}/tokens/{info['id']}")
        assert again.status_code == 204                      # revoking twice is not an error
        after = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/tokens")).json()
        assert [t["revoked"] for t in after["tokens"]] == [True]


async def test_the_default_life_is_ninety_days_and_the_bounds_are_one_to_three_sixty_five(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        _, info = await make_token(w, pid)
        days = (datetime.fromisoformat(info["expires_at"]).replace(tzinfo=UTC)
                - datetime.now(UTC)).total_seconds() / 86400
        assert 89 < days <= 90
        url = f"/api/codebase/packs/{pid}/tokens"
        for body in ({"label": "x", "expires_in_days": 0}, {"label": "x", "expires_in_days": 366},
                     {"label": ""}, {"label": "y" * 81}, {}, {"label": "x", "extra": 1},
                     {"label": "x", "expires_in_days": "soon"}):
            r = await call(w, "editor_a", "POST", url, json=body)
            assert r.status_code == 422, body
        ok = await call(w, "editor_a", "POST", url, json={"label": "y" * 80, "expires_in_days": 365})
        assert ok.status_code == 201
        blank = await call(w, "editor_a", "POST", url, json={"label": "   "})
        assert blank.status_code == 422


async def test_a_pack_holds_at_most_ten_active_tokens_and_a_revoked_one_makes_room(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        made = [await make_token(w, pid, label=f"t{i}") for i in range(10)]
        url = f"/api/codebase/packs/{pid}/tokens"
        full = await call(w, "editor_a", "POST", url, json={"label": "eleventh"})
        assert full.status_code == 409
        assert "at most 10" in full.json()["detail"]
        await call(w, "editor_a", "DELETE", f"{url}/{made[0][1]['id']}")
        assert (await call(w, "editor_a", "POST", url, json={"label": "eleventh"})).status_code == 201
        # an expired token is not active either
        async with w.factory() as s:
            row = await s.get(KnowledgePackToken, made[1][1]["id"])
            row.expires_at = datetime.now(UTC) - timedelta(hours=1)
            await s.commit()
        assert (await call(w, "editor_a", "POST", url, json={"label": "twelfth"})).status_code == 201


async def test_only_a_maintainer_manages_tokens_and_a_hidden_pack_is_a_missing_one(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        # `member_a` reads a workspace pack; `admin_a` maintains it as a workspace admin
        shared = await make_pack(w, slug="shared", audience="workspace")
        _, info = await make_token(w, shared)
        url = f"/api/codebase/packs/{shared}/tokens"
        calls = [("GET", url, None), ("POST", url, {"label": "x"}),
                 ("DELETE", f"{url}/{info['id']}", None)]
        for who in ("member_a", "viewer_a"):
            for method, path, body in calls:
                r = await call(w, who, method, path, json=body)
                assert (r.status_code, r.json()["detail"]) == (403, "Requires a pack maintainer"), (who, method)
        for who in ("admin_a", "owner_a", "gadmin", "su"):
            assert (await call(w, who, "GET", url)).status_code == 200, who
        # someone with no place in the workspace is answered in their own space,
        # where this pack does not exist: the 404 of a missing one
        for method, path, body in calls:
            r = await call(w, "loner", method, path, json=body)
            assert (r.status_code, r.json()["detail"]) == (404, "pack not found or not accessible")

        # a private pack: invisible to admins, so its tokens answer as a missing pack's do
        private = await make_pack(w, slug="private", audience="restricted")
        missing = "6b1f0c5e-0000-4000-8000-0000000000ff"
        for who in ("admin_a", "owner_a", "gadmin", "member_a"):
            for method, tail, body in (("GET", "tokens", None), ("POST", "tokens", {"label": "x"}),
                                       ("DELETE", f"tokens/{info['id']}", None)):
                hidden = await call(w, who, method, f"/api/codebase/packs/{private}/{tail}", json=body)
                absent = await call(w, who, method, f"/api/codebase/packs/{missing}/{tail}", json=body)
                assert (hidden.status_code, hidden.content) == (absent.status_code, absent.content), (who, method)
                assert hidden.status_code == 404


async def test_a_token_id_of_another_pack_is_not_found_and_is_not_revoked(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        one = await make_pack(w, slug="one")
        two = await make_pack(w, slug="two")
        token, info = await make_token(w, one)
        r = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{two}/tokens/{info['id']}")
        assert (r.status_code, r.json()["detail"]) == (404, "token not found")
        junk = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{two}/tokens/not-an-id")
        assert (junk.status_code, junk.json()["detail"]) == (404, "token not found")
        assert (await publish(w, token)).status_code == 200


async def test_the_audit_names_the_token_and_its_label_and_never_the_secret(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        token, info = await make_token(w, pid, label="release job")
        await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip(), "application/zip")})
        await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}/tokens/{info['id']}")
        rows = {r["action"]: r for r in w.audit if r["action"].startswith("codebase.")}
        created, revoked = rows["codebase.token_created"], rows["codebase.token_revoked"]
        for row in (created, revoked):
            assert row["target"] == pid and row["workspace_id"] == w.ws["ws-a"]
            assert row["actor_id"] == w.uid("editor_a")
            assert row["detail"] == {"token_id": info["id"], "label": "release job"}
        pushed = rows["codebase.version_uploaded"]
        assert pushed["actor_id"] == w.uid("editor_a") and pushed["actor"] == w.users["editor_a"].email
        assert pushed["detail"] == {"number": 1, "file_count": 3, "total_bytes": pushed["detail"]["total_bytes"],
                                    "allowed_count": 0, "token_id": info["id"]}
        assert token not in repr(w.audit) and hashlib.sha256(token.encode()).hexdigest() not in repr(w.audit)


# ─── The token at rest ───────────────────────────────────────────────


async def test_only_the_hash_of_a_token_is_stored(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        async with w.factory() as s:
            rows = (await s.scalars(select(KnowledgePackToken).where(
                KnowledgePackToken.pack_id == pid))).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert token not in {str(getattr(row, c.name)) for c in row.__table__.columns}
            assert (row.workspace_id, row.created_by_id) == (w.ws["ws-a"], w.uid("editor_a"))


# ─── What a token publishes ──────────────────────────────────────────


async def test_status_before_the_first_version_says_so_and_after_it_lists_every_file(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        before = await publish(w, token)
        assert before.status_code == 200
        body = before.json()
        assert set(body) == {"pack", "current_version", "files", "usage"}
        assert body["pack"] == {"id": pid, "slug": "ledger", "title": "Pack ledger", "audience": "teams"}
        assert (body["current_version"], body["files"]) == (None, [])
        assert body["usage"] == {"calls_30d": 0, "last_used_at": None, "skills": []}

        up = await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip(), "application/zip")},
                           data={"note": "first"})
        assert up.status_code == 201, up.text
        up_body = up.json()
        assert up_body["version"]["number"] == 1 and up_body["version"]["current"] is True
        assert up_body["skills"] == ["billing-api"] and up_body["ignored"] == []
        after = (await publish(w, token)).json()
        assert after["current_version"]["number"] == 1 and after["current_version"]["file_count"] == 3
        assert after["current_version"]["sha"] == up_body["version"]["sha"]
        files = {f["path"]: f for f in after["files"]}
        assert set(files) == {"README.md", "billing-api/SKILL.md", "billing-api/ref.md"}
        skill = files["billing-api/SKILL.md"]
        assert skill["sha"] == hashlib.sha256(SKILL.encode()).hexdigest()
        assert (skill["kind"], skill["skill"], skill["size_bytes"]) == ("skill", "billing-api", len(SKILL.encode()))
        assert after["usage"]["skills"] == [{"skill": "billing-api", "opens_30d": 0, "last_used_at": None}]


async def test_the_same_files_again_are_unchanged_and_cost_no_version(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        archive = {"archive": ("p.zip", skill_zip(), "application/zip")}
        first = await publish(w, token, "POST", files=archive)
        assert first.status_code == 201
        again = await publish(w, token, "POST", files=archive, data={"note": "same"})
        assert again.status_code == 200
        assert again.json()["unchanged"] is True and again.json()["version"]["number"] == 1
        assert await versions(w, pid) == [1]
        changed = await publish(w, token, "POST", files={"archive": (
            "p.zip", skill_zip({"billing-api/more.md": "# More\n"}), "application/zip")})
        assert changed.status_code == 201 and changed.json()["version"]["number"] == 2
        assert sorted(await versions(w, pid)) == [1, 2]
        # after a rollback the files of version 2 are NOT the current ones: a push stores them again
        await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions/1/activate")
        third = await publish(w, token, "POST", files={"archive": (
            "p.zip", skill_zip({"billing-api/more.md": "# More\n"}), "application/zip")})
        assert third.status_code == 201 and third.json()["version"]["number"] == 3


async def test_a_dry_run_answers_with_the_files_and_stores_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        before = len(w.audit)
        for flag in ("true", "TRUE", "1"):
            r = await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip(
                {".hidden/x.md": "x", "logo.png": "not text"}), "application/zip")},
                data={"dry_run": flag})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["dry_run"] is True and body["skills"] == ["billing-api"]
            assert sorted(f["path"] for f in body["files"]) == [
                "README.md", "billing-api/SKILL.md", "billing-api/ref.md"]
            assert sorted(body["ignored"]) == [".hidden/x.md", "logo.png"]
            assert len(body["sha"]) == 64
        assert await versions(w, pid) == []
        assert (await publish(w, token)).json()["current_version"] is None
        assert [r for r in w.audit[before:] if r["action"] == "codebase.version_uploaded"] == []
        # any other value is a real upload
        real = await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip(), "application/zip")},
                             data={"dry_run": "false"})
        assert real.status_code == 201 and await versions(w, pid) == [1]


async def test_loose_files_with_their_paths_publish_like_a_zip(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        r = await publish(w, token, "POST",
                          files=[("files", ("README.md", b"# Ledger\n", "text/markdown")),
                                 ("files", ("SKILL.md", SKILL.encode(), "text/markdown")),
                                 ("files", ("ref.md", b"# Ref\n", "text/markdown"))],
                          data={"paths": ["README.md", "billing-api/SKILL.md", "billing-api/ref.md"]})
        assert r.status_code == 201, r.text
        assert r.json()["skills"] == ["billing-api"]
        both = await publish(w, token, "POST", files=[
            ("archive", ("p.zip", skill_zip(), "application/zip")), ("files", ("a.md", b"# A\n", "text/markdown"))])
        assert both.status_code == 422 and both.json()["detail"]["code"] == "bad_upload"
        none = await publish(w, token, "POST", data={"note": "x"})
        assert none.status_code == 422 and none.json()["detail"]["code"] == "bad_upload"


async def test_a_refused_upload_is_the_same_422_as_on_the_pack_page_and_never_echoes_a_value(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        value = "AKIA" + "ABCDEFGHIJKLMNOP"                         # an AWS-style key id
        leak = zip_of({"README.md": "# Ledger\n", "billing-api/SKILL.md": SKILL,
                       "billing-api/notes.md": f"key = {value}\n"})
        via_token = await publish(w, token, "POST", files={"archive": ("p.zip", leak, "application/zip")})
        via_page = await upload_zip(w, "editor_a", pid, leak)
        assert via_token.status_code == via_page.status_code == 422
        assert via_token.json() == via_page.json()
        detail = via_token.json()["detail"]
        assert detail["code"] == "secrets_found"
        assert [(h["path"], h["line"]) for h in detail["hits"]] == [("billing-api/notes.md", 1)]
        assert value not in via_token.text
        assert await versions(w, pid) == []
        no_zip = await publish(w, token, "POST", files={"archive": ("p.zip", b"not a zip", "application/zip")})
        assert no_zip.status_code == 422 and no_zip.json()["detail"]["code"] == "bad_archive"


async def test_the_archive_route_returns_the_current_version_as_a_zip(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        empty = await publish(w, token, path="/api/codebase/publish/archive")
        assert (empty.status_code, empty.json()["detail"]) == (404, "version not found")
        await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip(), "application/zip")})
        await publish(w, token, "POST", files={"archive": ("p.zip", skill_zip({"x.md": "# X\n"}), "application/zip")})
        got = await publish(w, token, path="/api/codebase/publish/archive")
        assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
        assert got.headers["content-disposition"] == 'attachment; filename="ledger-v2.zip"'
        with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
            assert sorted(zf.namelist()) == ["README.md", "billing-api/SKILL.md", "billing-api/ref.md", "x.md"]
            assert zf.read("billing-api/SKILL.md").decode() == SKILL
        await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/versions/1/activate")
        older = await publish(w, token, path="/api/codebase/publish/archive")
        assert older.headers["content-disposition"].endswith('ledger-v1.zip"')


async def test_using_a_token_marks_it_used_and_the_page_can_see_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        assert (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/tokens")).json()[
            "tokens"][0]["last_used_at"] is None
        await publish(w, token)
        used = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/tokens")).json()["tokens"][0]
        assert used["last_used_at"] is not None
        moment = datetime.fromisoformat(used["last_used_at"])
        assert abs((datetime.now(UTC) - moment.replace(tzinfo=moment.tzinfo or UTC)).total_seconds()) < 60


async def test_a_token_publishes_the_pack_it_was_made_for_and_no_other(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        one, token_one = await ready(w, slug="one")
        two, token_two = await ready(w, slug="two")
        await publish(w, token_one, "POST", files={"archive": ("p.zip", note_zip("one"), "application/zip")})
        assert await versions(w, one) == [1] and await versions(w, two) == []
        assert (await publish(w, token_two)).json()["pack"]["id"] == two
        assert (await publish(w, token_one)).json()["pack"]["id"] == one
        # the pack is named by the token, not by anything the caller sends
        spoof = await w.client.get(f"/api/codebase/publish?pack_id={two}", headers=bearer(token_one))
        assert spoof.json()["pack"]["id"] == one
        spoof = await w.client.get("/api/codebase/publish", headers={**bearer(token_one), "X-Workspace": "ws-b"})
        assert spoof.json()["pack"]["id"] == one


async def test_a_token_of_one_workspace_cannot_reach_another(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        _, token_a = await ready(w)
        mine = (await publish(w, token_a)).json()["pack"]["id"]
        assert mine != B_PACK_ID
        up = await publish(w, token_a, "POST", files={"archive": ("p.zip", skill_zip(), "application/zip")})
        assert up.status_code == 201
        async with w.factory() as s:
            n = await s.scalar(select(func.count()).select_from(KnowledgePackVersion)
                               .where(KnowledgePackVersion.pack_id == B_PACK_ID))
            assert n == 1                                            # B's pack is as it was seeded
        # and B's own token reaches B's pack only
        got = await publish(w, B_PUBLISH_TOKEN)
        assert got.status_code == 200 and got.json()["pack"]["id"] == B_PACK_ID


# ─── Every way a token stops working is the same answer ──────────────


async def test_nothing_but_a_live_pack_token_passes_the_gate_and_every_refusal_reads_alike(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        calls = [("GET", "/api/codebase/publish", {}), ("GET", "/api/codebase/publish/archive", {}),
                 ("POST", "/api/codebase/publish", {"files": {"archive": ("p.zip", skill_zip(), "application/zip")}})]
        wrong = {
            "none": {},
            "empty": {"Authorization": ""},
            "scheme": {"Authorization": f"Basic {token}"},
            "empty bearer": {"Authorization": "Bearer "},
            "prefix only": {"Authorization": "Bearer cpk_"},
            "made up": {"Authorization": "Bearer cpk_" + "a" * 43},
            "no prefix": {"Authorization": f"Bearer {token[4:]}"},
            "upper prefix": {"Authorization": f"Bearer CPK_{token[4:]}"},
            "truncated": {"Authorization": f"Bearer {token[:-1]}"},
            "padded": {"Authorization": f"Bearer {token}x"},
            "very long": {"Authorization": "Bearer cpk_" + "a" * 5000},
            "non-ascii": {"Authorization": ("Bearer cpk_é" + "a" * 40).encode("utf-8")},
            "a login": {"X-Test-User": "editor_a", "X-Workspace": "ws-a"},
            "a session jwt": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc"},
            "an mcp project token": {"Authorization": "Bearer cmcp_" + "b" * 43},
            "a pack invite": {"Authorization": "Bearer b-pack-invite-token-" + "z" * 24},
            "in a cookie": {"Cookie": f"authorization=Bearer {token}"},
            "in x-api-key": {"X-API-Key": token},
        }
        for method, path, kw in calls:
            for name, headers in wrong.items():
                r = await w.client.request(method, path, headers=headers, **kw)
                assert (r.status_code, r.content) == NOT_VALID, (method, path, name)
                assert r.headers.get("www-authenticate") == "Bearer"
        # the real one passes all three
        for method, path, kw in calls:
            r = await publish(w, token, method, path, **kw)
            assert r.status_code in (200, 201, 404), (method, path, r.status_code)
        assert await versions(w, pid) == [1]


async def test_a_revoked_or_an_expired_token_stops_at_once(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w)
        token, info = await make_token(w, pid)
        stale, stale_info = await make_token(w, pid, label="stale")
        assert (await publish(w, token)).status_code == 200 and (await publish(w, stale)).status_code == 200
        await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}/tokens/{info['id']}")
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID
        async with w.factory() as s:
            row = await s.get(KnowledgePackToken, stale_info["id"])
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()
        r = await publish(w, stale)
        assert (r.status_code, r.content) == NOT_VALID
        listed = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/tokens")).json()["tokens"]
        assert {t["label"]: t["revoked"] for t in listed} == {"ci": True, "stale": False}


async def test_a_token_of_a_deleted_pack_is_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        assert (await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}")).status_code == 204
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID
        async with w.factory() as s:
            assert await s.scalar(select(func.count()).select_from(KnowledgePackToken)
                                  .where(KnowledgePackToken.pack_id == pid)) == 0


async def _set_role(w, who, role):
    from src.db.models import WorkspaceMember

    async with w.factory() as s:
        row = await s.get(WorkspaceMember, (w.ws["ws-a"], w.uid(who)))
        if role is None:
            await s.delete(row)
        else:
            row.role = role
        await s.commit()


async def test_a_token_ends_with_the_makers_place_in_the_workspace(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="shared", audience="workspace")
        # admin_a maintains a workspace pack by being a workspace admin
        token, _ = await make_token(w, pid, who="admin_a")
        assert (await publish(w, token)).status_code == 200
        await _set_role(w, "admin_a", "member")                  # demoted: a reader only
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID
        await _set_role(w, "admin_a", "admin")                   # promoted back: the same token works again
        assert (await publish(w, token)).status_code == 200
        await _set_role(w, "admin_a", None)                      # removed from the workspace
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID


async def test_a_token_ends_when_the_maker_is_switched_off_or_deleted(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="shared", audience="workspace")
        token, _ = await make_token(w, pid, who="admin_a")
        from src.users.store import get_user_store

        store = get_user_store()
        user = store.get_by_id(w.uid("admin_a"))
        user.is_active = False
        store.update(user)
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID
        user.is_active = True
        store.update(user)
        assert (await publish(w, token)).status_code == 200
        store.delete(w.uid("admin_a"))
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID


async def test_a_token_ends_when_the_makers_right_to_maintain_goes_by_a_change_of_the_pack(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        base = "/api/codebase/packs"
        # 1. maintained by name: demoted to reader by another maintainer
        pid = await make_pack(w, slug="named", audience="restricted")
        add = await call(w, "editor_a", "POST", f"{base}/{pid}/members",
                         json={"email": w.users["member_a"].email, "permission": "maintain"})
        assert add.status_code == 200, add.text
        token, _ = await make_token(w, pid, who="member_a")
        assert (await publish(w, token)).status_code == 200
        await call(w, "editor_a", "POST", f"{base}/{pid}/members",
                   json={"email": w.users["member_a"].email, "permission": "read"})
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID
        await call(w, "editor_a", "POST", f"{base}/{pid}/members",
                   json={"email": w.users["member_a"].email, "permission": "maintain"})
        assert (await publish(w, token)).status_code == 200
        removed = await call(w, "editor_a", "DELETE", f"{base}/{pid}/members/{w.uid('member_a')}")
        assert removed.status_code == 204
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID

        # 2. maintained through a team: the grant is taken away, or lowered to read
        team = w.ids["team_a"]
        pid2 = await make_pack(w, slug="viateam", audience="restricted",
                               teams=[{"team_id": team, "permission": "maintain"}])
        token2, _ = await make_token(w, pid2, who="owner_a")      # in team-a, by the team's grant
        assert (await publish(w, token2)).status_code == 200
        await call(w, "editor_a", "PUT", f"{base}/{pid2}/teams",
                   json={"teams": [{"team_id": team, "permission": "read"}]})
        r = await publish(w, token2)
        assert (r.status_code, r.content) == NOT_VALID
        await call(w, "editor_a", "PUT", f"{base}/{pid2}/teams",
                   json={"teams": [{"team_id": team, "permission": "maintain"}]})
        assert (await publish(w, token2)).status_code == 200
        await call(w, "editor_a", "PUT", f"{base}/{pid2}/teams", json={"teams": []})
        r = await publish(w, token2)
        assert (r.status_code, r.content) == NOT_VALID

        # 3. an admin maintains a "teams" pack only because of the audience; made private, they lose it
        pid3 = await make_pack(w, slug="audience", audience="teams")
        token3, _ = await make_token(w, pid3, who="admin_a")
        assert (await publish(w, token3)).status_code == 200
        assert (await call(w, "editor_a", "PATCH", f"{base}/{pid3}", json={"audience": "restricted"})).status_code == 200
        r = await publish(w, token3)
        assert (r.status_code, r.content) == NOT_VALID
        # ...while the editor who made the pack is still its named maintainer, and a token of theirs still works
        token4, _ = await make_token(w, pid3, who="editor_a")
        assert (await publish(w, token4)).status_code == 200


async def test_a_team_that_is_deleted_takes_the_maintain_it_gave_with_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        team = w.ids["team_a"]
        pid = await make_pack(w, slug="viateam", audience="restricted",
                              teams=[{"team_id": team, "permission": "maintain"}])
        token, _ = await make_token(w, pid, who="owner_a")
        assert (await publish(w, token)).status_code == 200
        from src.db.models import TeamMember

        async with w.factory() as s:
            row = await s.get(TeamMember, (team, w.uid("owner_a")))
            await s.delete(row)                                   # leaves the team
            await s.commit()
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID


async def test_a_superadmins_token_and_a_global_admins_follow_the_same_rule(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="shared", audience="workspace")
        su_token, _ = await make_token(w, pid, who="su")
        ga_token, _ = await make_token(w, pid, who="gadmin")
        assert (await publish(w, su_token)).status_code == 200
        assert (await publish(w, ga_token)).status_code == 200
        # made private: the superadmin still sees everything, a global admin does not
        assert (await call(w, "editor_a", "PATCH", f"/api/codebase/packs/{pid}",
                           json={"audience": "restricted"})).status_code == 200
        assert (await publish(w, su_token)).status_code == 200
        r = await publish(w, ga_token)
        assert (r.status_code, r.content) == NOT_VALID


async def test_removing_a_pack_member_who_made_a_token_leaves_the_pack_but_not_the_token(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="shared", audience="workspace")
        token, _ = await make_token(w, pid, who="editor_a")
        # the creator's explicit membership is removed; as a workspace-pack's editor they are not an admin
        async with w.factory() as s:
            row = await s.get(KnowledgePackMember, (pid, w.uid("editor_a")))
            await s.delete(row)
            await s.commit()
        r = await publish(w, token)
        assert (r.status_code, r.content) == NOT_VALID


# ─── The token is accepted nowhere else ──────────────────────────────


async def test_the_session_dependency_refuses_a_pack_token(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from src.api.deps import get_current_user

    async with world(tmp_path, monkeypatch) as w:
        _, token = await ready(w)
        from src.users.store import get_user_store

        with pytest.raises(HTTPException) as caught:
            get_current_user(authorization=f"Bearer {token}", users=get_user_store())
        assert caught.value.status_code == 401
        assert token not in str(caught.value.detail)


def _api_routes():
    """Every route of every router module the API mounts: (route, gate names)."""
    import importlib
    import pkgutil

    from fastapi.routing import APIRoute

    import src.api.routers as pkg

    def names(dependant, into):
        for sub in dependant.dependencies:
            into.add(getattr(sub.call, "__name__", repr(sub.call)))
            names(sub, into)
        return into

    mods = [importlib.import_module(f"{pkg.__name__}.{m.name}") for m in pkgutil.iter_modules(pkg.__path__)]
    routers = [m.router for m in mods if hasattr(m, "router")]
    try:
        from src.ee.analytics import productivity_router
        from src.ee.analytics import router as analytics_router

        routers += [getattr(r, "router", r) for r in (analytics_router, productivity_router)]
    except ImportError:  # pragma: no cover — a trimmed build
        pass
    return [(route, names(route.dependant, set()))
            for router in routers for route in router.routes if isinstance(route, APIRoute)]


async def test_no_other_route_takes_a_pack_token_for_a_credential(tmp_path, monkeypatch):
    """Every route of the API, with the REAL login dependency (the harness's
    stand-in is taken off), answers a request carrying a live pack token exactly as
    it answers one carrying a made-up token of the same shape: it is no credential."""
    import re

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.deps import get_current_user

    async with world(tmp_path, monkeypatch) as w:
        pid, token = await ready(w)
        app = FastAPI()
        from tests.api.rbac_world import _routers

        for router in _routers():
            app.include_router(router)
        app.dependency_overrides.pop(get_current_user, None)
        seen = 0
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://t") as client:
            for route, _gates in _api_routes():
                if route.path.startswith("/api/codebase/publish"):
                    continue
                path = re.sub(r"\{[^}]+\}", pid, route.path)
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    kw = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
                    plain = await client.request(
                        method, path, headers={**bearer("cpk_" + "q" * 43), "X-Workspace": "ws-a"}, **kw)
                    with_token = await client.request(
                        method, path, headers={**bearer(token), "X-Workspace": "ws-a"}, **kw)
                    assert (with_token.status_code, with_token.content) == (
                        plain.status_code, plain.content), (method, route.path)
                    assert token not in with_token.text
                    seen += 1
        assert seen > 150, "the walk found too few routes to mean anything"
        assert await versions(w, pid) == []


def test_only_the_publish_routes_use_the_pack_token_gate_and_they_use_nothing_else():
    publish_routes = [(route, gates) for route, gates in _api_routes() if "require_pack_token" in gates]
    assert sorted((sorted(route.methods), route.path) for route, _ in publish_routes) == [
        (["GET"], "/api/codebase/publish"), (["GET"], "/api/codebase/publish/archive"),
        (["POST"], "/api/codebase/publish")]
    for route, gates in publish_routes:
        assert not gates & {"get_current_user", "require_codebase_reader", "require_pack_author",
                            "require_signed_in", "current_workspace_id"}, (route.path, gates)
    # and nothing else under that prefix escapes it
    assert not [route.path for route, gates in _api_routes()
                if route.path.startswith("/api/codebase/publish") and "require_pack_token" not in gates]


async def test_the_mcp_verifier_the_dev_endpoint_and_the_project_token_path_do_not_know_it(tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy import create_engine

    from src.access import resolver
    from src.mcp_server import project_tokens
    from src.mcp_server.auth import JwtConfig, JwtTokenVerifier

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    async with world(tmp_path, monkeypatch) as w:
        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        try:
            _, token = await ready(w)
            for accept_projects in (False, True):                # /mcp/dev and /mcp
                verifier = JwtTokenVerifier(JwtConfig.from_env(), accept_project_tokens=accept_projects)
                assert await verifier.verify_token(token) is None
                assert await verifier.verify_token(f"Bearer {token}") is None
            assert project_tokens.is_project_token(token) is False
            assert await asyncio.to_thread(project_tokens.lookup_hash, token) is None
        finally:
            engine.dispose()
