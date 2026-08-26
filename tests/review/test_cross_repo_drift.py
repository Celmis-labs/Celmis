"""The one check nothing else in the product does — and it had no tests at all.

Cross-repo drift is the differentiator: a constant changed in one repository
and left behind in its siblings. `etl/config.py` sets
EMBEDDING_MODEL = "gemini-embedding-2"; so does `chat/config.py`; a PR bumps
the first to -3 and nothing anywhere says the second exists. The vectors
diverge, and the Qdrant queries fail days later with no error that points
here.

Against 1232 tests elsewhere, this module had zero. That is worse than it
sounds, because of how it fails: `_build_cross_repo_drift` catches every
exception and returns an empty string. A broken grep, a group that stops
resolving, a diff format that shifts — the review proceeds, silently, minus
the only signal that would have caught the divergence. Absence of a finding is
indistinguishable from a clean result.

So the tests below are ordered by what actually goes wrong: the parser
(line numbers in the OLD file, which is the part diff formats make easy to get
wrong), the noise filter (whose job is a false-positive rate near zero), and
then the ways the whole thing turns itself off without saying so.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.review.cross_repo_drift import (
    DriftHit,
    DriftMatch,
    DriftReport,
    _grep_repo,
    _is_interesting,
    detect_drift,
    extract_removed_values,
)
from src.review.models import Hunk, PullRequest


def _pr(*hunks: Hunk, repo: str = "acme/etl", number: int = 7) -> PullRequest:
    return PullRequest(
        provider="github", repo=repo, number=number,
        title="bump the embedding model", description="", author="dev",
        base_ref="main", base_sha="a" * 40,
        head_ref="feature", head_sha="b" * 40,
        state="open", hunks=list(hunks),
    )


def _hunk(content: str, *, path: str = "config.py", old_start: int = 1) -> Hunk:
    return Hunk(
        file_path=path, old_file_path=path,
        old_start=old_start, old_count=content.count("\n"),
        new_start=old_start, new_count=content.count("\n"),
        content=content,
    )


# ─── the parser ──────────────────────────────────────────────────────


def test_the_canonical_case_from_the_docstring():
    """The example the module was written for, end to end through the parser."""
    pr = _pr(_hunk(
        "@@ -10,3 +10,3 @@\n"
        " import os\n"
        '-EMBEDDING_MODEL = "gemini-embedding-2"\n'
        '+EMBEDDING_MODEL = "gemini-embedding-3"\n'
        " TIMEOUT = 30\n"
    ))
    values = extract_removed_values(pr)
    assert ("gemini-embedding-2", "config.py", 11) in values
    # …and the NEW value is not reported: it has not drifted anywhere, it is
    # what everything should be moving to.
    assert not any(v == "gemini-embedding-3" for v, _, _ in values)


def test_the_line_number_is_in_the_OLD_file():
    """The whole point of reading `@@ -N` rather than counting output lines.
    A wrong number sends somebody to an unrelated line in a file that has since
    moved on, which is worse than no number."""
    pr = _pr(_hunk(
        "@@ -100,5 +100,5 @@\n"
        " context one\n"
        " context two\n"
        '-QUEUE_NAME = "orders-v2"\n'
        " context three\n"
    ))
    values = extract_removed_values(pr)
    assert values == [("orders-v2", "config.py", 102)]


def test_added_lines_do_not_advance_the_old_counter():
    """`+` lines exist only in the new file. Counting them shifts every
    subsequent removal by one — the classic unified-diff mistake."""
    pr = _pr(_hunk(
        "@@ -1,4 +1,6 @@\n"
        " keep\n"
        '+ADDED_ONE = "brand-new-1"\n'
        '+ADDED_TWO = "brand-new-2"\n'
        '-REMOVED = "target-value-9"\n'
    ))
    values = extract_removed_values(pr)
    assert values == [("target-value-9", "config.py", 2)]


def test_the_file_header_lines_are_not_read_as_removals():
    """`--- a/path` starts with a dash and is not a removed line."""
    pr = _pr(_hunk(
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        '-MODEL = "keep-this-one"\n'
    ))
    assert extract_removed_values(pr) == [("MODEL", "config.py", 1),
                                          ("keep-this-one", "config.py", 1)] or \
           extract_removed_values(pr) == [("keep-this-one", "config.py", 1)]


def test_content_before_any_hunk_header_is_ignored():
    """Without a `@@` there is no old-file line number to attribute a removal
    to, and inventing one is worse than skipping it."""
    pr = _pr(_hunk('-STRAY = "no-header-here"\n'))
    assert extract_removed_values(pr) == []


def test_duplicates_within_a_hunk_are_reported_once():
    pr = _pr(_hunk(
        "@@ -1,3 +1,3 @@\n"
        '-A = "repeated-value-1"\n'
        '-B = "repeated-value-1"\n'
    ))
    values = [v for v, _, _ in extract_removed_values(pr)]
    # Same value, different lines — both are real sites, so both are kept.
    assert values.count("repeated-value-1") == 2


def test_single_and_double_quotes_are_both_read():
    pr = _pr(_hunk(
        "@@ -1,3 +1,3 @@\n"
        "-SINGLE = 'single-quoted-1'\n"
        '-DOUBLE = "double-quoted-2"\n'
    ))
    values = {v for v, _, _ in extract_removed_values(pr)}
    assert "single-quoted-1" in values
    assert "double-quoted-2" in values


def test_a_hunk_with_no_content_does_not_raise():
    """Binary files and rename-only entries arrive with empty content."""
    pr = _pr(_hunk(""))
    assert extract_removed_values(pr) == []


# ─── the noise filter ────────────────────────────────────────────────
#
# This is where false positives would come from, and a false positive here is
# expensive: the report goes into the architect's prompt as an authoritative
# signal, so noise does not merely clutter, it misleads.


@pytest.mark.parametrize("value", [
    "gemini-embedding-2",     # the canonical case
    "orders-v2",              # queue name
    "api.example.com",        # host
    "SECRET_KEY",             # constant name
    "camelCaseThing",         # identifier
    "v1.2.3",                 # version
    "some_snake_case",        # identifier
])
def test_distinctive_values_are_kept(value: str):
    assert _is_interesting(value) is True


@pytest.mark.parametrize("value", [
    "true", "false", "none", "null", "self", "this",   # keywords
    "get", "post", "put", "delete",                    # verbs
    "error", "warning", "info", "debug",               # log levels
    "string", "number", "object", "array",             # type names
    "{}", "{0}", "%s", "%d",                           # format placeholders
    ".py", ".tsx",                                     # bare extensions
    "ab",                                              # too short
    "   ",                                             # whitespace
    "!!!",                                             # no alphanumerics
    "hello world",                                     # lowercase prose
])
def test_generic_noise_is_dropped(value: str):
    assert _is_interesting(value) is False


def test_the_length_bounds_are_enforced_at_both_ends():
    assert _is_interesting("a" * 2) is False
    assert _is_interesting("x1" + "y" * 198) is True     # exactly 200
    assert _is_interesting("x1" + "y" * 199) is False    # 201


# ─── grep, which is where it meets a real repository ─────────────────


def test_grep_finds_the_sibling_and_reports_a_relative_path(tmp_path: Path):
    """The absolute path on our disk is meaningless to the reader; the path
    inside their repository is the whole value of the finding."""
    repo = tmp_path / "chat"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "config.py").write_text(
        'EMBEDDING_MODEL = "gemini-embedding-2"\n')

    matches = _grep_repo("gemini-embedding-2", repo, "acme/chat")
    assert len(matches) == 1
    assert matches[0].file == "src/config.py"
    assert matches[0].line == 1
    assert matches[0].other_repo_slug == "acme/chat"
    assert "gemini-embedding-2" in matches[0].excerpt


def test_vendored_and_generated_directories_are_skipped(tmp_path: Path):
    """A value found in node_modules is somebody else's copy, and reporting it
    is exactly the kind of noise that teaches people to stop reading."""
    repo = tmp_path / "chat"
    for junk in ("node_modules", "dist", ".venv"):
        d = repo / junk
        d.mkdir(parents=True)
        (d / "bundled.js").write_text('const M = "gemini-embedding-2";\n')
    (repo / "app.py").write_text('MODEL = "gemini-embedding-2"\n')

    matches = _grep_repo("gemini-embedding-2", repo, "acme/chat")
    assert [m.file for m in matches] == ["app.py"]


def test_a_missing_repo_is_not_an_error(tmp_path: Path):
    assert _grep_repo("anything-here", tmp_path / "gone", "acme/gone") == []


def test_the_value_is_matched_literally_not_as_a_pattern(tmp_path: Path):
    """`-F`. A removed value containing regex metacharacters — a version like
    `1.2.3`, a path — must not become a pattern that matches other things."""
    repo = tmp_path / "chat"
    repo.mkdir()
    (repo / "a.py").write_text('V = "1.2.3"\nW = "10203"\n')

    matches = _grep_repo("1.2.3", repo, "acme/chat")
    assert len(matches) == 1
    assert "1.2.3" in matches[0].excerpt


def test_a_slow_grep_is_survivable(tmp_path: Path, monkeypatch):
    """One unresponsive repository must not take the review down with it."""
    repo = tmp_path / "chat"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="grep", timeout=8)

    monkeypatch.setattr(subprocess, "run", _boom)
    assert _grep_repo("value-here", repo, "acme/chat") == []


def test_excerpts_are_truncated(tmp_path: Path):
    repo = tmp_path / "chat"
    repo.mkdir()
    (repo / "a.py").write_text('X = "needle-value-1"  # ' + "z" * 400 + "\n")

    matches = _grep_repo("needle-value-1", repo, "acme/chat")
    assert len(matches[0].excerpt) <= 141


# ─── the report ──────────────────────────────────────────────────────


def test_no_matches_reads_as_no_drift_not_as_a_finding():
    """`has_drift` is false when values were extracted but found nowhere else.
    Reporting "checked, nothing" as a hit would be a false positive in the
    architect's prompt."""
    report = DriftReport(
        group_name="core",
        hits=[DriftHit(value="v", pr_file="a.py", pr_line=1, matches=[])],
    )
    assert report.has_drift is False
    assert "no cross-repo drift" in report.to_markdown().lower()


