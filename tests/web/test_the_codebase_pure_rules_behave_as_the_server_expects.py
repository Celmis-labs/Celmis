"""The rules a Codebase page leans on, run as code and not read as a
description of code.

`web/lib/codebase.ts` imports nothing, so the web app's own tsc compiles it on
its own and node runs it — what is asserted is what the browser executes:

* a refused upload is read from the server's structured `detail` (code,
  message, hits as `path:line label`), and anything else — a network failure,
  a 403, a plain-string `detail` — is NOT mistaken for one;
* a folder pick leaves out hidden folders, `__MACOSX` and `node_modules`
  before they use the 5 MB the whole upload has, and nothing else;
* a pack's files are grouped the way a person looks for them;
* an invite link's state is revoked, expired, used up or active, in that order
  of precedence;
* the limits and vocabularies the page states are the server's: copied
  constants drift, and a page that says "400 files" over a server that says
  300 turns a clear refusal into a surprise.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _strip_comments,
)
from tests.web.test_the_seams_between_page_and_server_hold import _compile_and_run

LIB = WEB / "lib" / "codebase.ts"
HINTS = WEB / "components" / "codebase" / "ingest-error.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages" / "en.json"

pytestmark = pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")


def _run(tmp_path, body: str):
    return _compile_and_run(
        tmp_path, {"codebase.ts": LIB.read_text(encoding="utf-8")},
        'import * as cb from "./codebase";\n' + body)


# ─── a refused upload ────────────────────────────────────────────────

REFUSAL = {
    "body": {"detail": {"code": "secrets_found", "message": "Secrets were found.",
                        "hits": [{"path": "api/notes.md", "line": 14, "label": "API key"},
                                 {"path": ".env", "line": 0, "label": "secret file"}]}},
}


def test_a_structured_refusal_is_read_with_its_hits(tmp_path):
    got = _run(tmp_path, f"""
const r = cb.refusalOf({json.dumps(REFUSAL)});
console.log(JSON.stringify({{ r, labels: r ? r.hits.map(cb.hitLabel) : null }}));
""")
    assert got["r"]["code"] == "secrets_found"
    assert got["r"]["message"] == "Secrets were found."
    assert got["labels"] == ["api/notes.md:14 API key", ".env secret file"]


@pytest.mark.parametrize("error", [
    None,
    "boom",
    {},
    {"body": None},
    {"body": "<html>502</html>"},
    {"body": {"detail": "Requires a pack maintainer"}},
    {"body": {"detail": [{"loc": ["body", "note"], "msg": "too long"}]}},
    {"body": {"detail": {"message": "no code"}}},
    {"body": {"detail": {"code": "x"}}},
    {"body": {"detail": {"code": 3, "message": "m"}}},
], ids=lambda e: json.dumps(e)[:40])
def test_anything_that_is_not_a_structured_refusal_is_not_read_as_one(tmp_path, error):
    got = _run(tmp_path, f"console.log(JSON.stringify(cb.refusalOf({json.dumps(error)})));")
    assert got is None


def test_a_refusal_with_odd_hits_keeps_the_ones_it_can_and_invents_nothing(tmp_path):
    got = _run(tmp_path, """
const r = cb.refusalOf({ body: { detail: { code: "c", message: "m", hits: [
  null, 4, { line: 3 }, { path: "a.md" }, { path: "b.md", line: "7", label: 9 },
  { path: "c.md", line: 2, label: "token" } ] } } });
console.log(JSON.stringify(r));
""")
    assert got["hits"] == [
        {"path": "a.md", "line": 0, "label": ""},
        {"path": "b.md", "line": 0, "label": ""},
        {"path": "c.md", "line": 2, "label": "token"},
    ]


def test_a_hit_never_carries_more_than_where_and_what_kind(tmp_path):
    """The server returns `path`, `line`, `label` and never the matched text; the
    reader must not pass along a fourth field if one ever appears."""
    got = _run(tmp_path, """
const r = cb.refusalOf({ body: { detail: { code: "c", message: "m", hits: [
  { path: "a.md", line: 1, label: "key", match: "AKIA-not-for-display" } ] } } });
console.log(JSON.stringify(r));
""")
    assert set(got["hits"][0]) == {"path", "line", "label"}
    assert "AKIA" not in json.dumps(got)


# ─── a folder pick ───────────────────────────────────────────────────

SKIPPED = {
    "skills/a/SKILL.md": False,
    "SKILL.md": False,
    "notes/readme.md": False,
    "api/v1.json": False,
    "my.folder/a.md": False,
    ".git/config": True,
    "repo/.git/HEAD": True,
    ".env": True,
    "skills/.hidden/x.md": True,
    "__MACOSX/._a": True,
    "pkg/__MACOSX/a": True,
    "node_modules/x/index.js": True,
    "web/node_modules/x/a.md": True,
    ".DS_Store": True,
    "node_modules_old/a.md": False,
    "a/__MACOSX_notes/b.md": False,
}


def test_a_folder_pick_leaves_out_exactly_the_junk(tmp_path):
    got = _run(tmp_path, f"""
