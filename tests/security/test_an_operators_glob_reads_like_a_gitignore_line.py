"""`SECRET_PATH_GLOBS_EXTRA` is read like a `.gitignore` line: a name hides its subtree, `**/` reaches the root.

THE DEFECT. The operator's globs were `fnmatch`ed against the whole path and against the file name, nothing else. So
`**/report.txt` did not hide `report.txt` at the repository root (the `/` of `**/` had nothing to match), and a
directory was hidden only by spelling the files below it: `config`, `config/`, `/config/*` and `./config/*` all left
`config/app.txt` readable (and `config/` was not even a way to name a directory).

THE CONTRACT (documented in docs/ROLES_AND_ACCESS.md and `.env.example`).
  * a pattern with no `/` in it, or starting with `**/`, applies at any depth, the repository root included;
  * any other `/`, a leading one included, ties it to the repository root; a leading `./` is dropped;
  * a pattern hides what it names and everything below it: `config` hides the file `config` and `config/app.txt`;
  * a trailing `/` names a directory only: `config/` hides `src/config/app.txt` and not a file called `config`;
  * `/**/` also matches no directory (`a/**/b.txt` hides `a/b.txt`); `*` still matches `/` (`deploy/*` reaches
    `deploy/a/b`); `?` and `[abc]` are as in `fnmatch`; the comparison is case-blind, `\\` is `/`.
Globs only ever add to the built-in lists.
"""

from __future__ import annotations

import pytest

from src.security import secret_files as sf

HIDDEN, SHOWN = "deny", "ok"

#: (glob, path, verdict): the table of the findings that started this, then the rules above.
TABLE = [
    # a leading **/ reaches the root
    ("**/report.txt", "report.txt", HIDDEN),
    ("**/report.txt", "a/report.txt", HIDDEN),
    ("**/report.txt", "a/b/c/report.txt", HIDDEN),
    ("**/report.txt", "xreport.txt", SHOWN),
    ("**/report.txt", "a/report.txt.md", SHOWN),
    ("**/deploy/*.yml", "deploy/a.yml", HIDDEN),
    ("**/deploy/*.yml", "x/y/deploy/a.yml", HIDDEN),
    ("**/deploy/*.yml", "x/notdeploy/a.yml", SHOWN),
    ("**/**/x.txt", "x.txt", HIDDEN),
    # a bare directory name hides its subtree, at any depth
    ("config", "config/app.txt", HIDDEN),
    ("config", "src/config/app.txt", HIDDEN),
    ("config", "src/config/deep/er/app.txt", HIDDEN),
    ("config", "config", HIDDEN),
    ("config", "src/config", HIDDEN),
    ("config", "configs/app.txt", SHOWN),
    ("config", "src/myconfig/app.txt", SHOWN),
    ("config", "src/config.py", SHOWN),
    # a trailing slash: a directory, at any depth, its subtree
    ("config/", "config/app.txt", HIDDEN),
    ("config/", "src/config/app.txt", HIDDEN),
    ("config/", "src/config", SHOWN),                  # a FILE called config is not the directory
    ("config/", "configs/app.txt", SHOWN),
    ("a/b/", "a/b/c.txt", HIDDEN),
    ("a/b/", "x/a/b/c.txt", SHOWN),                    # a slash inside ties it to the root
    # a leading slash, a leading ./ and a path: tied to the root
    ("/config/*", "config/app.txt", HIDDEN),
    ("/config/*", "config/deep/er/app.txt", HIDDEN),
    ("/config/*", "src/config/app.txt", SHOWN),
    ("./config/*", "config/app.txt", HIDDEN),
    ("./config/*", "src/config/app.txt", SHOWN),
    ("././config/*", "config/app.txt", HIDDEN),
    ("/config", "config/app.txt", HIDDEN),
    ("/config", "src/config/app.txt", SHOWN),
    ("config/*", "config/app.txt", HIDDEN),
    ("config/*", "src/config/app.txt", SHOWN),
    ("deploy/internal", "deploy/internal/x.txt", HIDDEN),
    ("deploy/internal", "deploy/internal", HIDDEN),
    ("deploy/internal", "deploy/internals/x.txt", SHOWN),
    ("deploy/*", "deploy/a/b/c.txt", HIDDEN),
    # /**/ matches no directory too
    ("a/**/b.txt", "a/b.txt", HIDDEN),
    ("a/**/b.txt", "a/x/b.txt", HIDDEN),
    ("a/**/b.txt", "a/x/y/b.txt", HIDDEN),
    ("a/**/b.txt", "b.txt", SHOWN),
    ("a/**/b.txt", "c/a/b.txt", SHOWN),
    ("docs/**", "docs/a/b.md", HIDDEN),
    ("docs/**", "src/docs/a.md", SHOWN),
    ("a/**/b.txt", "a/xb.txt", SHOWN),                 # the joint is a whole name or none: `a/x` + `b.txt` is not it
    ("a/**/b.txt", "a/x/yb.txt", SHOWN),
    ("a/**/b/**/c.txt", "a/b/c.txt", HIDDEN),
    ("a/**/b/**/c.txt", "a/x/b/y/z/c.txt", HIDDEN),
    ("a/**/b/**/c.txt", "a/x/xb/c.txt", SHOWN),
    ("?/**/b", "x/^b", SHOWN),
    ("**/b/c", "a/b/c", HIDDEN),
    ("**/b/c", "a/xb/c", SHOWN),
    ("**/b/c", "a/b/cd", SHOWN),
    # names, as before
    ("*.example", "config/app.example", HIDDEN),
    ("*.example", "app.example", HIDDEN),
    ("*.example", "app.examples", SHOWN),
    (".env.example", "api/.env.example", HIDDEN),
    ("internal-*.txt", "deploy/internal-notes.txt", HIDDEN),
    ("Deploy/Internal-*.txt", "deploy/internal-notes.txt", HIDDEN),
    ("Deploy/Internal-*.txt", "src/deploy/internal-notes.txt", SHOWN),
    ("[ab]*.txt", "a1.txt", HIDDEN),
    ("[ab]*.txt", "x/b1.txt", HIDDEN),
    ("[ab]*.txt", "c1.txt", SHOWN),
    ("[!ab]*.txt", "c1.txt", HIDDEN),
    ("[!ab]*.txt", "a1.txt", SHOWN),
    ("app?.txt", "app1.txt", HIDDEN),
    ("app?.txt", "app12.txt", SHOWN),
    ("[a-c]x", "bx", HIDDEN),
    ("[a-c]x", "dx", SHOWN),
    ("[z-a]x", "zx", SHOWN),                           # a reversed range names nothing (and does not break the read)
    ("[!/]x", "ax", HIDDEN),
    ("[!/]x", "a/x", SHOWN),                           # a class that leaves out `/` does not read one
    ("ç*.txt", "src/Ça.txt", HIDDEN),                  # not ASCII, and case-folded
    ("a[b.txt", "a[b.txt", HIDDEN),                    # an unclosed [ is a plain character
    ("a+b(1).txt", "x/a+b(1).txt", HIDDEN),            # regular-expression characters are plain
    # case-blind, like the filesystems the files come from
    ("CONFIG/", "config/app.txt", HIDDEN),
    ("config", "src/Config/APP.TXT", HIDDEN),
    ("**/Report.TXT", "a/report.txt", HIDDEN),
    ("config", "src/Ｃonfig/app.txt", HIDDEN),     # a fullwidth C reads as C
    # a backslash is a slash
    ("deploy\\internal", "deploy/internal/x.txt", HIDDEN),
    ("config\\", "src/config/app.txt", HIDDEN),
    # the example env names too (the glob decides before them)
    ("config", "config/.env.example", HIDDEN),
    ("**/.env.example", ".env.example", HIDDEN),
    ("config/", "src/.env.example", "keys_only"),
    # an empty or slash-only glob names nothing
    ("", "src/app.py", SHOWN),
    ("/", "src/app.py", SHOWN),
    ("./", "src/app.py", SHOWN),
        ("  ", "src/app.py", SHOWN),
    # ... while `*`, `**` and `**/` name everything: an operator who writes them hides the whole repository
    ("*", "src/app.py", HIDDEN),
    ("**", "src/app.py", HIDDEN),
    ("**/", "src/app.py", HIDDEN),
]


