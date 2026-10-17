"""Helpers for the Codebase REST tests — not a test module.

`seeded(w)` creates, through the real routes and as ``editor_a``, one pack of
each shape in workspace A:

    pw   audience workspace                      every member
    pt   audience teams, no grant                owners/admins + editor_a (the creator)
    ptg  audience teams, granted to team-a       + team-a's members
    pr   audience restricted, no grant           editor_a (the creator) only
    prg  audience restricted, granted to team-a  + team-a's members (owner_a, admin_a)

each with one uploaded version holding a note that carries the pack's own marker
word, so a search for the marker finds exactly that pack.
"""

from __future__ import annotations

import io
import zipfile

SHAPES = {
    "pw": ("workspace", False),
    "pt": ("teams", False),
    "ptg": ("teams", True),
    "pr": ("restricted", False),
    "prg": ("restricted", True),
}

def marker(slug: str) -> str:
    """The word that appears only in a pack's own note. It starts with the slug so
    that no two markers share a search stem (a word's first five letters)."""
    return f"{slug}quokka"


def zip_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in files.items():
            zf.writestr(path, text)
    return buf.getvalue()


def note_zip(slug: str) -> bytes:
    return zip_of({"README.md": f"# {slug}\n", "notes/n.md": f"# Note\n\nThe {marker(slug)} recipe.\n"})


async def call(w, who: str, method: str, path: str, ws: str | None = "ws-a", **kw):
    return await w.client.request(method, path, headers=w.h(who, ws), **kw)


async def upload_zip(w, who: str, pack_id: str, data: bytes, note: str = "", ws: str = "ws-a"):
    return await call(w, who, "POST", f"/api/codebase/packs/{pack_id}/versions", ws,
                      files={"archive": ("pack.zip", data, "application/zip")}, data={"note": note})


async def seeded(w) -> dict[str, str]:
    ids: dict[str, str] = {}
    for slug, (audience, granted) in SHAPES.items():
        body = {"slug": slug, "title": f"Pack {slug}", "description": f"About {slug}",
                "audience": audience,
                "teams": [{"team_id": w.ids["team_a"], "permission": "read"}] if granted else []}
        r = await call(w, "editor_a", "POST", "/api/codebase/packs", json=body)
        assert r.status_code == 201, r.text
        ids[slug] = r.json()["id"]
        r = await upload_zip(w, "editor_a", ids[slug], note_zip(slug))
        assert r.status_code == 201, r.text
    return ids
