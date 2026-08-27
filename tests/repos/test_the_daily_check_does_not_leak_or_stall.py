"""A daily check runs the same command on every repository, every day.

Which changes what a small flaw costs. A credential in argv is one process
listing during one push; the same credential in a scheduled `ls-remote` is
every repository, every day, forever. A missing timeout is one slow request;
here it is a scheduler tick that never ends and a sweep that silently stops
happening.

So this file holds three properties that only matter because of the schedule:
the secret is never an argument, the inherited credential-helper list is
cleared before ours is added, and the network call is bounded.

The helper list is not a detail. `-c credential.helper=<x>` APPENDS; it does
not replace. A helper configured in the image answers first and can hand git
somebody else's credential — which surfaces as a permission error on an
account that has permission, and reads as a broken token rather than a wrong
one.
"""
from __future__ import annotations

import subprocess

import pytest

from src.repos import freshness


@pytest.fixture
def spy(monkeypatch):
    seen = {}

    def fake(args, **kw):
        seen["args"] = list(args)
        seen["env"] = dict(kw.get("env") or {})
        seen["timeout"] = kw.get("timeout")
        return subprocess.CompletedProcess(
            args, 0, stdout=f"{'a' * 40}\tHEAD\n", stderr="")

    monkeypatch.setattr(freshness.subprocess, "run", fake)
    return seen


SECRET = "ghp_notinargv"


def test_the_secret_is_never_a_command_line_argument(spy):
    freshness._run_ls_remote(
        "https://github.com/acme/w.git", "HEAD",
        {"CELMIS_GIT_USER": "x-access-token", "CELMIS_GIT_PW": SECRET})
    joined = " ".join(spy["args"])
    assert SECRET not in joined, f"the token is in argv: {joined[:120]}"
    assert "@github.com" not in joined, "the URL carries a credential"


def test_the_secret_travels_in_the_environment(spy):
    freshness._run_ls_remote(
        "https://github.com/acme/w.git", "HEAD",
        {"CELMIS_GIT_USER": "x-access-token", "CELMIS_GIT_PW": SECRET})
    assert spy["env"]["CELMIS_GIT_PW"] == SECRET


def test_the_inherited_helper_list_is_cleared_first(spy):
    freshness._run_ls_remote("https://github.com/acme/w.git", "HEAD",
                             {"CELMIS_GIT_USER": "u", "CELMIS_GIT_PW": SECRET})
    args = spy["args"]
    helpers = [args[i + 1] for i, a in enumerate(args)
               if a == "-c" and args[i + 1].startswith("credential.helper")]
    assert helpers and helpers[0] == "credential.helper=", (
        "a helper configured in the image answers before ours"
    )
    assert len(helpers) >= 2


def test_a_public_remote_is_asked_without_any_helper_at_all(spy):
    """No credential means no credential machinery.

    Configuring an empty helper for an anonymous request would still clear the
    list, and on a box where a helper IS the intended way to reach a mirror
    that would break a working setup for no gain.
    """
    freshness._run_ls_remote("https://github.com/acme/w.git", "HEAD", None)
    assert "credential.helper=" not in spy["args"]
    assert "CELMIS_GIT_PW" not in spy["env"]


def test_the_network_call_is_bounded(spy):
    freshness._run_ls_remote("https://github.com/acme/w.git", "HEAD", None)
    assert spy["timeout"] == freshness.LS_REMOTE_TIMEOUT_SECONDS
    assert 0 < spy["timeout"] <= 60, (
        "a scheduler tick that waits minutes on one remote stops being daily"
    )


def test_the_environment_is_not_inherited(spy):
    """The api process holds the credential store's master key."""
    freshness._run_ls_remote("https://github.com/acme/w.git", "HEAD", None)
    assert "CELMIS_MASTER_KEY" not in spy["env"]
    assert set(spy["env"]) <= {
        "GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "PATH", "GIT_SSH_COMMAND",
        "CELMIS_GIT_USER", "CELMIS_GIT_PW",
    }


# ─── what the remote actually said ───────────────────────────────────

def _answer(monkeypatch, stdout, code=0, stderr=""):
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, code, stdout, stderr))


def test_the_sha_is_taken_from_the_first_column(monkeypatch):
    _answer(monkeypatch, f"{'b' * 40}\tHEAD\n")
    assert freshness._run_ls_remote("u", "HEAD", None) == "b" * 40


def test_a_ref_that_does_not_exist_is_an_error_not_an_absence(monkeypatch):
    """An empty answer means the branch is gone — renamed, deleted.

    A real condition with a real remedy, and emphatically not "no changes".
    Returning None here would have the caller record a successful check that
    found nothing.
    """
    _answer(monkeypatch, "")
    with pytest.raises(RuntimeError, match="no ref"):
        freshness._run_ls_remote("u", "refs/heads/gone", None)


