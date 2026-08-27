"""The push URL carried the token, and the docstring said it did not.

`src/agent/workspace.py` opens with a paragraph headed "Credential hygiene",
and it is the paragraph somebody auditing this file reads:

    The token is re-injected only for the one `git push` invocation (via a
    one-shot env askpass), which keeps the token out of `.git/config`

Two of those three claims were true. The token really was re-injected for one
invocation, and `.git/config` really was scrubbed. But there is no askpass
that supplies a token anywhere in this repository — `GIT_ASKPASS: "echo"` in
workspace.py and clone.py suppresses a prompt, it does not answer one. The
token reached git inside `repo.push_url`:

    https://x-access-token:<token>@github.com/owner/repo.git

passed as an ARGUMENT to `git push`. An argument is visible in `ps auxww` to
anyone on the box for as long as the push runs.

That is not a theoretical exposure. A token embedded in a remote URL was read
out of this very repository's `.git/config` by a routine `git remote -v` on
the day this test was written — no misconfiguration, no attack, just a command
whose output nobody expected to contain a secret.

The wrong half of the sentence is the dangerous half: an auditor who reads
"env askpass" rules out argv and process listings, and is wrong to.

The token now goes through a credential helper that reads it from the
environment, and the URL git is given carries no credentials at all.

AND THE HELPER LIST IS CLEARED FIRST. `git -c credential.helper=…` APPENDS;
it does not replace. A helper configured in the environment — osxkeychain on
a developer's machine, a store helper in an image — answers before ours and
can hand git a different account's credential. That failure looks exactly like
a permissions problem: `remote: Permission to … denied to <someone>`, on an
account with full rights. It cost three attempts to diagnose by hand.
"""
from __future__ import annotations

import inspect

from src.agent import workspace


class _FakeRepo:
    slug = "github_acme-widgets"
    path = "/tmp/does-not-matter"
    clean_url = "https://github.com/acme/widgets.git"
    push_url = "https://x-access-token:ghp_SECRET123%2Fx@github.com/acme/widgets.git"
    default_branch = "main"


class _Calls:
    """Stands in for `_run`, recording every git invocation."""

    def __init__(self, dirty=True):
        self.calls: list[dict] = []
        self.dirty = dirty

    def __call__(self, cmd, cwd, timeout=300, env_extra=None):
        self.calls.append({"cmd": list(cmd), "env": dict(env_extra or {})})
        out = "M f.py" if (self.dirty and "status" in cmd) else ""
        if "rev-parse" in cmd:
            out = "deadbeef"
        return type("P", (), {"stdout": out, "stderr": "", "returncode": 0})()

    def push(self) -> dict:
        return next(c for c in self.calls if "push" in c["cmd"])


def _do_push(monkeypatch, repo=None) -> _Calls:
    calls = _Calls()
    ws = type("WS", (), {"session_id": "b8960e01-dead", "repos": [repo or _FakeRepo()]})()
    monkeypatch.setattr(workspace, "_run", calls)
    monkeypatch.setattr(workspace, "_commit_message", lambda *a, **k: "msg")
    monkeypatch.setattr(workspace, "_compare_url", lambda *a, **k: None)
    workspace.commit_and_push(ws)
    return calls


SECRET = "ghp_SECRET123/x"          # the decoded form of what push_url carries


def test_the_secret_never_appears_in_the_push_command(monkeypatch):
    """`ps auxww` shows argv. It must not show this.

    Behavioural on purpose: an earlier version of this test asserted that the
    string "push_url" was absent from the source, which is a claim about
    spelling. The code still reads push_url — it has to, that is where the
    credential lives — and splits it.
    """
    push = _do_push(monkeypatch).push()
    joined = " ".join(push["cmd"])
    assert SECRET not in joined, f"the token is in argv: {joined[:120]}"
    assert "ghp_" not in joined
    assert "@github.com" not in joined, "the URL still carries a credential"


def test_the_url_git_is_given_is_the_clean_one(monkeypatch):
    push = _do_push(monkeypatch).push()
    assert "https://github.com/acme/widgets.git" in push["cmd"]


def test_the_secret_reaches_git_through_the_environment(monkeypatch):
    push = _do_push(monkeypatch).push()
    assert push["env"].get("CELMIS_GIT_PW") == SECRET, (
        "the helper has nothing to read; the push will fail to authenticate"
    )
    assert push["env"].get("CELMIS_GIT_USER") == "x-access-token"


def test_the_secret_is_percent_decoded_on_the_way_out(monkeypatch):
    """`push_url` builds with quote(token, safe=""), so `/` arrives as %2F.

    A helper that emitted the encoded form would authenticate with a password
    that is not the token — and the failure would look like a bad token.
    """
    push = _do_push(monkeypatch).push()
    assert "%2F" not in push["env"]["CELMIS_GIT_PW"]
    assert push["env"]["CELMIS_GIT_PW"].endswith("/x")


def test_only_the_push_carries_the_credential(monkeypatch):
    """status, add, commit and rev-parse have no business holding it."""
    calls = _do_push(monkeypatch)
    for c in calls.calls:
        if "push" in c["cmd"]:
            continue
        assert not c["env"], f"{c['cmd'][:4]} was handed {sorted(c['env'])}"


