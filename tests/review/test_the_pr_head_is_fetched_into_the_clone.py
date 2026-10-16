"""The head commit of an OPEN pull request is made readable in the clone.

Real throw-away git repositories throughout: a bare "origin", a clone that
stops at the default branch, and a pull request branch the clone has never
seen. A mock of `git fetch` would test the mock.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from src.review import head_fetch
from src.review.agents import verifier_evidence
from src.review.code_context import CodeSource
from src.review.models import Hunk, PullRequest
from src.review.structural_constants import find_undefined_constants

MODEL = "<?php\nclass Model {\n    const STATUS_PENDING = 1;\n}\n"
USE = "<?php\nclass Use1 {\n    function f() { return Model::STATUS_PENDNG; }\n}\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """(origin, clone, pr): the PR branch exists only on origin."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "Model.php").write_text(MODEL)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(work, "checkout", "-q", "-b", "feature/x")
    (work / "Use.php").write_text(USE)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "pr")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "feature/x")
    lines = USE.splitlines()
    hunk = Hunk(file_path="Use.php", old_file_path="Use.php", old_start=0, old_count=0,
                new_start=1, new_count=len(lines),
                content=f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines))
    pr = PullRequest(provider="github", repo="o/n", number=7, title="t", description="",
                     author="a", base_ref="main", base_sha="b", head_ref="feature/x",
                     head_sha=sha, state="open", hunks=[hunk])
    monkeypatch.setattr(head_fetch, "_find_clone", lambda _pr: clone)
    monkeypatch.setattr(head_fetch, "_git_env", lambda *_a, **_k: dict(os.environ))
    return origin, clone, pr


def _has(clone: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(clone), "cat-file", "-e", f"{sha}^{{commit}}"],
                          capture_output=True).returncode == 0