def test_a_failure_is_reported_with_credentials_stripped(monkeypatch):
    _answer(monkeypatch, "", code=128,
            stderr="fatal: could not read Username for "
                   "'https://x-access-token:ghp_leak@github.com'")
    with pytest.raises(RuntimeError) as e:
        freshness._run_ls_remote("u", "HEAD", None)
    assert "ghp_leak" not in str(e.value)
    assert "REDACTED" in str(e.value)


def test_garbage_where_a_sha_belongs_is_rejected(monkeypatch):
    """A proxy's HTML error page parses as a first column too."""
    _answer(monkeypatch, "<html>502 Bad Gateway</html>\tHEAD\n")
    with pytest.raises(RuntimeError):
        freshness._run_ls_remote("u", "HEAD", None)


# ─── the ref is a name, not a glob ───────────────────────────────────

def test_a_sibling_branch_cannot_answer_for_the_one_we_track(monkeypatch):
    """`ls-remote <url> main` also matches `feature/main`, and sorts it FIRST.

    Reproduced against real git: a repository with both branches answers two
    lines, `refs/heads/feature/main` on line zero. Taking line zero compared
    the tracked branch against a neighbour — which reads as permanently behind
    or permanently current depending on which neighbour happened to exist.
    """
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 0,
            f"{'f' * 40}\trefs/heads/feature/main\n{'c' * 40}\trefs/heads/main\n", ""))
    assert freshness._run_ls_remote("u", "refs/heads/main", None) == "c" * 40


def test_a_bare_branch_name_is_qualified_before_matching(monkeypatch):
    """Defence in depth: even handed a bare name, match the real ref."""
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 0,
            f"{'f' * 40}\trefs/heads/feature/main\n{'c' * 40}\trefs/heads/main\n", ""))
    assert freshness._run_ls_remote("u", "main", None) == "c" * 40


def test_head_is_matched_as_head(monkeypatch):
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 0, f"{'d' * 40}\tHEAD\n{'e' * 40}\trefs/heads/main\n", ""))
    assert freshness._run_ls_remote("u", "HEAD", None) == "d" * 40


def test_a_branch_that_is_absent_raises_even_when_others_answered(monkeypatch):
    """The dangerous shape: output exists, just not for the branch we asked."""
    monkeypatch.setattr(
        freshness.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 0, f"{'f' * 40}\trefs/heads/feature/main\n", ""))
    with pytest.raises(RuntimeError, match="no ref"):
        freshness._run_ls_remote("u", "refs/heads/main", None)


def test_the_ref_asked_for_is_fully_qualified(monkeypatch):
    """A glob reaching the remote at all is the defect; qualify before sending."""
    seen = {}

    class _Cfg:
        provider = "github"
        url = "https://github.com/acme/w.git"
        branch = "main"

    class _Store:
        def get_in_workspace(self, ws, slug): return _Cfg()
        def get(self, uid, slug): return _Cfg()

    monkeypatch.setattr("src.api.auto_review.get_auto_review_store", lambda: _Store())
    monkeypatch.setattr("src.credentials.resolve_git_credential",
                        lambda *a, **k: None)
    monkeypatch.setattr(freshness, "_run_ls_remote",
                        lambda url, ref, env: seen.setdefault("ref", ref) or "a" * 40)
    freshness.remote_head("slug", workspace_id="ws")
    assert seen["ref"] == "refs/heads/main"


# ─── the username slot is not one value per host ─────────────────────

def test_bitbucket_gets_the_username_its_token_type_requires(monkeypatch):
    """`x-access-token` authenticates as nobody on Bitbucket.

    And the failure reads as a bad token rather than a wrong username, which
    is a diagnosis that sends somebody to rotate a credential that was fine.
    The rules live in git_auth_kwargs; this only checks we ask it.
    """
    class _Creds:
        secret = "ATATTsomething"
        metadata = {}

    got = freshness._basic_auth("bitbucket", _Creds())
    assert got is not None
    assert got["CELMIS_GIT_USER"] != "x-access-token", (
        "the Bitbucket username slot was filled from a two-entry table"
    )
    assert got["CELMIS_GIT_PW"] == "ATATTsomething"


def test_a_legacy_app_password_uses_its_stored_username(monkeypatch):
    """`creds.metadata` is where that username lives, and it was being dropped."""
    class _Creds:
        secret = "app-password-value"
        metadata = {"username": "kmakoid"}

    got = freshness._basic_auth("bitbucket", _Creds())
    assert got and got["CELMIS_GIT_USER"] == "kmakoid"


def test_github_still_gets_the_token_username(monkeypatch):
    class _Creds:
        secret = "ghp_x"
        metadata = {}

    got = freshness._basic_auth("github", _Creds())
    assert got and got["CELMIS_GIT_PW"] == "ghp_x"
    assert got["CELMIS_GIT_USER"] == "x-access-token"


def test_no_secret_means_no_credential_machinery():
    class _Creds:
        secret = ""
        metadata = {}

    assert freshness._basic_auth("github", _Creds()) is None
