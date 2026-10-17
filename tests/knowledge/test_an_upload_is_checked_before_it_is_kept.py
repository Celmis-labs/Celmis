"""What a pack upload may contain, and every way it is refused.

The contract (docs/CODEBASE.md, "Uploading"): a zip or a list of files becomes
validated items or the WHOLE upload is refused with a stable code. Nothing is
written to disk; bytes are counted as they are actually decompressed; a secret
is reported as `path:line label` and its value is never repeated; paths that
could escape a folder, links, encrypted entries, binaries, private keys and
files over the limits are all refused; harmless debris (`__MACOSX`, `.DS_Store`,
`.git`, images) is ignored and listed.
"""

from __future__ import annotations

import io
import secrets
import stat
import zipfile

import pytest

from src.knowledge import ingest
from src.knowledge.ingest import IngestError, ingest_directory, ingest_files, ingest_zip

SKILL = "---\nname: {name}\ndescription: {desc}\n---\n\n# Title\n\nBody about {name}.\n"


def skill(name="billing-client", desc="How to call the billing client safely."):
    return SKILL.format(name=name, desc=desc)


def make_zip(files: dict[str, bytes | str], *, symlinks=(), flag_bits=None,
             compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        for name, target in symlinks:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    data = buf.getvalue()
    if flag_bits:
        data = _set_encrypted(data)
    return data


def _set_encrypted(data: bytes) -> bytes:
    """Mark every entry encrypted (general-purpose flag bit 0), the way a
    password-protected archive does."""
    out = bytearray(data)
    for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = out.find(sig)
        while at != -1:
            out[at + offset] |= 0x01
            at = out.find(sig, at + 4)
    return bytes(out)


ROOT = ("README.md", b"# pack\n")      # a file at the root keeps one folder from being stripped


def refused(data: bytes | None = None, *, files=None) -> IngestError:
    with pytest.raises(IngestError) as caught:
        ingest_zip(data) if files is None else ingest_files(files)
    return caught.value


# ─── What is kept ────────────────────────────────────────────────────


def test_a_skill_and_its_files_become_items_of_that_skill():
    result = ingest_zip(make_zip({
        "billing-client/SKILL.md": skill(),
        "billing-client/reference/retries.md": "# Retries\n\nBack off twice.\n",
        "billing-client/scripts/call.py": "print('hi')\n",
    }))
    by_path = {f.path: f for f in result.files}
    assert set(by_path) == {"SKILL.md", "reference/retries.md", "scripts/call.py"}
    assert by_path["SKILL.md"].kind == "skill" and by_path["SKILL.md"].skill == "billing-client"
    assert by_path["reference/retries.md"].kind == "file"
    assert by_path["reference/retries.md"].skill == "billing-client"
    assert result.skills == ["billing-client"]


def test_the_kind_of_a_plain_file_follows_its_folder():
    result = ingest_files([
        ("api/invoices.http", b"GET /v1/invoices\n"),
        ("apis/orders.md", b"# Orders\n"),
        ("checklists/release.md", b"- [ ] tag\n"),
        ("notes/how-we-retry.md", b"# Retry\n"),
        ("context/shop.md", b"# Shop\n"),
        ("misc/other.md", b"# Other\n"),
        ("README.md", b"# Pack\n"),
    ])
    kinds = {f.path: f.kind for f in result.files}
    assert kinds == {
        "api/invoices.http": "api", "apis/orders.md": "api",
        "checklists/release.md": "checklist", "notes/how-we-retry.md": "note",
        "context/shop.md": "note", "misc/other.md": "doc", "README.md": "doc",
    }


def test_front_matter_kind_wins_over_the_folder():
    result = ingest_files([("misc/where.md", b"---\nkind: checklist\n---\n# Where\n")])
    assert result.files[0].kind == "checklist"


def test_an_unknown_front_matter_kind_is_refused():
    err = refused(files=[ROOT, ("misc/where.md", b"---\nkind: skill\n---\n# Where\n")])
    assert err.code == "bad_front_matter" and "misc/where.md" in err.message


def test_front_matter_gives_name_description_repos_and_tags():
    text = ("---\nname: Retry notes\ndescription: Why we retry twice\n"
            "repos: [shop-api, billing:src/client.py:42]\ntags:\n  - retries\n  - http\n---\n"
            "# Heading\n\nBody.\n")
    [f] = ingest_files([("notes/retry.md", text.encode())]).files
    assert (f.name, f.description) == ("Retry notes", "Why we retry twice")
    assert f.repos == ["shop-api", "billing:src/client.py:42"] and f.tags == ["retries", "http"]


def test_without_front_matter_the_description_is_the_first_paragraph_line():
    [f] = ingest_files([("notes/a.md", b"# Title\n\n- Retries twice, then gives up.\n\nMore.\n")]).files
    assert f.name == "a" and f.description == "Retries twice, then gives up."


def test_a_leading_rule_line_in_a_note_is_not_front_matter():
    text = b"---\nJust a thought: it ends here\n---\n\nBody text.\n"
    [f] = ingest_files([("notes/a.md", text)]).files
    assert f.content.startswith("---") and f.description


def test_every_file_carries_its_hash_size_and_most_frequent_terms():
    [f] = ingest_files([("notes/a.md", b"alpha alpha beta\n")]).files
    assert len(f.sha) == 64 and f.size_bytes == len(b"alpha alpha beta\n")
    assert f.terms["alpha"] == 2 and f.terms["beta"] == 1


def test_a_version_hash_changes_with_any_file():
    a = ingest_files([("a.md", b"one\n"), ("b.md", b"two\n")]).files
    b = ingest_files([("a.md", b"one\n"), ("b.md", b"twoo\n")]).files
    assert ingest.version_sha(a) == ingest.version_sha(list(reversed(a)))
    assert ingest.version_sha(a) != ingest.version_sha(b)


def test_one_shared_top_folder_is_stripped():
    result = ingest_files([("pack/a.md", b"# A\n"), ("pack/notes/b.md", b"# B\n")])
    assert [f.path for f in result.files] == ["a.md", "notes/b.md"]
    assert {f.path: f.kind for f in result.files}["notes/b.md"] == "note"


def test_two_top_folders_are_kept_as_they_are():
    result = ingest_files([("one/a.md", b"# A\n"), ("two/b.md", b"# B\n")])
    assert [f.path for f in result.files] == ["one/a.md", "two/b.md"]


def test_a_skill_at_the_root_is_not_stripped_and_nothing_is_lost():
    result = ingest_files([("SKILL.md", skill().encode()), ("docs/x.md", b"# X\n")])
    assert {f.path for f in result.files} == {"SKILL.md", "docs/x.md"}


def test_a_skill_without_a_name_takes_the_folder_it_was_zipped_in():
    text = "---\ndescription: Does a thing.\n---\n# T\n"
    [f] = ingest_zip(make_zip({"My Skill/SKILL.md": text})).files
    assert f.skill == "my-skill"


def test_a_skill_in_a_subfolder_without_a_name_takes_that_folder():
    text = "---\ndescription: Does a thing.\n---\n# T\n"
    result = ingest_files([("skills/Deploy Helper/SKILL.md", text.encode()),
                           ("skills/other/SKILL.md", skill("other").encode())])
    assert result.skills == ["deploy-helper", "other"]


def test_debris_and_non_text_files_are_ignored_and_listed():
    result = ingest_zip(make_zip({
        "pack/a.md": "# A\n", "__MACOSX/pack/._a.md": b"\x00\x05", "pack/.DS_Store": b"\x00",
        "pack/.git/config": "[core]\n", "pack/.hidden/x.md": "# no\n",
        "pack/logo.png": b"\x89PNG\r\n", "pack/bin/tool": b"\x7fELF", "pack/data.zip": b"PK",
    }))
    assert [f.path for f in result.files] == ["a.md"]
    assert set(result.ignored) == {
        "__MACOSX/pack/._a.md", "pack/.DS_Store", "pack/.git/config", "pack/.hidden/x.md",
        "pack/logo.png", "pack/bin/tool", "pack/data.zip"}
    assert result.ignored_total == 7


def test_a_folder_on_disk_is_read_like_a_zip(tmp_path):
    (tmp_path / "skills" / "billing").mkdir(parents=True)
    (tmp_path / "skills" / "billing" / "SKILL.md").write_text(skill("billing"))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "n.md").write_text("# N\n")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    result = ingest_directory(tmp_path)
    assert {f.path for f in result.files} == {"skills/billing/SKILL.md", "notes/n.md"}
    assert ".git" in result.ignored and "pic.png" in result.ignored