def test_a_missing_head_is_fetched_and_the_three_consumers_use_it(world):
    _origin, clone, pr = world
    assert not _has(clone, pr.head_sha)
    res = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    assert res.available and res.state == "fetched"
    assert res.sentence == "head fetched into clone"
    assert _has(clone, pr.head_sha)

    # The working tree, HEAD and branches are untouched; the ref is private.
    assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not (clone / "Use.php").exists()
    assert _git(clone, "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
    assert _git(clone, "rev-parse", "refs/celmis/pr-heads/7") == pr.head_sha

    src = CodeSource(pr, clone, None)
    assert src.clone_has_head and "STATUS_PENDNG" in (src.read("Use.php") or "")
    assert verifier_evidence._has_commit(clone, pr.head_sha)
    assert "STATUS_PENDNG" in (verifier_evidence._show(clone, pr.head_sha, "Use.php") or "")
    found = find_undefined_constants(pr, clone)
    assert [f.title for f in found] == ["Undefined class constant Model::STATUS_PENDNG"]
    assert "not available locally" not in found[0].body


def test_a_second_call_finds_the_head_already_there(world):
    _o, _c, pr = world
    assert head_fetch.ensure_head_in_clone(pr, timeout_s=30).state == "fetched"
    again = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    assert again.state == "present" and again.available


def test_a_deleted_branch_falls_back_to_fetching_the_sha(world):
    origin, clone, pr = world
    # Keep the commit reachable on origin under another ref, drop the branch.
    subprocess.run(["git", "-C", str(origin), "update-ref", "refs/keep/x", pr.head_sha], check=True)
    subprocess.run(["git", "-C", str(origin), "update-ref", "-d", "refs/heads/feature/x"], check=True)
    subprocess.run(["git", "-C", str(origin), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
    res = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    assert res.available and _has(clone, pr.head_sha)


def test_a_failed_fetch_is_fail_open_and_says_why(world, tmp_path):
    origin, clone, pr = world
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    res = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    assert not res.available and res.state == "unavailable"
    assert res.sentence.startswith("unavailable: ") and len(res.sentence) > 14
    assert not _has(clone, pr.head_sha)


def test_the_result_is_one_stage_with_its_sentence(world):
    from src.review.stages import StageRecorder

    _o, _c, pr = world
    stages = StageRecorder()
    head_fetch.record_head_fetch(stages, head_fetch.ensure_head_in_clone(pr, timeout_s=30))
    st = stages.get("head_fetch")
    assert st["status"] == "success" and st["reason"] == "head fetched into clone"


def test_an_unusable_branch_name_is_not_passed_to_git(world):
    _o, _c, pr = world
    pr.head_ref = "--upload-pack=touch /tmp/x"
    res = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    # The sha fetch still works against a real origin; the name never reached argv.
    assert res.state in ("fetched", "unavailable")


def test_no_clone_and_no_timeout_are_skips_not_errors(world, monkeypatch):
    _o, _c, pr = world
    assert head_fetch.ensure_head_in_clone(pr, timeout_s=0).state == "skipped"
    monkeypatch.setattr(head_fetch, "_find_clone", lambda _pr: None)
    res = head_fetch.ensure_head_in_clone(pr, timeout_s=30)
    assert res.state == "skipped" and res.sentence == "skipped: no local clone"


def test_a_busy_clone_times_out_instead_of_waiting_forever(world):
    _o, clone, pr = world
    held = threading.Event()
    release = threading.Event()

    def hold():
        with head_fetch._clone_lock(clone, time.monotonic() + 5):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(5)
        t0 = time.monotonic()
        res = head_fetch.ensure_head_in_clone(pr, timeout_s=1)
        assert time.monotonic() - t0 < 5
        assert not res.available and "busy" in res.reason
    finally:
        release.set()
        t.join()
    assert head_fetch.ensure_head_in_clone(pr, timeout_s=30).available


def test_the_file_lock_excludes_another_holder(world):
    import fcntl

    _o, clone, _pr = world
    with open(clone / ".git" / head_fetch.LOCK_FILE, "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError), head_fetch._clone_lock(clone, time.monotonic() + 0.5):
            pass


def test_old_private_refs_are_pruned(world, monkeypatch):
    _o, clone, pr = world
    monkeypatch.setattr(head_fetch, "KEEP_PRIVATE_REFS", 2)
    base = _git(clone, "rev-parse", "HEAD")
    for i in range(4):
        _git(clone, "update-ref", f"refs/celmis/pr-heads/{100 + i}", base)
    assert head_fetch.ensure_head_in_clone(pr, timeout_s=30).available
    refs = _git(clone, "for-each-ref", "--format=%(refname)", "refs/celmis/pr-heads/").split()
    assert len(refs) == 2


# ── constant check when the head stays unavailable ─────────────────────────


def _fallback_pr(clone: Path, hunks: list[Hunk]) -> PullRequest:
    return PullRequest(provider="github", repo="o/n", number=9, title="t", description="",
                       author="a", base_ref="main", base_sha="b", head_ref="gone",
                       head_sha="f" * 40, state="open", hunks=hunks)


def _hunk(path: str, body: str, new_start: int = 1) -> Hunk:
    n = len(body.splitlines())
    return Hunk(file_path=path, old_file_path=path, old_start=1, old_count=n,
                new_start=new_start, new_count=n, content=f"@@ -1,{n} +1,{n} @@\n{body}")


def test_without_the_head_a_typo_is_found_against_the_clone_head_and_labelled(world):
    _o, clone, _pr = world
    pr = _fallback_pr(clone, [_hunk("Use.php", "+<?php\n+echo Model::STATUS_PENDNG;")])
    found = find_undefined_constants(pr, clone)
    assert [f.title for f in found] == ["Undefined class constant Model::STATUS_PENDNG"]
    assert "head commit was not available locally" in found[0].body


def test_without_the_head_a_constant_added_by_the_diff_is_present(world):
    _o, clone, _pr = world
    pr = _fallback_pr(clone, [
        _hunk("Model.php", "+    const STATUS_NEW = 5;"),
        _hunk("Use.php", "+echo Model::STATUS_NEW;"),
    ])
    assert find_undefined_constants(pr, clone) == []


def test_without_the_head_a_removed_constant_is_absent_only_if_none_remains(world):
    _o, clone, _pr = world
    use = "+echo Model::STATUS_PENDING;"
    gone = _fallback_pr(clone, [_hunk("Model.php", "-    const STATUS_PENDING = 1;"),
                                _hunk("Use.php", use)])
    assert [f.title for f in find_undefined_constants(gone, clone)] == \
        ["Undefined class constant Model::STATUS_PENDING"]
    # Another definition remains in the tree: still defined.
    (clone / "Other.php").write_text("<?php\nclass Other { const STATUS_PENDING = 1; }\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "other")
    assert find_undefined_constants(gone, clone) == []


def test_without_the_head_a_class_the_pr_adds_stays_silent(world):
    _o, clone, _pr = world
    pr = _fallback_pr(clone, [_hunk("New.php", "+<?php\n+class Fresh {}\n+echo Fresh::NOPE;")])
    assert find_undefined_constants(pr, clone) == []
