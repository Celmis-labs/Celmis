"""What a review reads: the whole pull request, or only what is new.

`decide_scope` is the one place that says so, and every doubt in it ends in
the whole PR (a review that reads too much is slow, one that reads too little
is wrong). The helpers beside it keep an increment honest: a merged-in target
branch is not the author's change, a removed line outdates the comment on it,
and a finding that is already a comment is not posted twice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.review.diff import parse_unified_diff
from src.review.scope import (
    AUTOMATIC,
    CommitInfo,
    ReviewRequest,
    already_posted,
    decide_scope,
    drop_diff_sections,
    map_old_line,
    plan_threads,
    removed_old_lines,
    restrict_to_pr,
    same_added_lines,
    same_commit,
)

REVIEWED = "a1" * 20
MIDDLE = "b2" * 20
HEAD = "c3" * 20


def _commit(sha: str, *parents: str) -> CommitInfo:
    return CommitInfo(sha=sha, parents=tuple(parents))


def _history(*commits: CommitInfo):
    return lambda: list(commits)


def _decide(commits, *, setting="incremental", request=AUTOMATIC,
            last=REVIEWED, head=HEAD):
    return decide_scope(setting, request, last_reviewed_sha=last, head_sha=head,
                        list_commits=commits)


def _never_listed():
    raise AssertionError("the commits were listed although the answer did not need them")


# ─── the decision ────────────────────────────────────────────────────


def test_new_commits_after_the_reviewed_one_are_an_incremental_review() -> None:
    d = _decide(_history(_commit(HEAD, MIDDLE), _commit(MIDDLE, REVIEWED), _commit(REVIEWED)))

    assert (d.mode, d.code, d.base_sha, d.new_commits) == ("incremental", "incremental", REVIEWED, 2)


def test_a_first_review_reads_the_whole_pull_request_without_listing_commits() -> None:
    d = _decide(_never_listed, last=None)

    assert (d.mode, d.code) == ("full", "first_review")


@pytest.mark.parametrize("request_", [
    ReviewRequest(trigger="command", force=True),
    ReviewRequest(trigger="webhook", scope="full"),
])
def test_a_forced_or_full_pinned_request_is_always_whole(request_) -> None:
    d = _decide(_never_listed, request=request_)

    assert d.mode == "full"


def test_the_repository_setting_full_keeps_every_review_whole() -> None:
    d = _decide(_never_listed, setting="full")

    assert (d.mode, d.code) == ("full", "setting_full")


def test_a_request_pinned_incremental_overrides_a_full_setting() -> None:
    d = _decide(_history(_commit(HEAD, REVIEWED), _commit(REVIEWED)), setting="full",
                request=ReviewRequest(trigger="cli", scope="incremental"))

    assert d.mode == "incremental"


def test_the_same_head_is_a_quiet_skip_for_an_automatic_run() -> None:
    d = _decide(_never_listed, head=REVIEWED[:12])

    assert (d.mode, d.code) == ("skip", "no_new_commits")


@pytest.mark.parametrize("request_", [
    ReviewRequest(trigger="command"),
    ReviewRequest(trigger="manual"),
    ReviewRequest(trigger="command", resume=True),
])
def test_a_person_who_asks_again_gets_a_whole_review_not_a_quiet_skip(request_) -> None:
    d = _decide(_never_listed, head=REVIEWED, request=request_)

    assert (d.mode, d.code) == ("full", "asked_again")


def test_a_provider_that_cannot_list_the_commits_means_the_whole_pull_request() -> None:
    assert _decide(lambda: None).code == "commits_unavailable"


def test_a_listing_that_raises_means_the_whole_pull_request() -> None:
    def broken():
        raise RuntimeError("boom")

    assert _decide(broken).code == "commits_unavailable"


def test_a_force_push_that_dropped_the_reviewed_commit_is_a_whole_review() -> None:
    d = _decide(_history(_commit(HEAD, MIDDLE), _commit(MIDDLE)))

    assert (d.mode, d.code) == ("full", "history_rewritten")


def test_a_head_that_is_not_among_the_listed_commits_is_a_whole_review() -> None:
    d = _decide(_history(_commit(MIDDLE, REVIEWED), _commit(REVIEWED)))

    assert d.code == "history_rewritten"


def test_a_head_that_is_older_than_the_reviewed_commit_is_a_whole_review() -> None:
    d = _decide(_history(_commit(REVIEWED, HEAD), _commit(HEAD)), head=HEAD)

    assert d.code == "history_rewritten"


def test_new_merge_commits_only_are_a_quiet_skip() -> None:
    d = _decide(_history(_commit(HEAD, REVIEWED, "d4" * 20), _commit(REVIEWED)))

    assert (d.mode, d.code, d.new_commits) == ("skip", "only_merge_commits", 1)


def test_a_merge_commit_among_real_ones_does_not_skip() -> None:
    d = _decide(_history(_commit(HEAD, MIDDLE, "d4" * 20), _commit(MIDDLE, REVIEWED),
                         _commit(REVIEWED)))

    assert d.mode == "incremental" and d.new_commits == 2


def test_a_shortened_sha_from_a_webhook_names_the_same_commit() -> None:
    assert same_commit(REVIEWED[:12], REVIEWED)
    assert not same_commit(REVIEWED[:6], REVIEWED), "six characters name nothing"
    assert not same_commit(None, REVIEWED)


# ─── the increment ───────────────────────────────────────────────────

WHOLE_PR = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,6 @@
 one
+two
+three
+four
 five
 six
"""

