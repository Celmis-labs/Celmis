"""The defect agent reads the code around the diff, not the diff alone.

A hunk with three lines of context cannot show that a variable is declared
above it, that a caller passes a JSON string, or that a comparison was
inverted relative to the rest of the function. These tests pin the context the
agent is now given (src/review/code_context.py) and that every piece of it
fails open.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from src.review import code_context as cc
from src.review.agents.base import AgentContext
from src.review.agents.defect import _BEHAVIOUR_LENS, DefectAgent
from src.review.models import Hunk, PullRequest

PHP = '''<?php
class Cart {
    private $sum = 0;

    public function getTotal() {
        return max(1, $this->sum); // a "}" in a comment
    }

    public function resize($items) {
        $msg = "closing brace } and opening { in a string";
        foreach ($items as $it) {
            $label = '}';
            if ($it !== null) {
                $this->sum = $it;
            }
        }
        /* } stray brace in a block comment { */
        return $this->getTotal();
    }
}

function helper() {
    return 1;
}
'''

JS = '''function outer(list) {
  var total = 0;
  var s = "}{";
  list.forEach(function (x) {
    // } not a real brace
    total += x.size;
  });
  return `${total}}`;
}

var api = {
  load: function (opts) {
    return opts.url;
  },
};
'''


def _hunk(path, new_start, body, *, new_count=None, **kw):
    lines = body.split("\n")
    n_new = new_count if new_count is not None else sum(
        1 for ln in lines if not ln.startswith("-"))
    content = f"@@ -{new_start},{len(lines)} +{new_start},{n_new} @@\n{body}\n"
    return Hunk(
        file_path=path, old_file_path=path, old_start=new_start,
        old_count=len(lines), new_start=new_start, new_count=n_new,
        content=content, **kw,
    )


def _pr(hunks=(), head="abc1234"):
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="b" * 7, head_ref="f",
        head_sha=head, state="open", hunks=list(hunks),
    )


# ─── line-numbered rendering ─────────────────────────────────────────


def test_hunks_carry_new_side_line_numbers_and_removed_lines_none():
    h = _hunk("a.js", 10, " keep\n-old line\n+new line\n tail")
    nh = cc.number_hunk(h)
    rows = nh.text.split("\n")
    assert rows[1].strip() == "10  keep"
    assert rows[2] == f"{'':>6} -old line"           # no number on a deletion
    assert rows[3] == "    11 +new line"
    assert rows[4] == "    12  tail"
    assert nh.added == {11}
    assert (nh.first, nh.last) == (11, 11)


def test_a_pure_deletion_is_anchored_where_it_was_removed():
    nh = cc.number_hunk(_hunk("a.js", 5, " a\n-gone\n b", new_count=2))
    assert nh.added == set()
    assert nh.first == nh.last == 6


def test_the_numbered_diff_is_capped_with_a_notice():
    hunks = [_hunk("a.js", 1, "+" + "x" * 300) for _ in range(20)]
    out = cc.render_numbered_diff(hunks, max_chars=1000)
    assert "truncated" in out and len(out) < 1500


# ─── enclosing function: tree-sitter and the scanner ─────────────────


@pytest.mark.parametrize("engine", ["treesitter", "scanner"])
def test_php_enclosing_method_survives_braces_in_strings_and_comments(engine):
    spans = (
        cc._treesitter_functions(PHP, "x.php") if engine == "treesitter"
        else cc._scan_braces(PHP, "x.php")
    )
    assert spans, "no function found"
    line = next(i for i, t in enumerate(PHP.split("\n"), 1) if "$this->sum = $it" in t)
    fn = cc.enclosing_function(spans, line, line)
    assert fn is not None and fn.name == "resize"
    assert PHP.split("\n")[fn.start - 1].strip().startswith("public function resize")
    assert PHP.split("\n")[fn.end - 1].strip() == "}"
    assert fn.end > line + 3          # reaches past the string/comment braces
    helper = next(s for s in spans if s.name == "helper")
    assert helper.start > fn.end


@pytest.mark.parametrize("engine", ["treesitter", "scanner"])
def test_js_enclosing_function_outermost_and_named_property(engine):
    spans = (
        cc._treesitter_functions(JS, "x.js") if engine == "treesitter"
        else cc._scan_braces(JS, "x.js")
    )
    lines = JS.split("\n")
    inner = next(i for i, t in enumerate(lines, 1) if "total += x.size" in t)
    fn = cc.enclosing_function(spans, inner, inner)
    # the callback is inside `outer`; the outermost fitting function wins, so
    # the declaration of `total` above the callback is in view
    assert fn.name == "outer"
    assert fn.start == 1 and fn.end == 9
    load = next(i for i, t in enumerate(lines, 1) if "return opts.url" in t)
    assert cc.enclosing_function(spans, load, load).name == "load"


def test_python_falls_back_to_the_indent_scanner(monkeypatch):
    monkeypatch.setattr(cc, "_treesitter_functions", lambda *a, **k: None)
    src = "def a():\n    x = 1\n\n    return x\n\ndef b():\n    return 2\n"
    spans = cc.list_functions(src, "m.py")
    assert [(s.name, s.start, s.end) for s in spans] == [("a", 1, 4), ("b", 6, 7)]


def test_no_function_means_none_and_unknown_extension_is_empty():
    assert cc.enclosing_function([], 3, 3) is None
    assert cc.list_functions("anything", "notes.txt") == []


# ─── building the context against a real git repo ────────────────────


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "vendor").mkdir()
    (root / "src" / "cart.php").write_text(PHP)
    (root / "src" / "use.php").write_text(
        "<?php\n$b = new Cart();\n$b->resize(json_encode($rows));\n$x = 1;\n")
    (root / "vendor" / "lib.php").write_text("<?php\n$b->resize($vendored);\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
        check=True).stdout.strip()
    return root, head


def _resize_hunk():
    n = next(i for i, t in enumerate(PHP.split("\n"), 1) if "$this->sum = $it" in t)
    return _hunk("src/cart.php", n - 1, "-            if ($it === null) {\n+            if ($it !== null) {\n"
                 "                 $this->sum = $it;", new_count=3)


def test_context_has_enclosing_function_callers_and_callees(repo):
    root, head = repo
    pr = _pr([_resize_hunk()], head=head)
    src = cc.CodeSource(pr, root, None)
    out = cc.build_code_context(pr, pr.hunks, source=src)
    text = out.render()
    assert "`resize`" in out.enclosing
    assert "foreach ($items as $it)" in out.enclosing       # lines above the hunk
    assert "*" in out.enclosing                              # changed lines marked
    # call site from another file, with context lines; vendor skipped
    assert "src/use.php:3" in out.callsites
    assert "json_encode($rows)" in out.callsites
    assert "vendor/lib.php" not in text
    # the callee invoked on a changed line is not here (getTotal is called on
    # an unchanged line), so nothing is invented
    assert "callee `getTotal`" not in text
    assert out.symbols == ["resize"]


def test_callee_invoked_on_a_changed_line_is_included(repo):
    root, head = repo
    n = next(i for i, t in enumerate(PHP.split("\n"), 1) if "return $this->getTotal()" in t)
    h = _hunk("src/cart.php", n, "-        return $this->sum;\n+        return $this->getTotal();", new_count=1)
    pr = _pr([h], head=head)
    out = cc.build_code_context(pr, pr.hunks, source=cc.CodeSource(pr, root, None))
    assert "callee `getTotal`" in out.callees
    assert "max(1, $this->sum)" in out.callees


def test_budgets_are_respected(repo):
    root, head = repo
    pr = _pr([_resize_hunk()], head=head)
    src = cc.CodeSource(pr, root, None)
    tiny = cc.build_code_context(pr, pr.hunks, source=src, max_chars=200)
    assert len(tiny.render()) <= 200             # a hard ceiling
    nocalls = cc.build_code_context(pr, pr.hunks, source=src, max_callsites=0)
    assert nocalls.callsites == ""
    off = cc.build_code_context(pr, pr.hunks, source=src, max_chars=0)
    assert off.render() == ""


def test_a_commit_the_clone_lacks_falls_back_to_the_provider(repo):
    root, _ = repo
    calls = []

    class Provider:
        def read_file_at(self, repo_name, ref, path):
            calls.append((repo_name, ref, path))
            return PHP

    pr = _pr([_resize_hunk()], head="f" * 40)
    out = cc.build_code_context(pr, pr.hunks, source=cc.CodeSource(pr, root, Provider()))
    assert calls == [("o/r", "f" * 40, "src/cart.php")]
    assert "`resize`" in out.enclosing
    assert out.callsites == ""      # no grep without the commit: dropped, not faked


def test_no_clone_and_no_provider_fails_open():
    pr = _pr([_resize_hunk()])
    out = cc.build_code_context(pr, pr.hunks, source=cc.CodeSource(pr, None, None))
    assert out.render() == ""


def test_a_raising_provider_and_a_minified_file_fail_open():
    class Boom:
        def read_file_at(self, *a):
            raise RuntimeError("rate limited")

    pr = _pr([_resize_hunk()])
    assert cc.CodeSource(pr, None, Boom()).read("src/cart.php") is None

    class Minified:
        def read_file_at(self, *a):
            return "x" * 5000

    assert cc.CodeSource(pr, None, Minified()).read("a.js") is None


def test_a_crash_inside_the_build_returns_an_empty_context(monkeypatch):
    monkeypatch.setattr(cc, "_build", lambda *a, **k: 1 / 0)
    pr = _pr([_resize_hunk()])
    assert cc.build_code_context(pr, pr.hunks).render() == ""


# ─── the prompt ──────────────────────────────────────────────────────


def test_the_prompt_has_numbered_diff_context_graph_and_the_lens(repo, monkeypatch):
    root, head = repo
    pr = _pr([_resize_hunk()], head=head)
    monkeypatch.setattr(cc, "make_source", lambda p, provider=None: cc.CodeSource(p, root, None))
    prompt = DefectAgent(passes=1)._build_prompt(AgentContext(
        pull_request=pr, graph_summary="resize has 1 caller"))
    assert "## Code the diff sits in" in prompt
    assert "src/use.php:3" in prompt
    assert "resize has 1 caller" in prompt
    assert "    " in prompt and "+            if ($it !== null)" in prompt
    system = " ".join(DefectAgent.system_prompt.split())
    assert " ".join(_BEHAVIOUR_LENS.split()) in system
    for needle in ("POLARITY", "REMOVED SAFETY NET", "RETURN AND STATUS",
                   "CALLER/CALLEE SHAPE", "CALLEE SEMANTICS", "COPY-PASTE"):
        assert needle in system
    assert "absent from" in system and "undefined variable" in system.lower()
    assert "provable inside the file" not in system


def test_the_second_pass_asks_the_lens_question():
    from src.review.agents.defect import _SECOND_PASS

    flat = " ".join(_SECOND_PASS.split())
    assert "THE CHANGE LENS" in flat
    assert "pair each `-` line" in flat


def test_switched_off_the_prompt_is_the_plain_diff(monkeypatch):
    from src.review.settings import get_review_settings

    s = get_review_settings()
    monkeypatch.setattr(s, "code_context_enabled", False)
    pr = _pr([_resize_hunk()])
    prompt = DefectAgent(passes=1)._build_prompt(AgentContext(pull_request=pr))
    assert "## Code the diff sits in" not in prompt
    assert "```diff" in prompt


def test_the_context_is_built_once_for_both_passes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.review.agents.defect.build_code_context",
        lambda *a, **k: calls.append(1) or SimpleNamespace(render=lambda: "CTX"))
    agent = DefectAgent(passes=2)
    pr = _pr([_resize_hunk()])
    ctx = AgentContext(pull_request=pr)
    agent._build_prompt(ctx)
    agent._build_prompt(ctx)
    assert len(calls) == 1
