"""PyPI coverage must not vanish because one wheel does not build.

pip-audit resolves a requirements file by running `pip install --dry-run`, so a
single requirement with no wheel for the running interpreter — Pillow 10.1.0 on
Python 3.13, verified in production — fails the whole file. The audit then
reported PyPI as "not checked" for that repo and fell back to lock files plus
OSV, which is a real loss: 27 packages / 6 vulnerable were invisible in one of
this workspace's repos.

The fallback audits the exactly-pinned requirements with `--no-deps
--disable-pip`, which never invokes pip. It cannot see transitives, and these
tests exist mostly to pin that the loss is REPORTED rather than passed off as a
clean scan.
"""

from __future__ import annotations

from src.deps.native import _pip_audit_pinned_only


class _Result:
    def __init__(self, ok=True, stdout="", stderr="", error=""):
        self.ok, self.stdout, self.stderr, self.error = ok, stdout, stderr, error


def _runner_returning(payload_json: str, capture: list | None = None):
    def runner(cmd, cwd, timeout):
        if capture is not None:
            capture.append(cmd)
        return _Result(ok=True, stdout=payload_json)
    return runner


EMPTY = '{"dependencies": []}'


def test_only_exactly_pinned_lines_are_forwarded(tmp_path):
    """`--no-deps` refuses the run if ANY line is unpinned — one
    `setuptools>=80.9.0,<81` blanked a 35-line file in production."""
    (tmp_path / "requirements.txt").write_text(
        "Django==4.2.25\n"
        "setuptools>=80.9.0,<81\n"
        "requests==2.32.5\n"
        "# a comment\n"
        "\n"
        "-r other.txt\n",
        encoding="utf-8")
    written: list[str] = []

    def runner(cmd, cwd, timeout):
        # The last argument is the temp file the fallback wrote.
        with open(cmd[-1], encoding="utf-8") as fh:
            written.append(fh.read())
        return _Result(ok=True, stdout=EMPTY)

    payload, note = _pip_audit_pinned_only(
        tmp_path, ["-r", "requirements.txt"], runner, 60, "boom")
    assert payload == {"dependencies": []}
    body = written[0]
    assert "Django==4.2.25" in body
    assert "requests==2.32.5" in body
    assert "setuptools" not in body, "an unpinned line would abort the whole run"
    assert "-r other.txt" not in body


def test_the_degradation_is_stated_with_numbers(tmp_path):
    """"partial" with no detail is not usable. Which error, how many audited,
    how many lines dropped."""
    (tmp_path / "requirements.txt").write_text(
        "Django==4.2.25\nrequests==2.32.5\nsetuptools>=80\n", encoding="utf-8")
    _, note = _pip_audit_pinned_only(
        tmp_path, ["-r", "requirements.txt"],
        _runner_returning(EMPTY), 60, "ERROR: Failed to build 'Pillow'")
    assert "ERROR: Failed to build 'Pillow'" in note
    assert "audited 2 pinned requirements" in note
    assert "without the transitive tree" in note
    assert "1 unpinned line(s) skipped" in note


def test_no_skipped_lines_means_no_misleading_suffix(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "Django==4.2.25\n", encoding="utf-8")
    _, note = _pip_audit_pinned_only(
        tmp_path, ["-r", "requirements.txt"],
        _runner_returning(EMPTY), 60, "boom")
    assert "unpinned line" not in note


def test_the_fallback_runs_without_pip(tmp_path):
    """The whole point. With pip in the loop the same wheel fails again."""
    (tmp_path / "requirements.txt").write_text("Django==4.2.25\n", encoding="utf-8")
    seen: list[list[str]] = []
    _pip_audit_pinned_only(tmp_path, ["-r", "requirements.txt"],
                           _runner_returning(EMPTY, seen), 60, "boom")
    cmd = seen[0]
    assert "--no-deps" in cmd and "--disable-pip" in cmd
    assert "-s" in cmd and "osv" in cmd, "PyPI's own feed under-reports"


def test_extras_and_markers_are_normalised(tmp_path):
    """`uvicorn[standard]==0.34.0 ; python_version>='3.9'` is a pinned
    requirement — the advisory lookup keys on name and version only."""
    (tmp_path / "requirements.txt").write_text(
        'uvicorn[standard]==0.34.0 ; python_version>="3.9"\n', encoding="utf-8")
    written: list[str] = []

    def runner(cmd, cwd, timeout):
        with open(cmd[-1], encoding="utf-8") as fh:
            written.append(fh.read())
        return _Result(ok=True, stdout=EMPTY)

    _pip_audit_pinned_only(tmp_path, ["-r", "requirements.txt"], runner, 60, "boom")
    assert written[0].strip() == "uvicorn==0.34.0"


def test_a_project_build_target_has_nothing_to_pin(tmp_path):
    """`pip-audit .` builds the project — there is no requirements file to
    reduce, and pretending otherwise would audit the wrong thing."""
    payload, note = _pip_audit_pinned_only(
        tmp_path, ["."], _runner_returning(EMPTY), 60, "boom")
    assert payload is None and note == ""


def test_a_file_with_nothing_pinned_gives_up_cleanly(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "django>=4\nrequests~=2.0\n", encoding="utf-8")
    payload, note = _pip_audit_pinned_only(
        tmp_path, ["-r", "requirements.txt"], _runner_returning(EMPTY), 60, "boom")
    assert payload is None and note == ""


def test_a_failing_fallback_reports_nothing_rather_than_a_clean_bill(tmp_path):
    """The dangerous outcome is an empty result that reads as 'no
    vulnerabilities'."""
    (tmp_path / "requirements.txt").write_text("Django==4.2.25\n", encoding="utf-8")

    def failing(cmd, cwd, timeout):
        return _Result(ok=False, error="pip-audit not installed")

    payload, note = _pip_audit_pinned_only(
        tmp_path, ["-r", "requirements.txt"], failing, 60, "boom")
    assert payload is None and note == ""


def test_a_partial_check_stays_in_the_not_checked_list():
    """`summary.not_checked` is built as `status != "checked"`. A partial scan
    that claimed "checked" would erase its own caveat from the panel."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2]
              / "src" / "deps" / "auditor.py").read_text()
    assert 'c.get("status") != "checked"' in source, (
        "the not-checked filter changed — re-check that 'partial' still lands "
        "in the coverage panel"
    )