INCREMENT = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -2,3 +2,3 @@
 two
-three
+three changed
 four
diff --git a/target_only.py b/target_only.py
--- a/target_only.py
+++ b/target_only.py
@@ -1,1 +1,2 @@
 keep
+pulled in from the target branch
"""


def _hunks(raw: str):
    return parse_unified_diff(raw)[0]


def test_a_file_the_pull_request_does_not_show_is_the_target_branchs_not_the_authors() -> None:
    kept = restrict_to_pr(_hunks(INCREMENT), _hunks(WHOLE_PR))

    assert [h.file_path for h in kept] == ["app.py"]


def test_the_sections_of_dropped_files_leave_the_text_the_agents_read() -> None:
    text = drop_diff_sections(INCREMENT, {"target_only.py"})

    assert "target_only.py" not in text and "app.py" in text
    assert drop_diff_sections("no headers here", {"x"}) == "no headers here"


def test_the_old_side_lines_an_increment_removed_outdate_the_comments_on_them() -> None:
    removed, deleted = removed_old_lines(_hunks(INCREMENT))

    assert removed["app.py"] == {3}
    assert deleted == set()


def test_a_deleted_file_is_reported_as_deleted() -> None:
    raw = ("diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n"
           "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n")

    removed, deleted = removed_old_lines(_hunks(raw))

    assert "gone.py" in deleted and removed["gone.py"] == {1, 2}


BASE = "1" * 40


def _thread(path, line, *, resolved=False, fp="f" * 16, sha=BASE[:12], side="RIGHT",
            line_is_current=False):
    return SimpleNamespace(path=path, line=line, resolved=resolved, fingerprint=fp,
                           sha=sha, side=side, line_is_current=line_is_current)


def test_threads_on_removed_lines_or_deleted_files_are_outdated_the_rest_stand() -> None:
    gone_line = _thread("app.py", 3)
    untouched = _thread("app.py", 40)
    in_deleted = _thread("old.py", 1)
    done = _thread("app.py", 3, resolved=True)

    plan = plan_threads(
        [gone_line, untouched, in_deleted, done], {"app.py": {3}}, {"old.py"}, base_sha=BASE)

    assert plan.resolve == [gone_line, in_deleted]
    assert plan.keep_open == [untouched]
    assert plan.resolved_before == 1


def test_a_finding_within_three_lines_of_our_open_comment_is_already_posted() -> None:
    open_threads = [_thread("app.py", 10, fp="a" * 16)]

    assert already_posted("a" * 16, "app.py", 13, open_threads)
    assert already_posted("a" * 16, "app.py", 7, open_threads)
    assert not already_posted("a" * 16, "app.py", 14, open_threads)
    assert not already_posted("b" * 16, "app.py", 10, open_threads), "another finding"
    assert not already_posted("a" * 16, "other.py", 10, open_threads), "another file"


def test_a_comment_with_no_line_covers_its_whole_file() -> None:
    assert already_posted("a" * 16, "app.py", 999, [_thread("app.py", None, fp="a" * 16)])


def test_a_thread_posted_at_another_commit_is_not_judged_by_this_increments_line_numbers() -> None:
    older = _thread("app.py", 3, sha="9" * 12)
    unknown = _thread("app.py", 3, sha=None)
    at_base = _thread("app.py", 3)

    plan = plan_threads([older, unknown, at_base], {"app.py": {3}}, set(), base_sha=BASE)

    assert plan.resolve == [at_base]
    assert plan.keep_open == [older, unknown], "their line is in somebody else's numbering"


def test_a_thread_on_the_target_side_or_already_moved_to_the_head_is_not_judged_by_old_lines() -> None:
    on_target_side = _thread("app.py", 3, side="LEFT")
    at_head = _thread("app.py", 3, line_is_current=True)

    plan = plan_threads([on_target_side, at_head], {"app.py": {3}}, set(), base_sha=BASE)

    assert plan.resolve == [] and plan.keep_open == [on_target_side, at_head]


def test_a_thread_in_a_deleted_file_is_outdated_whatever_commit_it_was_posted_on() -> None:
    older = _thread("old.py", 5, sha="9" * 12)

    assert plan_threads([older], {}, {"old.py"}, base_sha=BASE).resolve == [older]


SHIFT = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -2,0 +3,2 @@
+new one
+new two
@@ -10,3 +12,2 @@
 keep ten
-gone eleven
 keep twelve
"""


def test_a_line_is_carried_from_the_old_side_of_an_increment_to_its_new_side() -> None:
    hunks = _hunks(SHIFT)

    assert map_old_line(hunks, "app.py", 1) == 1, "above every hunk"
    assert map_old_line(hunks, "app.py", 5) == 7, "after the first hunk, which added two lines"
    assert map_old_line(hunks, "app.py", 10) == 12, "context inside a hunk"
    assert map_old_line(hunks, "app.py", 11) is None, "removed by the increment"
    assert map_old_line(hunks, "app.py", 30) == 31, "after both hunks: +2 and -1"
    assert map_old_line(hunks, "other.py", 5) == 5, "a file the increment did not touch"


def test_two_diffs_that_add_the_same_lines_are_told_apart_from_ones_that_do_not() -> None:
    assert same_added_lines(_hunks(INCREMENT), _hunks(INCREMENT))
    assert not same_added_lines(_hunks(INCREMENT), _hunks(SHIFT))
    assert not same_added_lines([], []), "nothing added is not 'the same change'"
