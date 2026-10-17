"""A folder pick is counted the way the server counts it.

The server keeps text files only (`TEXT_EXTENSIONS` in `src/knowledge/ingest.py`)
and ignores hidden paths; its limit of 400 files counts the files KEPT. The
upload dialog used to count everything it was handed, so a folder of 150 notes
and 300 images was refused by the page although the server would have stored 150
files. `web/lib/codebase.ts` mirrors the two rules; this holds the mirror equal
to the server's, as the `celmis-pack` tool's is held:

* the list of extensions is the server's list, member for member;
* for every path in a long table the page says "kept" exactly when the server's
  own functions say the file is neither hidden nor of another format;
* the dialog counts those files against the limit and not the rest.
"""

from __future__ import annotations

import json
import re

import pytest

from src.knowledge import ingest
from tests.web.test_a_configured_reasoning_setting_survives_the_save import TSC, WEB
from tests.web.test_the_seams_between_page_and_server_hold import _compile_and_run

LIB = WEB / "lib" / "codebase.ts"
DIALOG = WEB / "components" / "codebase" / "upload-version.tsx"

pytestmark = pytest.mark.skipif(not TSC.exists(), reason="web deps not installed")

PATHS = [
    "README.md", "readme.MD", "SKILL.md", "notes/why.markdown", "a/b/c/d.txt", "conf.YAML", "x.yml",
    "data.json", "pyproject.toml", "setup.cfg", "app.ini", "run.sh", "script.ps1", "tool.py",
    "main.go", "lib.rs", "Main.java", "q.sql", "api/call.http", "schema.graphql", "msg.proto",
    "page.html", "style.scss", "main.tf", "table.csv", "gradle/build.gradle", ".env.example",
    "config/prod.env.example", "CONFIG.ENV.EXAMPLE", "logo.png", "photo.JPG", "doc.pdf", "bundle.zip",
    "archive.tar.gz", "binary.exe", "Makefile", "LICENSE", "id_rsa", "key.pem", "noext", "dir.md/file",
    "dir.md/README", ".md", "a/.md", "a.", "a/b.", ".hidden/notes.md", "notes/.hidden.md",
    "a/.git/config.txt", "__MACOSX/notes.md", "a/__MACOSX/x.md", "a/__MACOSX_notes/x.md",
    "node_modules/x/readme.md", ".env", ".gitignore", "x.mdx", "x.md.bak", "x.bak.md", "ünï/текст.md",
    "ünï/текст.PNG", "spaces in/name.txt", "a/b.TXT/c.png",
]


def server_keeps(path: str) -> bool:
    return ingest._extension_allowed(path) and not ingest._is_hidden(path)


def run(tmp_path, body: str):
    return _compile_and_run(
        tmp_path, {"codebase.ts": LIB.read_text(encoding="utf-8")},
        'import * as cb from "./codebase";\n' + body)


def test_the_page_lists_the_same_extensions_as_the_server(tmp_path):
    got = run(tmp_path, "console.log(JSON.stringify(cb.TEXT_EXTENSIONS));")
    assert len(got) == len(set(got)), "an extension is listed twice"
    assert set(got) == set(ingest.TEXT_EXTENSIONS)


def test_the_page_keeps_exactly_the_files_the_server_keeps(tmp_path):
    got = run(tmp_path, f"""
const paths: string[] = {json.dumps(PATHS)};
console.log(JSON.stringify(paths.map((p) => [cb.keptByServer(p), cb.hasTextExtension(p), cb.isHiddenPath(p)])));
""")
    for path, (kept, has_extension, hidden) in zip(PATHS, got, strict=True):
        assert kept == server_keeps(path), path
        assert has_extension == ingest._extension_allowed(path), path
        assert hidden == ingest._is_hidden(path), path


def test_a_local_skip_is_never_something_the_server_would_have_kept(tmp_path):
    """The page may leave out more than the server ignores (dependency folders),
    never less of what the server would refuse to keep and certainly never a
    text file the server would store outside `node_modules`."""
    got = run(tmp_path, f"""
const paths: string[] = {json.dumps(PATHS)};
console.log(JSON.stringify(paths.map((p) => cb.skippedLocally(p))));
""")
    for path, skipped in zip(PATHS, got, strict=True):
        if server_keeps(path) and "node_modules" not in path.split("/"):
            assert not skipped, path
        if ingest._is_hidden(path):
            assert skipped, path


def test_a_folder_of_notes_and_images_is_not_too_many_for_the_notes(tmp_path):
    notes = [f"kit/notes/n{i}.md" for i in range(150)]
    images = [f"kit/img/i{i}.png" for i in range(300)]
    got = run(tmp_path, f"""
const picked: string[] = {json.dumps(notes + images)};
const kept = picked.filter((p) => cb.keptByServer(p)).length;
console.log(JSON.stringify({{ all: picked.length, kept, over: kept > cb.UPLOAD_LIMITS.files }}));
""")
    assert got == {"all": 450, "kept": 150, "over": False}


def test_more_kept_files_than_the_limit_are_still_too_many(tmp_path):
    notes = [f"kit/n{i}.md" for i in range(ingest.MAX_FILES + 1)]
    got = run(tmp_path, f"""
const picked: string[] = {json.dumps(notes)};
console.log(JSON.stringify(picked.filter((p) => cb.keptByServer(p)).length > cb.UPLOAD_LIMITS.files));
""")
    assert got is True


def test_the_dialog_compares_the_kept_files_with_the_limit_and_says_how_many_it_will_keep():
    code = re.sub(r"/\*.*?\*/", "", DIALOG.read_text(encoding="utf-8"), flags=re.S)
    assert "keptByServer(relativePathOf(f))" in code
    assert 'const tooMany = mode === "folder" && (folder?.kept ?? 0) > UPLOAD_LIMITS.files;' in code
    assert "count: folder.kept" in code
    assert "files.length ?? 0) > UPLOAD_LIMITS.files" not in code