@pytest.mark.parametrize(("glob", "rel", "verdict"), TABLE, ids=[f"{g!r}-{r}" for g, r, _ in TABLE])
def test_a_glob_hides_what_it_names_and_what_is_below_it(monkeypatch, glob, rel, verdict):
    monkeypatch.setattr(sf, "_extra_globs", lambda: (glob,))

    assert sf.classify(rel) == verdict
    assert sf.is_secret_path(rel) is (verdict != SHOWN)


def test_several_globs_hide_what_any_of_them_names(monkeypatch):
    monkeypatch.setattr(sf, "_extra_globs", lambda: ("config/", "**/vault.dat", "/private"))

    assert [sf.classify(p) for p in ("a/config/x", "vault.dat", "private/x", "a/private/x", "src/app.py")] \
        == [HIDDEN, HIDDEN, HIDDEN, SHOWN, SHOWN]


def test_a_glob_never_unhides_what_the_built_in_lists_hide(monkeypatch):
    monkeypatch.setattr(sf, "_extra_globs", lambda: ("docs/", "**/app.py"))

    assert sf.classify(".env") == HIDDEN
    assert sf.classify("keys/id_rsa") == HIDDEN
    assert sf.classify(".git/config") == HIDDEN


def test_a_hidden_directory_is_hidden_from_every_reader(monkeypatch, tmp_path):
    from src.mcp_server.howto import engine
    from src.qa import text_search
    from src.retrieval.tier3_code import CodeReader

    root = tmp_path / "acme-app"
    for rel in ("config/app.txt", "src/config/deep/app.txt", "src/ok.txt", "report.txt", "src/report.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("CANARY_NEEDLE_77\n")
    monkeypatch.setattr(sf, "_extra_globs", lambda: ("config/", "**/report.txt"))

    for rel in ("config/app.txt", "src/config/deep/app.txt", "report.txt", "src/report.txt"):
        assert sf.plain_file(root, rel) is None, rel
        with pytest.raises(sf.SecretPathRefused):
            sf.safe_join(root, rel)
        assert CodeReader().read_full_file(root, rel) is None, rel
        assert engine._read(root, rel) is None, rel
    assert sf.plain_file(root, "src/ok.txt") is not None
    assert {rel for rel, _ in text_search.iter_text_files(root)} == {"src/ok.txt"}


def test_the_setting_reaches_the_classifier_with_the_new_forms(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("SECRET_PATH_GLOBS_EXTRA", '["**/vault.dat", "config/", "/private"]')
    get_settings.cache_clear()
    try:
        assert [sf.classify(p) for p in ("vault.dat", "src/config/a.txt", "private/a.txt", "src/a.py")] \
            == [HIDDEN, HIDDEN, HIDDEN, SHOWN]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_a_long_path_against_many_globs_is_judged_quickly(monkeypatch):
    import time

    monkeypatch.setattr(sf, "_extra_globs", lambda: tuple(f"*{i}*x*y*z*.nomatch" for i in range(50)))
    rel = "/".join(["segment-with-some-length"] * 12) + "/file.txt"
    t0 = time.perf_counter()
    assert sf.classify(rel) == SHOWN
    assert time.perf_counter() - t0 < 2
