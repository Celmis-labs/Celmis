"""The command log does not read as flaky tests when the tests passed.

MEASURED ON A REAL SESSION. The agent ran three commands. The exec sandbox
unpacks the repository at a different root than the file tools use, so
`cd /workspace/…/repo && python -m pytest` failed; plain `python -m pytest`
then failed because the image ships no pytest; `pip install pytest -q &&
python -m pytest tests/ -q` passed with "3 passed in 0.01s".

Two environment probes and a green run. Rendered as a bare list:

    [FAIL] exit=1 0.0s  cd /workspace/…/repo && python -m pytest tests/ -q
    [FAIL] exit=1 0.1s  python -m pytest tests/ -q
    [ok  ] exit=0 3.6s  pip install pytest -q && python -m pytest tests/ -q

a reviewer opening the pull request sees two failing test runs above one pass
and reasonably concludes the suite is flaky. That damages the best artefact
this feature produces.

NOTHING IS HIDDEN. Dropping a failed command would be the opposite mistake and
would contradict the product's own argument — that it reports what it did not
check. The failures stay on the page; they stop being the first thing read.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def repo(tmp_path):
    """A real empty git repo — `_commit_message` shells out for the diffstat,
    and stubbing that would test a different function."""
    import subprocess
    from types import SimpleNamespace as NS

    g = ["git", "-C", str(tmp_path)]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*g, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*g, "config", "user.name", "t"], check=True)
    (tmp_path / "x.py").write_text("x = 1\n")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "init"], check=True)
    return NS(path=tmp_path)


def render(verifications: list[dict], repo=None, requested_by: str = "") -> str:
    from types import SimpleNamespace as NS

    from src.agent.workspace import _commit_message
    ws = NS(session_id="478d671b-75d7-41a3-8e03-7691b6bae3e8")
    return _commit_message(
        ws, repo, prompt="fix it", summary="fixed",
        requested_by=requested_by,
        verifications=verifications,
    )


PROBES_THEN_PASS = [
    {"command": "cd /workspace/x/repo && python -m pytest tests/ -q",
     "ok": False, "exit_code": 1, "elapsed": 0.0},
    {"command": "python -m pytest tests/ -q",
     "ok": False, "exit_code": 1, "elapsed": 0.1},
    {"command": "pip install pytest -q && python -m pytest tests/ -q",
     "ok": True, "exit_code": 0, "elapsed": 3.6},
]


def test_the_verdict_comes_before_the_log(repo):
    body = render(PROBES_THEN_PASS, repo)

    assert body.index("RESULT:") < body.index("[FAIL]")


def test_the_verdict_says_the_final_command_passed(repo):
    body = render(PROBES_THEN_PASS, repo)

    assert "the final one passed" in body
    assert "not\n" not in body.split("RESULT:")[1][:200] or "not failing checks" in body


def test_every_command_is_still_shown(repo):
    """Hiding a failed command would be the opposite mistake."""
    body = render(PROBES_THEN_PASS, repo)

    assert body.count("[FAIL]") == 2
    assert body.count("[ok  ]") == 1


def test_a_genuinely_failing_last_command_is_not_softened(repo):
    body = render([
        {"command": "python -m pytest", "ok": True, "exit_code": 0, "elapsed": 1},
        {"command": "python -m pytest", "ok": False, "exit_code": 1, "elapsed": 1},
    ], repo)

    assert "did NOT pass" in body
    assert "Read the branch before trusting it" in body


def test_nothing_passing_keeps_its_warning(repo):
    body = render([
        {"command": "python -m pytest", "ok": False, "exit_code": 1, "elapsed": 1},
    ], repo)

    assert "NOTHING PASSED" in body


def test_an_all_green_run_gets_no_verdict_noise(repo):
    """A clean log needs no explaining."""
    body = render([
        {"command": "python -m pytest", "ok": True, "exit_code": 0, "elapsed": 1},
    ], repo)

    assert "RESULT:" not in body
    assert "[ok  ]" in body


def test_no_commands_still_says_so(repo):
    body = render([], repo)

    assert "no command was executed" in body


# ─── who authorised this ─────────────────────────────────────────────


def test_the_commit_names_the_person_who_asked(repo):
    """An agent commit was authored by `Celmis Agent <agent@celmis.local>`
    with no GitHub user association and no signature. From git alone an
    auditor could not tell WHICH human authorised a change to a repository —
    only `Celmis session: <uuid>` linked back, and resolving that needs Celmis
    access. For a product whose argument is that a machine can be trusted to
    push, "who let it" is the first question."""
    body = render(PROBES_THEN_PASS, repo,
                  requested_by="Kostiantyn Makoid <k@example.com>")

    assert "Requested-by: Kostiantyn Makoid <k@example.com>" in body


def test_the_commit_is_attributable_where_people_look(repo):
    """`Co-authored-by` is what GitHub renders as an avatar and indexes for
    search. The trailer alone is the audit answer; this is the visible one."""
    body = render(PROBES_THEN_PASS, repo,
                  requested_by="Kostiantyn Makoid <k@example.com>")

    assert "Co-authored-by: Kostiantyn Makoid <k@example.com>" in body


def test_an_unresolvable_user_adds_nothing(repo):
    """A wrong name is worse than none."""
    body = render(PROBES_THEN_PASS, repo, requested_by="")

    assert "Requested-by" not in body
    assert "Co-authored-by" not in body
    assert "Celmis session:" in body


def test_the_session_link_survives(repo):
    body = render(PROBES_THEN_PASS, repo, requested_by="A B <a@b.c>")

    assert "Celmis session: 478d671b-75d7-41a3-8e03-7691b6bae3e8" in body