def test_a_folder_with_a_symlink_is_refused(tmp_path):
    (tmp_path / "a.md").write_text("# A\n")
    (tmp_path / "link.md").symlink_to(tmp_path / "a.md")
    assert refused_dir(tmp_path).code == "symlink"


def refused_dir(path) -> IngestError:
    with pytest.raises(IngestError) as caught:
        ingest_directory(path)
    return caught.value


def test_a_folder_that_is_not_a_folder_is_refused(tmp_path):
    assert refused_dir(tmp_path / "missing").code == "bad_archive"


# ─── Paths ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "../evil.md", "a/../../evil.md", "/etc/passwd.md", "C:/windows/x.md", "a\\b.md",
    "a//b.md", "a/./b.md", "a/b\x00.md", "a/b\x07.md",
])
def test_a_path_that_could_leave_the_folder_refuses_the_whole_upload(name):
    err = refused(files=[("ok.md", b"# ok\n"), (name, b"# x\n")])
    assert err.code == "unsafe_path"


def test_zip_slip_in_a_real_archive_is_refused():
    err = refused(make_zip({"ok.md": "# ok\n", "../../evil.md": "# x\n"}))
    assert err.code == "unsafe_path"


def test_a_path_in_an_ignored_folder_is_still_checked():
    assert refused(make_zip({".git/../../evil.md": "x"})).code == "unsafe_path"


