"""An operator's glob is judged against a path in time proportional to the path, whatever the glob says.

THE DEFECT. `SECRET_PATH_GLOBS_EXTRA` globs were turned into regular expressions in which every `*` is a
bare `.*`. A regular expression backtracks, so `*a*a*a*.yaml` against a 20-segment path of `a`s took 11 s
(and a 120-segment one, minutes), and `**/a/**/b/**/c.txt` against a 4 KB path 2.5 s. The path is whatever a
caller of an MCP tool sends, so one request could hold a worker for as long as it liked. `fnmatch`, which
the globs used before they read like `.gitignore` lines, is linear.

THE CONTRACT. The glob is run over the path in one pass (`secret_files._Glob`): the work is the length of the
path times the length of the glob, nothing is retried. A path no filesystem can hold
(`_GLOB_PATH_MAX_CHARS`) is withheld without being read glob by glob.

The patterns and the 4 KB paths are judged in a child process: a regression is then a failed test after a few
seconds, not a test run that never comes back (the regular expression holds the interpreter while it backtracks).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.security import secret_files as sf

ROOT = Path(__file__).resolve().parents[2]
HIDDEN, SHOWN = "deny", "ok"

#: Run in the child: `classify` each (glob, path) with that glob as the operator's only one; report the
#: verdicts and the seconds all of them took together.
_CHILD = """
import json, sys, time
from src.security import secret_files as sf
cases = json.load(sys.stdin)
sf.classify("warm/up.txt")  # imports are paid for before the clock starts
t0 = time.perf_counter()
verdicts = []
for glob, rel in cases:
    sf._extra_globs = lambda g=glob: (g,)
    verdicts.append(sf.classify(rel))
print(json.dumps({"verdicts": verdicts, "seconds": time.perf_counter() - t0}))
"""

A_SEGMENTS = "/".join(["a" * 30] * 120)                    # 3719 characters, no dot in it
AB_SEGMENTS = "/".join(["a", "b"] * 1000)                  # 3999 characters
A_ONLY = "/".join(["a"] * 2000)                            # 3999 characters
A_RUN = "a" * 4000

#: (glob, path, verdict): paths of about 4 KB that made the regular expression backtrack, each with the
#: spelling that ends them differently (the literal tail is present but the name goes on; the tail is
#: really there), so that the glob has to be read to the end rather than refused for want of a letter.
ADVERSARIAL = [
    ("*a*a*a*.yaml", A_SEGMENTS, SHOWN),
    ("*a*a*a*.yaml", A_SEGMENTS + "/x.yaml.bak", SHOWN),
    ("*a*a*a*.yaml", A_SEGMENTS + "/x.yaml", HIDDEN),
    ("**/a/**/b/**/c.txt", AB_SEGMENTS, SHOWN),
    ("**/a/**/b/**/c.txt", AB_SEGMENTS + "/c.txt.bak", SHOWN),
    ("**/a/**/b/**/c.txt", AB_SEGMENTS + "/c.txt", HIDDEN),
    ("**/a/**/b/**/c.txt", A_ONLY + "/b/c.txt.bak", SHOWN),
    ("**/a/**/b/**/c.txt", A_ONLY + "/b/c.txt", HIDDEN),
    ("a*a*a*a*a*a*a*a*b", A_RUN + "c", SHOWN),
    ("a*a*a*a*a*a*a*a*b", A_RUN + "b", HIDDEN),
    ("*/*/*/*/*/*/*/*/x", A_ONLY + "/x/y", HIDDEN),
    ("[ab]*[ab]*[ab]*[ab]*z", A_SEGMENTS + "/zy", SHOWN),
    ("a?a?a?a?a?a?a?a?a?b/**/c", A_RUN + "b/d", SHOWN),
]


def _judge(cases: list[tuple[str, str, str]], *, limit: float = 15.0) -> dict[str, object]:
    try:
        done = subprocess.run(
            [sys.executable, "-c", _CHILD], input=json.dumps([[g, p] for g, p, _ in cases]),
            capture_output=True, text=True, timeout=limit, cwd=ROOT, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"judging {len(cases)} paths of ~4 KB took more than {limit:.0f} s: the glob backtracks")
    assert done.returncode == 0, done.stderr[-400:]
    return json.loads(done.stdout)


def test_an_adversarial_glob_on_a_path_of_four_kilobytes_is_judged_in_a_fraction_of_a_second():
    out = _judge(ADVERSARIAL)

    assert out["verdicts"] == [v for _, _, v in ADVERSARIAL]
    assert out["seconds"] < 0.5, f"{len(ADVERSARIAL)} paths took {out['seconds']:.2f} s"


def test_a_path_no_filesystem_could_hold_is_withheld_without_being_read_by_the_globs(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sf, "_extra_globs", lambda: ("config",))
    monkeypatch.setattr(sf, "_extra_hit", lambda low, globs: calls.append(low) or False)
    long_path = "/".join(["segment"] * (sf._GLOB_PATH_MAX_CHARS // 4)) + "/app.txt"

    assert len(long_path) > sf._GLOB_PATH_MAX_CHARS
    assert sf.classify(long_path) == HIDDEN
    assert calls == []
    assert sf.classify("src/app.txt") == SHOWN       # an ordinary path still goes through the globs
    assert calls == ["src/app.txt"]


def test_without_globs_a_long_path_is_judged_by_the_built_in_lists_only(monkeypatch):
    monkeypatch.setattr(sf, "_extra_globs", lambda: ())
    long_path = "/".join(["segment"] * (sf._GLOB_PATH_MAX_CHARS // 4)) + "/app.txt"

    assert sf.classify(long_path) == SHOWN
