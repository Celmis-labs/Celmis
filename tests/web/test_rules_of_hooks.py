"""No hook may sit below an early return. This one took the whole site down.

`WorkspaceSwitcher` renders in the top bar of every authenticated page. It
guards on the session — `if (!jwt) return null;` — and two hooks for the mobile
click-away fix were added BELOW that line. The first render happens while
`useSession()` is still loading, so `jwt` is undefined, the component returns
early, and eight hooks run. The session arrives, the early return is skipped,
and ten run. React counts hooks, sees more than last time, throws error #310,
and the error boundary unmounts the entire tree into "This page couldn't load".

Every page behind a login, at once.

Nothing about the placement looks wrong when reading the diff — the hooks sit
next to the code that uses them, which is where you would put them. `tsc` does
not model hook order, `next build` never renders a client component with a
loading session, and the guard test written for that change grepped the file
for `addEventListener("pointerdown"` — which was still perfectly true while the
component was crashing on every load.

So this test does not read the source. It runs the linter that already knows
the rule, and it runs it over the whole app rather than the file somebody
happened to remember. eslint reported both violations the entire time; nobody
pointed it at that file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"

# The rule that matters here. Kept to a named set rather than "eslint is
# clean", so that the pre-existing style errors elsewhere in the app do not
# make this test permanently red and therefore permanently ignored.
FATAL_RULES = {"react-hooks/rules-of-hooks"}


def _eslint_available() -> bool:
    return (WEB / "node_modules" / ".bin" / "eslint").exists()


@pytest.mark.skipif(not _eslint_available(), reason="web deps not installed")
def test_no_hook_is_called_conditionally_anywhere_in_the_app():
    proc = subprocess.run(
        [str(WEB / "node_modules" / ".bin" / "eslint"),
         "app", "components", "lib", "--format", "json"],
        cwd=WEB, capture_output=True, text=True, timeout=900,
    )
    # eslint exits non-zero when it reports anything at all, including the
    # style errors this test deliberately ignores — so the exit code says
    # nothing and the report is the only thing worth reading.
    try:
        report = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:  # pragma: no cover - eslint itself broke
        pytest.fail(f"eslint produced no parseable report:\n{proc.stderr[-2000:]}")

    violations = [
        f"{Path(f['filePath']).relative_to(WEB)}:{m['line']} {m['message']}"
        for f in report
        for m in f.get("messages", [])
        if m.get("ruleId") in FATAL_RULES
    ]
    assert not violations, (
        "a hook is called conditionally — this unmounts the page tree at "
        "runtime with React error #310:\n  " + "\n  ".join(violations)
    )


@pytest.mark.skipif(not _eslint_available(), reason="web deps not installed")
def test_the_switcher_calls_its_hooks_before_the_session_guard():
    """The specific shape that broke, pinned cheaply so a reader of this file
    sees what it is about without running eslint.

    The lint rule above is the real guard; this one is the story. It checks
    order, not presence — presence is what the previous guard checked, and
    presence was true throughout the outage.
    """
    source = (WEB / "components" / "app-shell.tsx").read_text()
    start = source.index("function WorkspaceSwitcher(")
    body = source[start:source.index("\nfunction ", start + 10)]

    guard = body.index("if (!jwt) return null;")
    for hook in ("useRef(", "useEffect(", "useState("):
        after = body.find(hook, guard)
        assert after == -1, (
            f"{hook} is called after the `!jwt` early return; the first render "
            "returns early and the next one does not, so React sees a "
            "different number of hooks and throws #310"
        )