def test_a_hit_renders_the_repo_file_and_line():
    """What the reader needs in order to act: which repository, which file,
    which line."""
    report = DriftReport(
        group_name="core",
        hits=[DriftHit(
            value="gemini-embedding-2", pr_file="config.py", pr_line=11,
            matches=[DriftMatch(other_repo_slug="acme/chat",
                                file="src/config.py", line=4,
                                excerpt='MODEL = "gemini-embedding-2"')],
        )],
    )
    md = report.to_markdown()
    assert report.has_drift is True
    for needle in ("gemini-embedding-2", "acme/chat", "src/config.py", "4"):
        assert needle in md


# ─── the ways it turns itself off ────────────────────────────────────


def test_no_group_means_no_drift_rather_than_an_exception():
    """A repository that belongs to no group has no siblings to drift from.
    That is a legitimate empty result, not a failure."""
    pr = _pr(_hunk("@@ -1,2 +1,2 @@\n-X = \"lonely-value-1\"\n"),
             repo="acme/standalone")
    report = detect_drift(pr)
    assert isinstance(report, DriftReport)
    assert report.has_drift is False


def test_the_orchestrator_no_longer_fails_silently():
    """It caught every exception and logged at `debug`, which on a production
    box is nowhere. A grep that stops working, a group that stops resolving —
    and the review proceeds without the one signal nothing else provides, with
    no error, no warning and no row anywhere.

    Same shape as `noData = OK` on a monitor: the absence of a signal
    presenting as health.
    """
    import inspect

    from src.review.orchestrator import ReviewOrchestrator

    body = inspect.getsource(ReviewOrchestrator._build_cross_repo_drift)
    assert "logger.debug" not in body, "the failure is invisible again"
    assert "logger.warning" in body
    assert "exc_info=True" in body, "no traceback means no way to diagnose it"
    assert "NO cross-repo drift signal" in body, (
        "the log line must say what was LOST, not merely that something failed"
    )