const paths: string[] = {json.dumps(list(SKIPPED))};
console.log(JSON.stringify(paths.map((p) => cb.skippedLocally(p))));
""")
    assert dict(zip(SKIPPED, got, strict=True)) == SKIPPED


# ─── sizes and names ─────────────────────────────────────────────────

SIZES = {0: "0 B", 812: "812 B", 1023: "1023 B", 1024: "1.0 KB", 3481: "3.4 KB",
         10240: "10 KB", 524288: "512 KB", 1048576: "1.0 MB", 5 * 1024 * 1024: "5.0 MB",
         -1: "", float("nan"): ""}


def test_sizes_read_the_way_people_say_them(tmp_path):
    inputs = [("NaN" if k != k else str(k)) for k in SIZES]
    got = _run(tmp_path, f"""
console.log(JSON.stringify([{", ".join(inputs)}].map((n) => cb.formatBytes(n))));
""")
    assert got == list(SIZES.values())


SLUGS = {
    "Payments service guides": "payments-service-guides",
    "  Spaces   and --- dashes!  ": "spaces-and-dashes",
    "Café Ünïcode": "cafe-unicode",
    "Привіт світ": "",
    "": "",
    "---": "",
    "a" * 80: "a" * 64,
    "x" * 63 + " y": "x" * 63,
}


def test_a_slug_is_suggested_from_the_title_and_never_ends_in_a_dash(tmp_path):
    got = _run(tmp_path, f"""
const titles: string[] = {json.dumps(list(SLUGS))};
console.log(JSON.stringify(titles.map((t) => cb.slugify(t))));
""")
    assert dict(zip(SLUGS, got, strict=True)) == SLUGS
    from src.knowledge.store import MAX_SLUG, SLUG_RE

    for slug in got:
        assert slug == "" or SLUG_RE.match(slug), slug
        assert len(slug) <= MAX_SLUG


# ─── markdown ────────────────────────────────────────────────────────


def test_front_matter_is_split_from_the_text_under_it(tmp_path):
    got = _run(tmp_path, r"""
console.log(JSON.stringify([
  cb.splitFrontMatter("---\nname: a\ndescription: b\n---\n# Title\nbody"),
  cb.splitFrontMatter("---\r\nname: a\r\n---\r\ntext"),
  cb.splitFrontMatter("# No front matter\n---\nnot a block\n---\n"),
  cb.splitFrontMatter("---\nnever closed\ntext"),
  cb.splitFrontMatter(""),
  [cb.isMarkdownPath("a/SKILL.md"), cb.isMarkdownPath("x.MARKDOWN"), cb.isMarkdownPath("x.mdx"), cb.isMarkdownPath("md")],
]));
""")
    assert got[0] == {"front": "name: a\ndescription: b", "body": "# Title\nbody"}
    assert got[1] == {"front": "name: a", "body": "text"}
    assert got[2]["front"] is None and got[2]["body"].startswith("# No front matter")
    assert got[3]["front"] is None
    assert got[4] == {"front": None, "body": ""}
    assert got[5] == [True, True, False, False]


# ─── the file list ───────────────────────────────────────────────────

ITEMS = [
    {"path": "skills/zeta/SKILL.md", "kind": "skill", "skill": "zeta", "name": "zeta", "description": "last skill"},
    {"path": "skills/alpha/ref/b.md", "kind": "file", "skill": "alpha", "name": "b", "description": ""},
    {"path": "skills/alpha/SKILL.md", "kind": "skill", "skill": "alpha", "name": "alpha", "description": "first skill"},
    {"path": "skills/alpha/ref/a.md", "kind": "file", "skill": "alpha", "name": "a", "description": ""},
    {"path": "notes/z.md", "kind": "note", "skill": None, "name": "z", "description": ""},
    {"path": "api/pay.md", "kind": "api", "skill": None, "name": "pay", "description": ""},
    {"path": "README.md", "kind": "doc", "skill": None, "name": "readme", "description": ""},
    {"path": "stray.txt", "kind": "file", "skill": None, "name": "stray", "description": ""},
    {"path": "odd/x.md", "kind": "weird", "skill": None, "name": "x", "description": ""},
    {"path": "checklists/sec.md", "kind": "checklist", "skill": None, "name": "sec", "description": ""},
]


def test_files_are_grouped_by_skill_then_by_kind_in_a_fixed_order(tmp_path):
    got = _run(tmp_path, f"""
