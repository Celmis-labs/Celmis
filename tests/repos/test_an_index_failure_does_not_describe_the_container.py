"""An index failure returned the deployment's directory layout to the browser.

    raise IndexError_(f"Index failed: {exc}")

and the router hands that straight back as an HTTP 500 `detail`. Two things
rode out on it:

  * THE CONTAINER'S LAYOUT — messages reaching the browser read
    `/workspace/repos/github_acme-worker/.git`, which tells a stranger the
    deployment's directory structure and the naming scheme of everything
    under it. The path INSIDE the repository is what a person needs;
    the absolute prefix is ours.

  * ANYTHING A LIBRARY PUT IN AN EXCEPTION. `CloneError` strips credentials —
    but it is one type out of everything the indexer can raise, and a
    subprocess error carries the argv it was built from, which for a clone
    contains a token. The log filter closed that hole towards the logs; this
    is the same hole pointing at the browser.

Sanitised at the RAISE SITE, not in the router: the exception is documented as
carrying "a message the caller can show or log verbatim", and that is only
true if it is true where it is raised.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.repos.indexing import safe_detail

TOKEN = "ghp_" + "A" * 36


def test_the_repos_root_is_replaced_by_a_label():
    s = get_settings()
    out = safe_detail(f"fatal: could not read {s.repos_dir}/github_acme-worker/.git")
    assert str(s.repos_dir) not in out
    assert "<repos>" in out


def test_the_path_inside_the_repository_survives():
    """The half that is useful. Stripping everything would be a message that
    tells the user nothing, which is the other way to fail this."""
    out = safe_detail("Index failed: src/settlement.py has a syntax error")
    assert "src/settlement.py" in out


def test_a_credential_in_a_subprocess_error_never_reaches_the_caller():
    out = safe_detail(
        f"Index failed: Cmd('git') failed: git clone "
        f"https://x-access-token:{TOKEN}@github.com/acme/worker.git")
    assert TOKEN not in out


@pytest.mark.parametrize("root", ["repos_dir", "data_dir", "logs_dir", "vault_dir"])
def test_every_configured_root_is_covered(root):
    s = get_settings()
    path = getattr(s, root)
    out = safe_detail(f"boom at {path}/whatever")
    assert str(path) not in out, f"{root} leaked"


def test_the_longest_root_wins():
    """`workspace_dir` is a prefix of the others. Replacing it first would
    turn `/workspace/repos/x` into `<workspace>/repos/x` — still true, but it
    throws away the more specific label for no reason."""
    s = get_settings()
    out = safe_detail(f"boom at {s.repos_dir}/github_acme-worker")
    assert "<repos>" in out and "<workspace>/repos" not in out


def test_a_long_message_is_bounded():
    out = safe_detail("x" * 5000)
    assert len(out) <= 400


def test_sanitising_never_raises():
    """It runs inside a raise path. An exception here would replace a
    diagnosable failure with an undiagnosable one."""
    assert safe_detail("") == ""
    assert safe_detail(None) == ""


def test_the_router_returns_the_sanitised_text():
    """End of the chain: whatever `IndexError_` carries is what the browser
    gets, so the guarantee has to hold on the exception itself."""
    import ast
    from pathlib import Path

    # `detail=message` where message came from str(exc) — the router does not
    # re-sanitise, by design, so every raise site must.
    idx = Path(__file__).resolve().parents[2] / "src/repos/indexing.py"
    itree = ast.parse(idx.read_text(encoding="utf-8"))
    raises = [n for n in ast.walk(itree)
              if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
              and isinstance(n.exc.func, ast.Name)
              and n.exc.func.id == "IndexError_"]
    assert raises, "no IndexError_ raises found — did the module move?"
    unwrapped = []
    for r in raises:
        arg = r.exc.args[0] if r.exc.args else None
        # A plain literal is fine: it contains no interpolation to leak.
        if isinstance(arg, ast.Constant):
            continue
        if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id == "safe_detail"):
            unwrapped.append(r.lineno)
    assert not unwrapped, (
        f"IndexError_ raised with an interpolated message and no safe_detail() "
        f"at lines {unwrapped} — that text reaches the browser verbatim"
    )
