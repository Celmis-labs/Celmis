"""`structural.undefined-class-constant`: a PHP constant reference that no
definition in the repository at the head commit backs.

Every case builds a real throw-away git repository, because the check is a
text search over a commit and a mock of `git grep` would test the mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.review.agents.base import AgentContext
from src.review.models import Hunk, PullRequest
from src.review.structural import StructuralAgent
from src.review.structural_constants import RULE_ID, find_undefined_constants

MODEL = """<?php
class Model {
    const STATUS_PENDING = 1;
    const STATUS_DONE = 2, STATUS_OLD = 3;
    public const string KIND = 'a';
}
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str], new_path: str, new_text: str) -> tuple[Path, PullRequest]:
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q")
    for name, text in {**files, new_path: new_text}.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    sha = _git(root, "rev-parse", "HEAD")
    lines = new_text.splitlines()
    content = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + ln for ln in lines)
    h = Hunk(file_path=new_path, old_file_path=new_path, old_start=0, old_count=0,
             new_start=1, new_count=len(lines), content=content)
    pr = PullRequest(
        provider="github", repo="x/y", number=1, title="t", description="", author="me",
        base_ref="main", base_sha="x", head_ref="b", head_sha=sha, state="open", hunks=[h],
    )
    return root, pr


def _run(tmp_path, snippet, extra=None, path="app/Use.php"):
    root, pr = _repo(tmp_path, {"app/Model.php": MODEL, **(extra or {})}, path, snippet)
    return find_undefined_constants(pr, root)


def test_misspelled_constant_is_a_proven_error_with_the_closest_name(tmp_path):
    out = _run(tmp_path, "<?php\n$m->status = Model::STATUS_PENDNG;\n")
    assert len(out) == 1
    f = out[0]
    assert f.rule_id == RULE_ID and f.evidence_kind == "proven"
    assert f.severity.value == "error" and f.line == 2
    assert "STATUS_PENDING" in f.body and f.suggestion == "Use `STATUS_PENDING`."


def test_defined_constants_are_silent(tmp_path):
    src = "<?php\n$a = Model::STATUS_PENDING; $b = Model::STATUS_OLD; $c = Model::KIND;\n"
    assert _run(tmp_path, src) == []


def test_define_enum_case_and_vendor_definitions_count(tmp_path):
    extra = {
        "vendor/lib/Lib.php": "<?php\nclass Lib { const FROM_VENDOR = 1; }\n",
        "app/E.php": "<?php\nenum E: int { case ACTIVE = 1; }\n",
        "app/c.php": "<?php\ndefine('LEGACY_FLAG', 1);\n",
    }
    src = "<?php\n$a = Lib::FROM_VENDOR; $b = E::ACTIVE; $c = Model::LEGACY_FLAG;\n"
    assert _run(tmp_path, src, extra) == []


def test_self_and_static_resolve_to_the_enclosing_class(tmp_path):
    src = "<?php\nclass Child {\n  function f() { return self::NOPE_ONE + static::NOPE_TWO; }\n}\n"
    out = _run(tmp_path, src)
    assert {f.title for f in out} == {
        "Undefined class constant self::NOPE_ONE", "Undefined class constant static::NOPE_TWO"}


def test_a_class_outside_the_repo_is_not_judged(tmp_path):
    assert _run(tmp_path, "<?php\n$a = PDO::FETCH_ASSOC; $b = Lib::WHO_KNOWS;\n") == []


def test_inheriting_from_an_unseen_class_is_not_judged(tmp_path):
    src = "<?php\nclass Child extends UnseenBase {\n  function f() { return self::NOPE; }\n}\n"
    assert _run(tmp_path, src) == []


def test_not_references(tmp_path):
    src = (
        "<?php\n$a = Model::class; $b = Model::make(); $c = Model::$count;\n"
        "$d = 'Model::NOPE'; // Model::NOPE_TOO\n/* Model::NOPE_3 */\n$e = Model::Camel;\n"
    )
    assert _run(tmp_path, src) == []


def test_only_added_php_lines_are_checked(tmp_path):
    assert _run(tmp_path, "const X = Model::GONE;\n", path="web/app.js") == []


def test_no_clone_is_silent_and_an_unknown_head_falls_back_labelled(tmp_path):
    root, pr = _repo(tmp_path, {"app/Model.php": MODEL}, "u.php", "<?php\n$a = Model::NOPE;\n")
    assert find_undefined_constants(pr, None) == []
    pr.head_sha = "deadbeef" * 5
    # An unknown head is no longer silence: the clone's HEAD plus the diff is
    # searched and the finding is labelled (see test_the_pr_head_is_fetched_*).
    out = find_undefined_constants(pr, root)
    assert len(out) == 1 and "not available locally" in out[0].body


def test_a_far_name_gets_no_suggestion(tmp_path):
    out = _run(tmp_path, "<?php\n$a = Model::SOMETHING_ELSE;\n")
    assert len(out) == 1 and out[0].suggestion is None


def test_structural_agent_reports_it_through_its_own_run(tmp_path):
    root, pr = _repo(tmp_path, {"app/Model.php": MODEL}, "u.php", "<?php\n$a = Model::STATUS_PENDNG;\n")
    res = StructuralAgent(clone_root=lambda _pr: root).review(AgentContext(pull_request=pr))
    assert [f.rule_id for f in res.findings] == [RULE_ID]
