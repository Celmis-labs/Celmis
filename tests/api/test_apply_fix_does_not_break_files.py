"""Apply-fix committed broken code and reported success.

The structural rule pack ships PROSE as its suggestion — `structural.py` has
six of them: "except Exception:", "logger.exception('...') or raise",
"def f(x=None): x = x or []", "logger.info(...)", "=== / !==",
"for (const x of arr) { await ... }". The reviews page sends that string as
`replacement` (page.tsx: `replacement: f.suggestion || ""`), and apply_fix
replaced the WHOLE line with it, at column 0, with no reindentation.

So pressing the button on any of those six committed syntactically broken code
into the customer's repository, then posted a PR comment saying it worked.

Three defences, in the order they run:
  1. reindent — the cheap one, fixes the column-0 damage for all six at once;
  2. a differential parse gate placed BEFORE branch creation, so a refusal does
     not leave an orphan `celmis-fix/*` branch behind;
  3. a re-run of the one rule that produced the finding, scoped to the lines
     that were rewritten.

The third state is deliberately NOT called "verified". A rule can stop matching
because the fix worked or because the code it matched on was deleted.
"""

from __future__ import annotations

import inspect
from pathlib import Path

# `src.review.structural` and `src.review.agents` import each other, so the
# package has to be loaded first — production reaches structural lazily from
# inside a function and never trips over it.
import src.review.agents  # noqa: F401,E402  (import order is the point)
from src.api.routers.apply_fix import _check_patch, _reindent
from src.review import structural  # noqa: E402

SRC = Path(__file__).resolve().parents[2] / "src"


class _In:
    """The subset of ApplyFixIn that _check_patch reads."""

    def __init__(self, file_path, finding_id, line_start=1, replacement=""):
        self.file_path = file_path
        self.finding_id = finding_id
        self.line_start = line_start
        self.replacement = replacement


# ─── 1. indentation ──────────────────────────────────────────────────


def test_the_replacement_takes_the_indentation_of_the_line_it_replaces():
    """Spliced at column 0 over an indented line, a Python suggestion silently
    terminates the enclosing block."""
    assert _reindent("logger.info(...)", "        print(x)\n") == \
        "        logger.info(...)"


def test_a_top_level_line_is_left_alone():
    assert _reindent("except Exception:", "except:\n") == "except Exception:"


def test_a_multi_line_suggestion_keeps_its_own_shape():
    """Only lines with no indentation of their own get the prefix, so a
    suggestion that is already structured is not double-indented."""
    out = _reindent("if x:\n    return 1", "    pass\n")
    assert out == "    if x:\n    return 1"


def test_an_empty_replacement_is_not_padded():
    """Deleting a line must not leave a line of whitespace behind."""
    assert _reindent("", "    x = 1\n") == ""
    assert _reindent("   \n", "    x = 1\n") == "   \n"


# ─── 2. the parse gate ───────────────────────────────────────────────


def test_prose_that_breaks_python_is_refused():
    """This is the live defect: every one of the six shipped suggestions is
    prose, and this one does not parse."""
    original = "def f():\n    try:\n        go()\n    except:\n        pass\n"
    patched = "def f():\n    try:\n        go()\n    logger.exception('...') or raise\n        pass\n"
    check = _check_patch(_In("a.py", "structural.bare-except"), original, patched)
    assert check.state == "refused_broke_file"
    assert check.reason, "a refusal with no reason is not actionable"


def test_a_file_that_was_already_broken_is_not_blamed_on_us():
    """Differential. Refusing there would block fixes to exactly the files most
    likely to need them."""
    original = "def f(:\n    pass\n"
    patched = "def f(:\n    x = 1\n"
    check = _check_patch(_In("a.py", "structural.nope"), original, patched)
    assert check.state != "refused_broke_file"


def test_an_unknown_file_type_is_applied_unchecked_not_refused():
    """A checker that reports "broken" when it means "unknown" blocks every
    patch to a language we have no grammar for."""
    check = _check_patch(_In("notes.rst", "structural.x"), "hello", "world")
    assert check.state == "applied_unchecked"
    assert check.reason


def test_the_gate_runs_before_the_branch_is_created():
    """A refusal after create_ref leaves an orphan celmis-fix/* branch in the
    customer's repo on every rejection — and on the current rule pack most
    presses are rejections."""
    source = (SRC / "api" / "routers" / "apply_fix.py").read_text()
    gate = source.find("_check_patch(p, content, new_content)")
    create = source.find('f"{api}/git/refs"')
    assert 0 < gate < create, "the gate moved after branch creation"