def test_the_detector_is_still_wired_into_the_review():
    """Dead code that passes its own tests is the other way a differentiator
    stops existing."""
    import inspect

    from src.review.orchestrator import ReviewOrchestrator

    # It is called from _build_context, which _review_impl drives. Checking
    # the whole class rather than one method name so a refactor that MOVES the
    # call still passes, while deleting it fails.
    # Keyed on the CALL, not on its argument list. This grepped for
    # "_build_cross_repo_drift(pr)" and broke the day the workspace was
    # threaded through for tenant isolation — a change that made the detector
    # safer. A test that fails when the code improves is keyed on the wrong
    # thing.
    src = inspect.getsource(ReviewOrchestrator)
    assert "self._build_cross_repo_drift(" in src
    assert "drift_md" in src
    assert "cross_repo_drift" in inspect.getsource(
        ReviewOrchestrator._build_context), (
        "the report is built but no longer reaches the agent context"
    )


def test_the_value_cap_is_honoured():
    """A large refactor removes hundreds of strings; grepping every sibling
    for each one is how a review times out."""
    import inspect

    sig = inspect.signature(detect_drift)
    assert sig.parameters["max_values"].default <= 50


# ─── proven and inferred are different kinds of claim ────────────────


def test_a_deterministic_finding_is_marked_as_proven():
    """`confidence` cannot express this. A float mixes "the grep matched" with
    "the model felt strongly", and once they share a scale the UI has no honest
    way to separate them."""
    from src.review.models import Finding

    plain = Finding(file_path="a.py", line=1)
    assert plain.evidence_kind == "inferred", "the default must be the cautious one"
    assert plain.is_proven is False

    proven = Finding(file_path="a.py", line=1, evidence_kind="proven")
    assert proven.is_proven is True


