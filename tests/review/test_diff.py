"""Tests для diff parser + skip patterns."""

from __future__ import annotations

import pytest

from src.review.diff import (
    _should_skip_file,
    _strip_diff_prefix,
    parse_unified_diff,
)
from src.review.settings import ReviewSettings


@pytest.fixture
def settings() -> ReviewSettings:
    return ReviewSettings()


# ─── _strip_diff_prefix ─────────────────────────────────────────────


class TestStripPrefix:
    def test_a_prefix(self) -> None:
        assert _strip_diff_prefix("a/src/foo.py") == "src/foo.py"

    def test_b_prefix(self) -> None:
        assert _strip_diff_prefix("b/src/foo.py") == "src/foo.py"

    def test_dev_null(self) -> None:
        assert _strip_diff_prefix("/dev/null") == "/dev/null"

    def test_no_prefix(self) -> None:
        assert _strip_diff_prefix("src/foo.py") == "src/foo.py"


# ─── Skip patterns ──────────────────────────────────────────────────


class TestSkipPatterns:
    def test_lock_file_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("package-lock.json", settings) is True
        assert _should_skip_file("subdir/yarn.lock", settings) is True
        assert _should_skip_file("Cargo.lock", settings) is True

    def test_minified_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("dist/app.min.js", settings) is True
        assert _should_skip_file("public/style.min.css", settings) is True

    def test_node_modules_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("node_modules/lodash/index.js", settings) is True

    def test_vendor_dir_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("vendor/github.com/foo/bar.go", settings) is True

    def test_image_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("public/logo.png", settings) is True
        assert _should_skip_file("docs/diagram.svg", settings) is True

    def test_pb2_generated_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("api_pb2.py", settings) is True

    def test_normal_file_not_skipped(self, settings: ReviewSettings) -> None:
        assert _should_skip_file("src/main.py", settings) is False
        assert _should_skip_file("README.md", settings) is False
        assert _should_skip_file("Dockerfile", settings) is False


# ─── parse_unified_diff ─────────────────────────────────────────────


SAMPLE_DIFF = """diff --git a/src/foo.py b/src/foo.py
index 1234567..abcdefg 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,7 @@
 def existing_func():
-    return 1
+    return 2
+    # New behavior 1
+    # New behavior 2

 def other_func():
     pass
"""


SAMPLE_MULTI_FILE_DIFF = """diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 def a():
+    print("a")
     return 1

diff --git a/src/b.py b/src/b.py
index 333..444 100644
--- a/src/b.py
+++ b/src/b.py
@@ -1,3 +1,4 @@
 def b():
+    print("b")
     return 2

diff --git a/yarn.lock b/yarn.lock
index 555..666 100644
--- a/yarn.lock
+++ b/yarn.lock
@@ -1,1 +1,1 @@
-old
+new
"""


class TestParseDiff:
    def test_empty_diff(self, settings: ReviewSettings) -> None:
        hunks, skipped = parse_unified_diff("", settings=settings)
        assert hunks == []
        assert skipped == []

    def test_single_file_single_hunk(self, settings: ReviewSettings) -> None:
        hunks, skipped = parse_unified_diff(SAMPLE_DIFF, settings=settings)
        assert len(hunks) == 1
        h = hunks[0]
        assert h.file_path == "src/foo.py"
        assert h.old_file_path == "src/foo.py"
        assert h.added_lines == 3  # return 2 + 2 comments
        assert h.removed_lines == 1
        assert not h.is_new_file
        assert not h.is_deleted_file

    def test_skip_lock_file_in_multi_file_diff(
        self, settings: ReviewSettings,
    ) -> None:
        hunks, skipped = parse_unified_diff(SAMPLE_MULTI_FILE_DIFF, settings=settings)
        # 2 hunks (a.py, b.py) — yarn.lock skipped
        assert len(hunks) == 2
        files = {h.file_path for h in hunks}
        assert files == {"src/a.py", "src/b.py"}
        assert any("yarn.lock" in s for s in skipped)

    def test_garbage_diff_returns_empty(self, settings: ReviewSettings) -> None:
        hunks, skipped = parse_unified_diff("not a diff", settings=settings)
        assert hunks == []

    def test_max_files_limit(self, settings: ReviewSettings) -> None:
        """File limit обмежує hunks list."""
        # Generate diff з 5 files
        chunks = []
        for i in range(5):
            chunks.append(f"""diff --git a/f{i}.py b/f{i}.py
index 111..222 100644
--- a/f{i}.py
+++ b/f{i}.py
@@ -1,1 +1,2 @@
 line
+added
""")
        big_diff = "".join(chunks)

        # Limit до 2 files
        custom_settings = ReviewSettings(max_files_reviewed=2)
        hunks, skipped = parse_unified_diff(big_diff, settings=custom_settings)
        files = {h.file_path for h in hunks}
        assert len(files) == 2
        # Решта 3 у skipped з 'file limit reached' note
        assert sum(1 for s in skipped if "file limit" in s) == 3