def test_a_refusal_never_reaches_the_provider():
    source = (SRC / "api" / "routers" / "apply_fix.py").read_text()
    gate = source.find('check.state == "refused_broke_file"')
    put = source.find("put = http.put(")
    assert 0 < gate < put
    assert "status_code=422" in source[gate:gate + 400]


# ─── 3. the rule re-run ──────────────────────────────────────────────


def test_a_patch_that_leaves_the_rule_matching_is_reported():
    """The state that matters: the patch landed and the finding did not go
    away. This used to be reported as plain success."""
    rule = next(r for r in structural.list_rules() if r.language == "python")
    src = "def f():\n    print('x')\n"
    if not structural.rule_still_matches(rule, src, "python",
                                         line_start=1, line_end=5):
        import pytest
        pytest.skip(f"rule {rule.id} does not match the sample")
    check = _check_patch(
        _In("a.py", f"structural.{rule.id}", line_start=1, replacement="x"),
        src, src)
    assert check.state == "still_fires"


def test_the_re_run_is_scoped_to_the_rewritten_lines():
    """A whole-file scan reports matches elsewhere that the review deliberately
    never raised — the review only flags lines the PR touched."""
    body = inspect.getsource(structural.rule_still_matches)
    assert "line_start <= line <= line_end" in body


def test_a_finding_with_no_rule_behind_it_is_unchecked():
    """LLM findings have no deterministic predicate. Saying nothing is honest;
    re-asking a model is not verification."""
    check = _check_patch(_In("a.py", "security.hardcoded-secret"),
                         "x = 1\n", "y = 1\n")
    assert check.state == "applied_unchecked"


def test_nothing_claims_the_word_verified():
    """A rule can stop matching because the fix worked, or because the code it
    matched on was deleted. `def load(items=[])` -> `def f(x=None): x = x or []`
    parses and stops matching, having renamed and emptied the function."""
    import re

    source = (SRC / "api" / "routers" / "apply_fix.py").read_text()
    assert "applied_check_silent" in source
    assert "check_state" in source, "the state never reaches the client"
    # The docstring explains why the word is not used, so strip prose first —
    # what matters is that no STATE or user-facing string claims it.
    code = re.sub(r'"""(?:.|\n)*?"""', "", source)
    code = "\n".join(line for line in code.splitlines()
                     if not line.lstrip().startswith("#"))
    assert "verified" not in code.lower(), (
        "something claims a fix is verified — the check can only say the rule "
        "stopped matching"
    )


# ─── the parse checker itself ────────────────────────────────────────


def test_python_parse_is_exact():
    assert structural.parses("x = 1\n", "python") == (True, "")
    ok, reason = structural.parses("def f(:\n", "python")
    assert not ok and "line" in reason


def test_an_oversized_file_is_not_parsed():
    """Past a couple of megabytes the parse costs more than the patch is
    worth, and it must not become a refusal."""
    ok, _ = structural.parses("x" * (structural.MAX_PARSE_BYTES + 1), "python")
    assert ok


def test_the_language_map_covers_the_shipped_rules():
    assert structural.language_for("a.py") == "python"
    assert structural.language_for("a.tsx") == "tsx"
    assert structural.language_for("README.md") is None


def test_rule_lookup_accepts_the_finding_id_shape():
    """Finding ids are `structural.<rule id>` — see _match_to_finding."""
    rule = structural.list_rules()[0]
    assert structural.rule_by_id(f"structural.{rule.id}") is rule
    assert structural.rule_by_id(rule.id) is rule
    assert structural.rule_by_id("structural.does-not-exist") is None


# ─── the branch name ─────────────────────────────────────────────────


def test_two_findings_of_one_rule_do_not_share_a_branch():
    """A finding id is a RULE id, so two `print-debug` findings in one PR
    collided on one branch and the second commit landed on the first one's
    file."""
    source = (SRC / "api" / "routers" / "apply_fix.py").read_text()
    idx = source.find("slug_src =")
    assert idx > 0, "the slug is still built from finding_id alone"
    line = source[idx:source.find("\n", idx)]
    assert "file_path" in line and "line_start" in line


# ─── the product speaks one language ─────────────────────────────────


def test_no_russian_reaches_a_customer_pull_request():
    """`(см. ...)` shipped in the skip message posted to real PRs. Ukrainian
    is `див.`; the product's UI language is English."""
    for rel in ("review/orchestrator.py", "review/providers/base.py"):
        text = (SRC / rel).read_text()
        assert "см." not in text, f"{rel} still posts Russian"
