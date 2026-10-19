"""No tracked document or script tells a macOS user to store a token at the
prompt of `security add-generic-password ... -w`.

That prompt (the `-w` with no value after it) keeps only the first 128 characters. An
MCP token is a JWT of about 450, so a person who followed the instructions stored
two of its three parts and got `401 invalid_token`. The form the instructions use
instead hands the whole value to `security -i` on its standard input, from the
clipboard, and `printf` is a shell builtin, so the token never shows in the process
list either:

    printf 'add-generic-password -U -s <service> -a %s -w %s\\n' "$USER" "$(pbpaste)" | security -i; pbcopy </dev/null

Reading a token (`find-generic-password ... -w`) is a different command and is fine.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.plugin import contract as C

THIS_FILE = C.ROOT / "tests" / "plugin" / "test_no_document_or_script_stores_a_token_through_the_prompt_that_cuts_it_at_128_characters.py"

#: `add-generic-password`, then `-w` as the last option: followed by the end of the
#: line, a closing quote, backtick or bracket, a pipe, or another option. A `-w` that
#: is followed by a value (`-w %s`, `-w "$(pbpaste)"`) does not prompt.
PROMPTING = re.compile(
    r"add-generic-password\b[^\n]*?\s-w(?:(?=[`'\")|;&]|$)|(?=[ \t]+(?:-[A-Za-z]|[`|;&)]|$)))",
    re.MULTILINE)

RECOMMENDED = ("printf 'add-generic-password -U -s {service} -a %s -w %s\\n' \"$USER\" "
               "\"$(pbpaste | tr -cd 'A-Za-z0-9._~+/=-')\" | security -i && pbcopy </dev/null")

SKIPPED_DIRS = {".git", "node_modules", ".venv", "venv", ".next", "__pycache__", ".mypy_cache", ".ruff_cache"}
MAX_BYTES = 2 * 1024 * 1024


def tracked_files():
    # Untracked files too: a new file must pass before it is committed, not after.
    done = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          cwd=C.ROOT, capture_output=True, text=True, check=False)
    if done.returncode == 0 and done.stdout:
        return [C.ROOT / name for name in done.stdout.split("\0") if name]
    return [p for p in C.ROOT.rglob("*")                      # no git here (a source archive)
            if p.is_file() and not SKIPPED_DIRS & set(p.relative_to(C.ROOT).parts)]


def text_of(path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


@pytest.mark.parametrize("line", [
    'security add-generic-password -s celmis-token -a "$USER" -w',
    '`security add-generic-password -s celmis-pack-token -a "$USER" -w` on macOS',
    '# macOS: security add-generic-password -s celmis-pack-token -a "$USER" -w\n# Linux: secret-tool',
    "security add-generic-password -w -s celmis-token -a me",
    '(security add-generic-password -s x -a "$USER" -w)',
    "sh -c 'security add-generic-password -s x -a me -w'",
    "security add-generic-password -a me -s x -w | cat",
])
def test_the_check_recognises_the_prompting_form(line):
    assert PROMPTING.search(line), line


@pytest.mark.parametrize("line", [
    RECOMMENDED.format(service="celmis-token"),
    RECOMMENDED.format(service="celmis-pack-token"),
    'security add-generic-password -s x -a me -w "$(pbpaste)"',
    "security add-generic-password -s x -a me -w 'a-value'",
    "security find-generic-password -s celmis-token -w",
    'token=$(security find-generic-password -s celmis-token -w 2>/dev/null || true)',
    "secret-tool store --label celmis-token service celmis-token",
])
def test_the_check_leaves_the_forms_that_keep_the_whole_value(line):
    assert not PROMPTING.search(line), line


def test_no_tracked_document_or_script_recommends_the_prompting_form():
    hits = []
    for path in tracked_files():
        if path == THIS_FILE:
            continue
        text = text_of(path)
        for m in PROMPTING.finditer(text):
            hits.append(f"{path.relative_to(C.ROOT)}:{text.count(chr(10), 0, m.start()) + 1}")
    assert hits == [], ("these tell macOS users to type a token at the prompt of `security`, which cuts it "
                        f"at 128 characters; use the printf ... | security -i form instead: {hits}")


@pytest.mark.parametrize("path,service", [
    (C.PLUGIN / "README.md", "celmis-token"),
    (C.PLUGIN / "README.md", "celmis-pack-token"),
    (C.PLUGIN / "bin" / "celmis-auth-header", "celmis-token"),
    (C.ROOT / "docs" / "CODEBASE.md", "celmis-pack-token"),
])
def test_where_a_token_is_stored_the_whole_value_form_is_what_is_shown(path, service):
    assert RECOMMENDED.format(service=service) in text_of(path), path.relative_to(C.ROOT)


def test_the_plugin_readme_says_why_the_prompt_is_not_used():
    readme = " ".join(text_of(C.PLUGIN / "README.md").split())
    assert "keeps only the first 128 characters" in readme and "cuts an MCP token" in readme
