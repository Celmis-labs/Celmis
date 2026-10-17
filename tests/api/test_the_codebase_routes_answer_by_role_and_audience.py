"""Who may see and change a pack, asked of the real routes for every role.

The contract (docs/CODEBASE.md, "Access"; docs/ROLES_AND_ACCESS.md):

  * a role gate comes first and answers identically for packs that exist and
    packs that do not; then the pack is loaded through its audience and grants;
    only then does the operation ask for a maintainer;
  * `workspace` packs are every member's; `teams` packs belong to owners,
    admins and the granted teams and named people; `restricted` packs to the
    granted teams and named people alone — an admin is not named by being an
    admin, a global admin sees no restricted pack, only the superadmin does;
  * a pack that is hidden, missing, another workspace's or whose id is not an
    id gets ONE answer, byte for byte, on every route, and nothing in the
    answer repeats a name the caller did not type;
  * someone who can see but not maintain gets `403 Requires a pack maintainer`
    on every change, and nothing is changed.
"""

from __future__ import annotations

import pytest

from tests.api.codebase_support import call, note_zip, seeded
from tests.api.rbac_world import B_PACK_ID, B_SECRET, world

NOT_FOUND = (404, b'{"detail":"pack not found or not accessible"}')
MAINTAINER = (403, b'{"detail":"Requires a pack maintainer"}')

#: actor -> (header workspace, slugs listed, slugs they may maintain)
EXPECTED = {
    "su": ("ws-a", ["pr", "prg", "pt", "ptg", "pw"], ["pr", "prg", "pt", "ptg", "pw"]),
    "gadmin": ("ws-a", ["pt", "ptg", "pw"], ["pt", "ptg", "pw"]),
    "owner_a": ("ws-a", ["prg", "pt", "ptg", "pw"], ["pt", "ptg", "pw"]),
    "admin_a": ("ws-a", ["prg", "pt", "ptg", "pw"], ["pt", "ptg", "pw"]),
    "admin2_a": ("ws-a", ["pt", "ptg", "pw"], ["pt", "ptg", "pw"]),
    "editor_a": ("ws-a", ["pr", "prg", "pt", "ptg", "pw"], ["pr", "prg", "pt", "ptg", "pw"]),
    "member_a": ("ws-a", ["pw"], []),
    "viewer_a": ("ws-a", ["pw"], []),
    "both": ("ws-a", ["pw"], []),
}


def _slugs(r) -> list[str]:
    return sorted(p["slug"] for p in r.json()["packs"])