def test_a_path_over_300_characters_is_refused():
    assert refused(files=[("a/" + "x" * 300 + ".md", b"# x\n")]).code == "path_too_long"


def test_a_path_nested_over_ten_levels_is_refused():
    deep = "/".join(["d"] * 10) + "/x.md"                    # 11 levels with the file
    assert refused(files=[ROOT, (deep, b"# x\n")]).code == "too_deep"
    ok = "/".join(["d"] * 9) + "/x.md"                       # 10 levels
    assert ingest_files([ROOT, (ok, b"# x\n")]).files


def test_the_same_path_twice_is_refused_whatever_the_case():
    err = refused(files=[("Notes/A.md", b"# a\n"), ("notes/a.md", b"# b\n")])
    assert err.code == "duplicate_path"


def test_two_spellings_of_one_accented_name_are_one_path():
    err = refused(files=[("caf\u00e9.md", b"# a\n"), ("cafe\u0301.md", b"# b\n")])
    assert err.code == "duplicate_path"


# ─── The archive itself ──────────────────────────────────────────────


def test_a_symlink_among_the_files_is_refused():
    err = refused(make_zip({"a.md": "# A\n"}, symlinks=[("link.md", "/etc/passwd")]))
    assert err.code == "symlink"


def test_a_symlink_inside_macos_debris_is_ignored():
    result = ingest_zip(make_zip({"a.md": "# A\n"}, symlinks=[("__MACOSX/link", "/x")]))
    assert [f.path for f in result.files] == ["a.md"]


def test_an_encrypted_entry_is_refused():
    assert refused(make_zip({"a.md": "# A\n"}, flag_bits=True)).code == "encrypted"