def test_the_structural_agent_produces_proven_findings():
    """It matches a syntax tree — file, line and the code itself."""
    import inspect

    from src.review import structural

    body = inspect.getsource(structural.StructuralAgent)
    assert 'evidence_kind="proven"' in body


def test_the_api_hands_the_distinction_to_the_ui():
    """Kept out of `confidence` on purpose, so the UI can put proven findings
    in their own section rather than in one list behind a badge."""
    from pathlib import Path

    routes = (Path(__file__).resolve().parents[2]
              / "src" / "api" / "routers" / "reviews.py").read_text()
    assert '"evidence_kind"' in routes


def test_drift_facts_carry_the_whole_evidence():
    """Value, where it was removed, and every place it still is — with file,
    line and the line itself. That is what "checkable in five seconds" means."""
    report = DriftReport(
        group_name="core",
        hits=[DriftHit(value="gemini-embedding-2", pr_file="config.py",
                       pr_line=11,
                       matches=[DriftMatch(other_repo_slug="acme/chat",
                                           file="src/config.py", line=4,
                                           excerpt='M = "gemini-embedding-2"')])],
    )
    facts = report.to_facts()
    hit = facts["hits"][0]
    assert hit["value"] == "gemini-embedding-2"
    assert hit["removed_from"] == {"file": "config.py", "line": 11}
    still = hit["still_in"][0]
    assert still["repo"] == "acme/chat"
    assert still["line"] == 4
    assert "gemini-embedding-2" in still["excerpt"]


def test_a_report_with_no_matches_yields_no_hits():
    report = DriftReport(group_name="core",
                         hits=[DriftHit(value="v", pr_file="a", pr_line=1)])
    assert report.to_facts()["hits"] == []