@pytest.mark.parametrize("who", sorted(EXPECTED))
async def test_each_role_lists_exactly_the_packs_its_audience_allows(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        r = await call(w, who, "GET", "/api/codebase/packs", EXPECTED[who][0])
        assert r.status_code == 200, r.text
        assert _slugs(r) == EXPECTED[who][1]
        assert sorted(p["slug"] for p in r.json()["packs"] if p["can_maintain"]) == EXPECTED[who][2]
        assert r.json()["can_create"] is (who in ("su", "gadmin", "owner_a", "admin_a",
                                                  "admin2_a", "editor_a"))


@pytest.mark.parametrize("who", sorted(EXPECTED))
async def test_a_pack_can_be_read_exactly_when_it_is_listed(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        for slug, pid in ids.items():
            r = await call(w, who, "GET", f"/api/codebase/packs/{pid}")
            if slug in EXPECTED[who][1]:
                assert r.status_code == 200, (slug, r.text)
                body = r.json()
                assert body["slug"] == slug and body["can_maintain"] == (slug in EXPECTED[who][2])
                # maintainers see who is named, the usage and the teams; readers do not
                assert (body["usage"] is not None) == body["can_maintain"]
                assert "usage_30d" not in body
                assert (body["members"] != []) == body["can_maintain"]
                assert (body["teams"] != []) == (body["can_maintain"] and slug in ("ptg", "prg"))
            else:
                assert (r.status_code, r.content) == NOT_FOUND, (slug, r.status_code)


def _requests(pid: str, item_id: str, invite_id: str) -> list[tuple[str, str, dict]]:
    """Every route that names a pack, with a body where one is needed."""
    base = f"/api/codebase/packs/{pid}"
    return [
        ("GET", base, {}),
        ("PATCH", base, {"json": {"title": "x"}}),
        ("DELETE", base, {}),
        ("PUT", f"{base}/teams", {"json": {"teams": []}}),
        ("POST", f"{base}/members", {"json": {"email": "member-a@acme-corp.io",
                                              "permission": "read"}}),
        ("DELETE", f"{base}/members/u-member_a", {}),
        ("GET", f"{base}/versions", {}),
        ("POST", f"{base}/versions", {"files": {"archive": ("p.zip", note_zip("zz"), "application/zip")}}),
        ("POST", f"{base}/versions/1/activate", {}),
        ("GET", f"{base}/versions/1/diff", {}),
        ("GET", f"{base}/versions/1/diff?against=1", {}),
        ("GET", f"{base}/versions/1/archive", {}),
        ("GET", f"{base}/items/{item_id}", {}),
        ("GET", f"{base}/invites", {}),
        ("POST", f"{base}/invites", {"json": {}}),
        ("DELETE", f"{base}/invites/{invite_id}", {}),
    ]


MISSING = "99999999-9999-4999-8999-999999999999"
# An id with an encoded slash never reaches the router (the framework answers its own
# generic 404 for every unmatched path), so it cannot tell a hidden pack from a missing one.
MALFORMED = ["not-a-uuid", "1" * 40, "%00", "null", "0"]


@pytest.mark.parametrize("who", ["su", "gadmin", "admin_a", "member_a", "viewer_a", "both"])
async def test_every_route_answers_a_hidden_pack_like_a_missing_one(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        hidden = [pid for slug, pid in ids.items() if slug not in EXPECTED[who][1]]
        missing_ids = [MISSING, *MALFORMED]
        if not hidden:
            hidden = [MISSING]                      # the superadmin hides nothing in A
        checked = 0
        for method, path, kw in _requests("{id}", MISSING, MISSING):
            answers = set()
            for pid in [*hidden, *missing_ids]:
                p = path.replace("{id}", pid)
                r = await call(w, who, method, p, **kw)
                answers.add((r.status_code, r.content))
                checked += 1
            assert len(answers) == 1, (method, path, answers)
            [(status, content)] = answers
            assert status == 404 and content == NOT_FOUND[1], (method, path, status, content)
        assert checked >= 16 * 2


async def test_the_other_workspaces_pack_is_missing_for_everyone_in_this_one(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        for who in ("su", "gadmin", "owner_a", "admin_a", "editor_a", "member_a", "both"):
            for method, path, kw in _requests(B_PACK_ID, MISSING, MISSING):
                r = await call(w, who, method, path, **kw)
                assert (r.status_code, r.content) == NOT_FOUND, (who, method, path)
                assert B_SECRET.encode() not in r.content


@pytest.mark.parametrize("who", ["owner_a", "admin_a", "admin2_a", "member_a", "viewer_a", "both"])
async def test_someone_who_can_see_but_not_maintain_changes_nothing(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        for slug in EXPECTED[who][1]:
            if slug in EXPECTED[who][2]:
                continue
            pid = ids[slug]
            before = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).json()
            item = before["items"][0]["id"]
            for method, path, kw in _requests(pid, item, MISSING):
                if method == "GET" and "invites" not in path:
                    continue                                   # reading is allowed
                r = await call(w, who, method, path, **kw)
                assert (r.status_code, r.content) == MAINTAINER, (who, slug, method, path)
            after = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).json()
            assert after == before, (who, slug)


@pytest.mark.parametrize("who", ["member_a", "viewer_a", "both"])
async def test_a_reader_cannot_create_a_pack(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, who, "POST", "/api/codebase/packs",
                       json={"slug": "mine", "title": "Mine"})
        assert r.status_code == 403
        assert r.json() == {"detail": "Requires the editor role or higher to create packs"}
        assert (await call(w, who, "GET", "/api/codebase/packs")).json()["packs"] == []


@pytest.mark.parametrize("who", ["owner_a", "admin_a", "editor_a", "gadmin", "su"])
async def test_authors_create_a_pack_and_maintain_it(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, who, "POST", "/api/codebase/packs",
                       json={"slug": "mine", "title": "Mine", "audience": "teams"})
        assert r.status_code == 201, r.text
        assert r.json()["can_maintain"] is True and r.json()["current_version"] is None
        got = await call(w, who, "GET", f"/api/codebase/packs/{r.json()['id']}")
        assert got.status_code == 200 and got.json()["can_maintain"] is True


async def test_the_creator_keeps_a_restricted_pack_and_an_admin_does_not_see_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                       json={"slug": "mine", "title": "Mine", "audience": "restricted"})
        pid = r.json()["id"]
        assert (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")).status_code == 200
        for who in ("owner_a", "admin_a", "admin2_a", "gadmin", "member_a"):
            got = await call(w, who, "GET", f"/api/codebase/packs/{pid}")
            assert (got.status_code, got.content) == NOT_FOUND, who
        assert (await call(w, "su", "GET", f"/api/codebase/packs/{pid}")).status_code == 200


async def test_a_name_clash_is_the_same_409_for_a_pack_the_caller_cannot_see(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        answers = set()
        for slug in ("pr", "pw"):                              # editor2 would not see `pr`
            r = await call(w, "admin2_a", "POST", "/api/codebase/packs",
                           json={"slug": slug, "title": "T"})
            answers.add((r.status_code, r.content))
        assert answers == {(409, b'{"detail":"slug is not available"}')}


async def test_a_pack_of_b_is_visible_to_b_and_to_nobody_else(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        seen = await call(w, "member_b", "GET", f"/api/codebase/packs/{B_PACK_ID}", "ws-b")
        assert seen.status_code == 200 and seen.json()["slug"] == "b-secret-pack"
        listed = await call(w, "member_b", "GET", "/api/codebase/packs", "ws-b")
        assert [p["slug"] for p in listed.json()["packs"]] == ["b-secret-pack"]
        # a member of both, asking about A: B's pack is missing; asking about B: theirs
        assert (await call(w, "both", "GET", f"/api/codebase/packs/{B_PACK_ID}", "ws-a")
                ).status_code == 404
        assert (await call(w, "both", "GET", f"/api/codebase/packs/{B_PACK_ID}", "ws-b")
                ).status_code == 200


async def test_nothing_in_a_hidden_answer_repeats_a_pack_or_team_name(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        for who in ("member_a", "viewer_a"):
            for slug in ("pt", "ptg", "pr", "prg"):
                for method, path, kw in _requests(ids[slug], MISSING, MISSING):
                    r = await call(w, who, method, path, **kw)
                    text = r.text.lower()
                    for word in ("pack pt", "pack pr", "about", "alpha-team", "team-a", "billing"):
                        assert word not in text, (who, slug, method, path, word)