def test_something_that_is_not_a_zip_is_refused():
    assert refused(b"this is not a zip at all").code == "bad_archive"
    assert refused(b"").code == "bad_archive"


def test_an_archive_over_five_megabytes_is_refused_before_it_is_opened():
    assert refused(b"\x00" * (ingest.MAX_ARCHIVE_BYTES + 1)).code == "archive_too_large"


def test_a_zip_bomb_is_refused_without_inflating_it():
    """50 MB of zeros deflate to a few KB: the declared size alone refuses it."""
    bomb = make_zip({"a.md": b"0" * (50 * 1024 * 1024)})
    assert len(bomb) < 200 * 1024
    assert refused(bomb).code == "file_too_large"


def test_many_small_files_that_add_up_are_refused_by_the_running_total():
    files = {f"f{i}.md": ("x" * (500 * 1024)) for i in range(20)}        # 10 MB, ~tiny zipped
    data = make_zip(files)
    assert len(data) < ingest.MAX_ARCHIVE_BYTES
    assert refused(data).code == "total_too_large"


def test_a_file_over_512_kb_is_refused():
    assert refused(files=[("big.md", b"x" * (512 * 1024 + 1))]).code == "file_too_large"
    assert ingest_files([("fit.md", b"x" * (512 * 1024))]).files


def test_more_than_400_files_are_refused():
    ok = [(f"n/{i}.md", b"# n\n") for i in range(400)]
    assert len(ingest_files(ok).files) == 400
    assert refused(files=[*ok, ("n/400.md", b"# n\n")]).code == "too_many_files"


def test_an_upload_with_nothing_usable_is_refused():
    assert refused(files=[("logo.png", b"\x89PNG")]).code == "no_files"
    assert refused(make_zip({})).code == "no_files"


# ─── Content ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("data", [b"\xff\xfe\x00bad", b"text\x00more", "caf\u00e9".encode("latin-1")])
def test_a_kept_file_that_is_not_utf8_text_is_refused(data):
    err = refused(files=[ROOT, ("notes/a.md", data)])
    assert err.code == "binary_file" and "notes/a.md" in err.message


@pytest.mark.parametrize("path", [
    "config/secrets.json", "keys/server.pem", "deploy/id_rsa", ".env", "app/.env.production",
    "creds/credentials.json",
])
def test_a_file_that_is_a_secret_by_its_name_is_refused(path):
    err = refused(files=[("ok.md", b"# ok\n"), (path, b"anything")])
    assert err.code == "secret_file"
    assert err.hits == [{"path": path, "line": 0, "label": "secret file"}]


def test_a_service_account_or_secret_manifest_is_refused_by_its_shape():
    sa = b'{\n  "type": "service_account",\n  "project_id": "p"\n}\n'
    assert refused(files=[("examples/auth.json", sa)]).code == "secret_file"
    manifest = b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\n"
    assert refused(files=[("examples/s.yaml", manifest)]).code == "secret_file"


def test_a_private_key_pasted_into_a_note_is_refused_and_not_repeated():
    body = secrets.token_urlsafe(40)
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\n"
    err = refused(files=[ROOT, ("notes/a.md", pem.encode())])
    assert err.code == "secret_file"
    assert [h["path"] for h in err.hits] == ["notes/a.md"]
    assert body not in repr(err.hits) + err.message + err.code


def test_a_secret_literal_is_reported_by_place_and_never_by_value():
    canary = secrets.token_urlsafe(12)
    text = f"# Setup\n\nCall it like this:\n\npassword = '{canary}'\n"
    err = refused(files=[ROOT, ("api/setup.md", text.encode())])
    assert err.code == "secrets_found"
    [hit] = err.hits
    assert (hit["path"], hit["line"]) == ("api/setup.md", 5) and hit["label"]
    assert canary not in (repr(err.hits) + err.message + str(err))