const g = cb.groupItems({json.dumps(ITEMS)});
console.log(JSON.stringify({{
  skills: g.skills.map((s) => [s.skill, s.description, s.items.map((i) => i.path)]),
  others: g.others.map((o) => [o.kind, o.items.map((i) => i.path)]),
}}));
""")
    assert got["skills"] == [
        ["alpha", "first skill",
         ["skills/alpha/SKILL.md", "skills/alpha/ref/a.md", "skills/alpha/ref/b.md"]],
        ["zeta", "last skill", ["skills/zeta/SKILL.md"]],
    ]
    # api, checklist, note, doc in that order; a stray "file" reads as a
    # document; a kind this page has not heard of comes last, not lost.
    assert got["others"] == [
        ["api", ["api/pay.md"]],
        ["checklist", ["checklists/sec.md"]],
        ["note", ["notes/z.md"]],
        ["doc", ["README.md", "stray.txt"]],
        ["weird", ["odd/x.md"]],
    ]


def test_no_file_is_lost_in_grouping(tmp_path):
    got = _run(tmp_path, f"""
const g = cb.groupItems({json.dumps(ITEMS)});
const all = [...g.skills.flatMap((s) => s.items), ...g.others.flatMap((o) => o.items)];
console.log(JSON.stringify(all.map((i) => i.path).sort()));
""")
    assert got == sorted(i["path"] for i in ITEMS)


# ─── diffs and invites ───────────────────────────────────────────────


def test_a_diff_entry_shows_its_path_whatever_shape_it_has(tmp_path):
    got = _run(tmp_path, """
console.log(JSON.stringify([
  cb.diffPath("a/b.md"), cb.diffPath({ path: "c.md", sha: "x" }), cb.diffPath(null),
  cb.diffPath(7), cb.diffPath({ path: 3 }),
]));
""")
    assert got == ["a/b.md", "c.md", "", "", ""]


INVITES = {
    "active": ({"revoked": False, "expires_at": "2026-12-01T00:00:00Z", "max_uses": 5, "used_count": 1}, "active"),
    "revoked beats everything": ({"revoked": True, "expires_at": "2020-01-01T00:00:00Z", "max_uses": 1, "used_count": 1}, "revoked"),
    "expired beats used up": ({"revoked": False, "expires_at": "2026-01-01T00:00:00Z", "max_uses": 1, "used_count": 1}, "expired"),
    "used up": ({"revoked": False, "expires_at": "2026-12-01T00:00:00Z", "max_uses": 3, "used_count": 3}, "used"),
    "expires this very moment": ({"revoked": False, "expires_at": "2026-10-01T00:00:00Z", "max_uses": 3, "used_count": 0}, "expired"),
}


def test_an_invite_is_shown_in_the_state_the_server_would_treat_it_in(tmp_path):
    now = "Date.parse('2026-10-01T00:00:00Z')"
    got = _run(tmp_path, f"""
const cases: any[] = {json.dumps([v[0] for v in INVITES.values()])};
console.log(JSON.stringify(cases.map((c) => cb.inviteState(c, {now}))));
""")
    assert dict(zip(INVITES, got, strict=True)) == {k: v[1] for k, v in INVITES.items()}


# ─── the server's own numbers ────────────────────────────────────────


def test_the_limits_the_page_states_are_the_servers(tmp_path):
    from src.knowledge import ingest

    got = _run(tmp_path, "console.log(JSON.stringify(cb.UPLOAD_LIMITS));")
    assert got == {
        "archiveBytes": ingest.MAX_ARCHIVE_BYTES,
        "files": ingest.MAX_FILES,
        "fileBytes": ingest.MAX_FILE_BYTES,
        "totalBytes": ingest.MAX_TOTAL_BYTES,
    }


def test_the_audiences_and_permissions_are_the_servers_in_the_servers_order(tmp_path):
    from src.users.roles import PACK_AUDIENCES, PACK_PERMISSIONS

    got = _run(tmp_path, "console.log(JSON.stringify([cb.PACK_AUDIENCES, cb.PACK_PERMISSIONS]));")
    assert got == [list(PACK_AUDIENCES), list(PACK_PERMISSIONS)]


def test_the_hit_list_shows_as_many_as_the_server_reports():
    """The server caps its report (`MAX_HITS`); showing fewer would hide a hit
    the person was told about, showing "more" would promise ones that never
    come."""
    from src.knowledge import ingest

    shown = re.search(r"const SHOWN_HITS = (\d+)", HINTS.read_text(encoding="utf-8"))
    assert shown and int(shown.group(1)) == ingest.MAX_HITS


def test_every_refusal_the_server_can_make_has_advice():
    import src.knowledge.ingest as ingest

    source = (WEB.parent / "src" / "knowledge" / "ingest.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'IngestError\(\s*"([a-z_]+)"', source))
    assert codes, "the server's codes were not found"
    assert ingest.IngestError  # the class the codes belong to
    hint_map = dict(re.findall(r'^\s+(\w+): "(codebase\.ingest\.hint\.\w+)",$',
                               _strip_comments(HINTS.read_text(encoding="utf-8")), re.M))
    assert set(hint_map) == codes, (
        f"no advice for {sorted(codes - set(hint_map))}; advice for a code the "
        f"server never sends: {sorted(set(hint_map) - codes)}")
    en = json.loads(MESSAGES.read_text(encoding="utf-8"))
    for code, key in hint_map.items():
        assert key == f"codebase.ingest.hint.{code}"
        assert key in en, key
