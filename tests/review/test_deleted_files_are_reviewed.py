"""A pull request that deletes a file used to be reviewed as if it were empty.

A unified diff writes `+++ /dev/null` for a deletion, so the target side names
nothing. The parser took the path from the target side only, `_should_skip_file`
returns True for "/dev/null", and the file was dropped — into `skipped_files`,
as the literal string "/dev/null".

The reviewer then saw: "All 2 changed files were filtered out by skip patterns
(lockfiles, binaries, build dirs, etc.): - /dev/null - /dev/null". Not an error.
An affirmative, false explanation.

Removing a permission check or an auth guard is exactly the change class this
was blind to, on all three providers, including the only one connected in
production. `models.py` had documented the fallback for years — "new path
(post-change); old path if file deleted" — and it had never been written.
"""

# The fixtures below are verbatim unified diffs. In that format the context
# line for an EMPTY source line is a single space, so the one "blank line with
# whitespace" in this file (the last line of MODIFICATION) is payload, not
# stray formatting: strip it and the hunk is a line shorter than its own @@
# header declares, and the fixture stops being a diff these tests can parse.
# ruff: noqa: W293

from __future__ import annotations

from src.review.diff import parse_unified_diff

DELETION = """diff --git a/src/legacy_auth.py b/src/legacy_auth.py
deleted file mode 100644
index 83db48f..0000000
--- a/src/legacy_auth.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def check_permission(user):
-    return user.is_admin
-
"""

MODIFICATION = """diff --git a/src/app.py b/src/app.py
index 83db48f..f735c4e 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import os
+import sys
 
"""

ADDITION = """diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000..f735c4e
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+import os
+
"""


def test_a_deleted_file_reaches_the_reviewer():
    hunks, skipped = parse_unified_diff(DELETION)
    assert [h.file_path for h in hunks] == ["src/legacy_auth.py"]
    assert skipped == []


def test_it_is_marked_as_a_deletion():
    """`is_deleted_file` existed and was unreachable — the file never got far
    enough to carry it."""
    hunks, _ = parse_unified_diff(DELETION)
    assert hunks[0].is_deleted_file is True


def test_the_removed_lines_are_visible():
    """The point of reviewing a deletion is seeing what left."""
    hunks, _ = parse_unified_diff(DELETION)
    assert "check_permission" in hunks[0].content


def test_dev_null_never_reaches_a_human():
    """It was printed to the reviewer as the name of a changed file."""
    _hunks, skipped = parse_unified_diff(DELETION + MODIFICATION)
    assert "/dev/null" not in skipped
    assert not any("/dev/null" in s for s in skipped)


def test_a_delete_only_pull_request_is_not_reported_as_filtered_out():
    """The whole PR came back "everything was filtered out by skip patterns",
    which is a sentence about lockfiles and build directories."""
    hunks, skipped = parse_unified_diff(DELETION)
    assert hunks, "a delete-only PR still has nothing to review"
    assert not skipped


def test_additions_and_modifications_still_work():
    """The fix reads the source side only when the target side is /dev/null.
    A NEW file has `--- /dev/null` on the source side and must keep its new
    path, not become "/dev/null"."""
    hunks, skipped = parse_unified_diff(ADDITION + MODIFICATION)
    paths = sorted(h.file_path for h in hunks)
    assert paths == ["src/app.py", "src/new.py"]
    assert skipped == []
    added = [h for h in hunks if h.file_path == "src/new.py"][0]
    assert added.is_new_file is True
    assert added.is_deleted_file is False


def test_the_old_path_is_still_recorded_separately():
    hunks, _ = parse_unified_diff(DELETION)
    assert hunks[0].old_file_path == "src/legacy_auth.py"


def test_a_mixed_pull_request_counts_every_file():
    """`changed_files` drives the "Files changed: N" line in the posted
    summary, which undercounted by exactly the number of deletions."""
    hunks, _ = parse_unified_diff(DELETION + MODIFICATION + ADDITION)
    assert sorted({h.file_path for h in hunks}) == [
        "src/app.py", "src/legacy_auth.py", "src/new.py",
    ]