def test_placeholders_and_commit_hashes_are_not_secrets():
    text = ("# Auth\n\nSend `Authorization: Bearer <token>`.\n\nexport API_KEY=${API_KEY}\n\n"
            "Fixed in 3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e (see billing:src/client.py:42).\n")
    assert ingest_files([("api/auth.md", text.encode())]).files


# ─── Skills ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("front", [
    "---\nname: x\n---\n# no description\n",
    "---\nname: x\ndescription: \"\"\n---\n",
    "---\nname: Bad Name\ndescription: d\n---\n",
    "---\nname: x\ndescription: d\n",                                  # never closed
    "---\nname: x\ndescription: [unclosed\n---\n",                      # not YAML
    "---\n- just\n- a list\n---\n",
    "# no front matter at all\n",
])
def test_a_skill_needs_a_valid_name_and_a_description(front):
    err = refused(files=[ROOT, ("s/SKILL.md", front.encode())])
    assert err.code == "bad_front_matter" and "s/SKILL.md" in err.message


def test_a_skill_description_over_1536_characters_is_refused():
    text = f"---\nname: x\ndescription: {'d' * 1537}\n---\n"
    assert refused(files=[("s/SKILL.md", text.encode())]).code == "bad_front_matter"
    ok = f"---\nname: x\ndescription: {'d' * 1536}\n---\n"
    assert ingest_files([("s/SKILL.md", ok.encode())]).files


def test_yaml_aliases_are_refused_so_front_matter_cannot_balloon():
    bomb = "---\nname: x\ndescription: d\na: &a [1,2,3]\nb: &b [*a,*a,*a]\nc: [*b,*b,*b]\n---\n"
    err = refused(files=[("s/SKILL.md", bomb.encode())])
    assert err.code == "bad_front_matter"


def test_front_matter_over_8_kb_is_refused():
    text = "---\nname: x\ndescription: d\nnote: " + "z" * 9000 + "\n---\n"
    assert refused(files=[("s/SKILL.md", text.encode())]).code == "bad_front_matter"


def test_python_object_tags_in_front_matter_are_refused():
    text = "---\nname: x\ndescription: !!python/object/apply:os.system ['id']\n---\n"
    assert refused(files=[("s/SKILL.md", text.encode())]).code == "bad_front_matter"


def test_a_skill_inside_another_skill_is_refused():
    err = refused(files=[("a/SKILL.md", skill("a").encode()),
                         ("a/inner/SKILL.md", skill("b").encode())])
    assert err.code == "nested_skill"
    err = refused(files=[("SKILL.md", skill("root").encode()),
                         ("other/SKILL.md", skill("b").encode())])
    assert err.code == "nested_skill"


def test_two_skills_with_one_name_are_refused():
    err = refused(files=[("a/SKILL.md", skill("same").encode()),
                         ("b/SKILL.md", skill("same").encode())])
    assert err.code == "duplicate_skill"


def test_sibling_skills_each_own_their_files():
    result = ingest_files([
        ("a/SKILL.md", skill("a").encode()), ("a/ref.md", b"# a ref\n"),
        ("b/SKILL.md", skill("b").encode()), ("b/ref.md", b"# b ref\n"),
        ("notes/n.md", b"# n\n"),
    ])
    skills = {f.path: f.skill for f in result.files}
    assert skills == {"a/SKILL.md": "a", "a/ref.md": "a", "b/SKILL.md": "b",
                      "b/ref.md": "b", "notes/n.md": None}
    assert result.skills == ["a", "b"]


def test_repos_and_tags_must_be_short_lists_of_text():
    bad = "---\nrepos: {a: b}\n---\n# x\n"
    assert refused(files=[("notes/a.md", bad.encode())]).code == "bad_front_matter"
    many = "---\ntags: [" + ", ".join(f"t{i}" for i in range(31)) + "]\n---\n# x\n"
    assert refused(files=[("notes/a.md", many.encode())]).code == "bad_front_matter"