def test_a_destination_without_credentials_is_used_unchanged(monkeypatch):
    """A public remote, or a local path in a test.

    The first version of this substituted `clean_url` whenever `push_url` had
    no credential in it. tests/agent/test_workspace_push.py caught it: those
    tests push to a local bare repo whose path IS the push_url, while
    clean_url is a fake github.com address — so three real pushes went to
    github.com and got "Repository not found". `push_url` is where to push;
    the credential is only an aspect of it.
    """
    class _Local(_FakeRepo):
        push_url = "/tmp/some/bare/repo.git"

    push = _do_push(monkeypatch, _Local()).push()
    assert "/tmp/some/bare/repo.git" in push["cmd"]
    assert "https://github.com/acme/widgets.git" not in push["cmd"]
    assert not push["env"]


def test_the_inherited_helper_list_is_cleared_before_ours_is_added(monkeypatch):
    """`-c credential.helper=<x>` APPENDS. Ours must be the only one asked.

    A helper configured in the environment answers first and can hand git
    another account's credential — a failure that reads as
    `remote: Permission to … denied to <someone>` on an account with full
    rights.
    """
    cmd = _do_push(monkeypatch).push()["cmd"]
    settings = [cmd[i + 1] for i, a in enumerate(cmd)
                if a == "-c" and cmd[i + 1].startswith("credential.helper")]
    assert settings, "no credential helper is configured for the push"
    assert settings[0] == "credential.helper=", (
        f"the first credential.helper setting is {settings[0]!r}; it must be "
        f"the empty one that resets the inherited list"
    )
    assert len(settings) >= 2, "the list is cleared and never refilled"


def test_the_helper_reads_the_password_from_a_variable(monkeypatch):
    cmd = _do_push(monkeypatch).push()["cmd"]
    helper = [a for a in cmd if a.startswith("credential.helper=!")]
    assert helper, "no helper script"
    assert "$CELMIS_GIT_PW" in helper[0]
    assert SECRET not in helper[0]


def test_a_failed_push_still_redacts():
    """The existing guarantee must survive the change."""
    src = inspect.getsource(workspace._run)
    assert src.count("strip_credentials") >= 2, (
        "the failure path no longer redacts both the command and its stderr"
    )


def test_the_docstring_no_longer_claims_an_askpass_supplies_the_token():
    """The sentence that made an auditor stop looking.

    Keyed on the claim, not on wording: any text saying the token arrives via
    askpass is false while no askpass in this repository supplies one.
    """
    doc = (workspace.__doc__ or "").lower()
    assert "askpass" not in doc or "suppress" in doc or "not" in doc, (
        "the module docstring still credits an askpass with delivering the token"
    )


def test_no_askpass_in_the_repository_actually_supplies_a_token():
    """The premise of this whole file, pinned.

    If someone later adds a real token-supplying askpass, this fails and the
    prose above needs rewriting rather than quietly becoming wrong again.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    suppliers = []
    for path in (root / "src").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "ASKPASS" in line and "=" in line:
                value = line.split("ASKPASS", 1)[1]
                if '"echo"' not in value and "'echo'" not in value:
                    suppliers.append(f"{path.relative_to(root)}: {line.strip()[:70]}")
    assert not suppliers, "an askpass now supplies something: " + "; ".join(suppliers)


def _env_of(monkeypatch, **kw) -> dict:
    """The environment `_run` actually hands subprocess.run."""
    seen = {}

    def fake(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return type("P", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(workspace.subprocess, "run", fake)
    workspace._run(["git", "status"], cwd="/tmp", **kw)
    return seen


def test_run_adds_nothing_to_the_environment_unless_asked(monkeypatch):
    """Guards the real `_run`, which the fake in the tests above replaces.

    Those tests record the `env_extra` that `commit_and_push` passes, so a
    credential injected INSIDE `_run` — a stray default, a module-level
    fallback — is invisible to every one of them. It was: mutating `_run` to
    set a password for all callers left the whole file green.
    """
    env = _env_of(monkeypatch)
    assert env == {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                   "PATH": "/usr/bin:/bin:/usr/local/bin"}, (
        f"_run puts something else in the environment of every git call: "
        f"{sorted(set(env) - {'GIT_TERMINAL_PROMPT', 'GIT_ASKPASS', 'PATH'})}"
    )


def test_run_passes_through_exactly_what_it_was_given(monkeypatch):
    env = _env_of(monkeypatch, env_extra={"CELMIS_GIT_PW": "s3cret"})
    assert env["CELMIS_GIT_PW"] == "s3cret"
    assert env["GIT_TERMINAL_PROMPT"] == "0", "the base environment was replaced"


def test_the_environment_is_not_inherited_from_the_process(monkeypatch):
    """A minimal env, not os.environ.

    The api process holds the credential store's master key and every tenant's
    settings in its environment. Handing that to a git subprocess the agent
    can influence would be a much larger hole than the one this file closes.
    """
    monkeypatch.setenv("CELMIS_MASTER_KEY", "must-not-travel")
    env = _env_of(monkeypatch)
    assert "CELMIS_MASTER_KEY" not in env
