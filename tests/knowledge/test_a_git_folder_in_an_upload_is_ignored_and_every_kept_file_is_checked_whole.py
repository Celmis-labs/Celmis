"""An uploaded `.git/` is listed and dropped, never refused and never read; a kept file is checked whole.

THE DEFECT. The secret classifier learned that anything under `.git/` is a secret file, so a folder or a zip that
carried its `.git/` (the usual "zip the project" and "pick the folder" uploads) was REFUSED with "the upload contains
files that look like secrets" instead of ignored and listed as before. And the content check of an upload looked at the
first 8 KiB of a file only, so a private key or a `Secret` manifest after a long preamble was kept.

THE CONTRACT.
  * a `.git` segment (however it is spelled) puts the path in `ignored`: it is never stored, never read, and it does
    not make the upload a refusal; everything else in the upload is judged as before;
  * the content check of a kept file reads all of it (it is at most `MAX_FILE_BYTES`), in any line ending and layout
    the secret classifier knows.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from src.knowledge import ingest
from src.knowledge.ingest import IngestError, ingest_directory, ingest_files, ingest_zip

SKILL = b"---\nname: billing\ndescription: How to call the billing client.\n---\n\n# Billing\n"
TOKEN_URL = b"[remote \"origin\"]\n\turl = https://ci:CANARY_TOKEN_8d2f@example.com/acme/app.git\n"
PAD = b"# a long preamble line that is plain prose and nothing else\n" * 200          # ~12 KB
PEM = b"-----BEGIN RSA PRIVATE KEY-----\nCANARYKEYBODY\n-----END RSA PRIVATE KEY-----\n"
K8S = b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\ndata:\n  p: CANARY\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _refusal(files: list[tuple[str, bytes]]) -> IngestError:
    with pytest.raises(IngestError) as caught:
        ingest_files(files)
    return caught.value


# ─── .git ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("git", [".git", ".GIT", ".Git", ".git ", ".git.", "．git", ".​git"])
def test_a_git_folder_in_the_upload_is_ignored_and_listed_not_refused(git):
    files = [(f"kb/{git}/config", TOKEN_URL), (f"kb/{git}/objects/ab/cdef", b"\x78\x01blob"),
             (f"kb/{git}/HEAD", b"ref: refs/heads/main\n"), ("kb/SKILL.md", SKILL)]

    for result in (ingest_files(files), ingest_zip(_zip(dict(files)))):
        assert [f.path for f in result.files] == ["SKILL.md"]
        assert len(result.ignored) == 3 and all("config" in p or "cdef" in p or "HEAD" in p for p in result.ignored)
        assert result.ignored_total == 3
        assert b"CANARY_TOKEN_8d2f" not in b"".join(f.content.encode() for f in result.files)


def test_a_git_folder_deeper_in_the_tree_is_ignored_too():
    result = ingest_files([("kb/vendor/lib/.git/config", TOKEN_URL), ("kb/a.md", b"# A\n")])

    assert [f.path for f in result.files] == ["a.md"]
    assert result.ignored == ["kb/vendor/lib/.git/config"]


def test_a_git_file_of_a_submodule_is_ignored_like_the_folder():
    result = ingest_files([("kb/sub/.git", b"gitdir: ../.git/modules/sub\n"), ("kb/a.md", b"# A\n")])

    assert [f.path for f in result.files] == ["a.md"] and result.ignored == ["kb/sub/.git"]


def test_a_git_folder_is_never_read_and_does_not_hide_a_real_secret_beside_it(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(TOKEN_URL)
    (tmp_path / "a.md").write_bytes(b"# A\n")
    seen = []
    real = ingest._read_file
    monkeypatch.setattr(ingest, "_read_file", lambda p, n: (seen.append(p.name), real(p, n))[1])

    result = ingest_directory(tmp_path)

    assert [f.path for f in result.files] == ["a.md"] and ".git" in result.ignored
    assert seen == ["a.md"], "nothing under .git/ is opened"

    (tmp_path / ".env").write_bytes(b"A=1\n")
    with pytest.raises(IngestError) as caught:
        ingest_directory(tmp_path)
    assert caught.value.code == "secret_file" and [h["path"] for h in caught.value.hits] == [".env"]


def test_a_secret_next_to_a_git_folder_still_refuses_the_upload_and_names_only_the_secret():
    err = _refusal([("kb/.git/config", TOKEN_URL), ("kb/id_rsa", PEM), ("kb/a.md", b"# A\n")])

    assert err.code == "secret_file" and [h["path"] for h in err.hits] == ["kb/id_rsa"]  # refused by name, before the folder is stripped


def test_a_dotted_name_that_is_not_git_is_still_judged_by_its_name():
    ignored = ingest_files([("kb/.gitignore", b"x\n"), ("kb/.github/ci.yml", b"a: 1\n"), ("kb/a.md", b"# A\n")]).ignored
    assert set(ignored) == {"kb/.gitignore", "kb/.github/ci.yml"}
    assert _refusal([("kb/.aws/credentials", b"x\n"), ("kb/a.md", b"# A\n")]).code == "secret_file"


# ─── the whole file is sniffed ───────────────────────────────────────

@pytest.mark.parametrize(("path", "body"), [
    ("kb/notes.md", PAD + PEM),
    ("kb/notes.txt", PAD + PEM),
    ("kb/deploy.yaml", PAD + K8S),
    ("kb/deploy.yaml", PAD.replace(b"\n", b"\r\n") + K8S.replace(b"\n", b"\r\n")),
    ("kb/deploy.yaml", PAD + b"items:\n- apiVersion: v1\n  kind: Secret\n  data:\n    p: CANARY\n"),
    ("kb/deploy.yaml", PAD + b"{apiVersion: v1, kind: Secret, data: {p: CANARY}}\n"),
    ("kb/sa.json", PAD + b'{"type": "service_account", "private_key_id": "CANARY"}\n'),
    ("kb/sa.yaml", PAD + b"type: service_account\nprivate_key_id: CANARY\n"),
    ("kb/win.txt", (b"x\n" * 6000 + PEM).decode().encode("utf-16")),
])
def test_a_secret_after_a_long_preamble_refuses_the_upload(path, body):
    assert len(body) > 8192

    err = _refusal([(path, body), ("kb/a.md", b"# A\n")])

    assert err.code == "secret_file" and [h["path"] for h in err.hits] == [path.split("/", 1)[1]]
    assert "CANARY" not in err.args[0] and "CANARY" not in repr(err.hits)


def test_a_large_plain_note_and_a_source_file_that_mentions_the_words_are_kept():
    ts = b'export interface Node {\n  kind: "Secret";\n  type: "service_account";\n}\n'
    result = ingest_files([("kb/long.md", PAD * 30), ("kb/node.ts", ts), ("kb/a.md", b"# A\n")])

    assert sorted(f.path for f in result.files) == ["a.md", "long.md", "node.ts"]
