"""Importing litellm must not copy some `.env` into this process's environment.

`import litellm` calls `dotenv.load_dotenv()` in its default mode, which walks
up from litellm's own file in site-packages. With the virtualenv inside the
main checkout, that loaded the MAIN checkout's `.env` the first time anything
imported litellm — so a review setting cached before the first LLM call (300)
disagreed with one read after it (600), and a test passed or failed depending
on which test file happened to run first.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import dotenv
calls = []
dotenv.load_dotenv = lambda *a, **kw: calls.append((a, kw)) or True
import src  # what every Celmis entry point imports first
import litellm  # noqa: F401
print(len(calls))
"""


def _run(env: dict) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=120, check=True,
    )
    return out.stdout.strip().splitlines()[-1]


def test_litellm_is_imported_without_loading_a_dotenv():
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_MODE"}
    assert _run(env) == "0", "litellm loaded a .env into os.environ on import"


def test_the_probe_would_see_it_happen():
    """Without the default, the same probe counts the call — so the 0 above
    is the default working, not a probe that cannot see anything."""
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_MODE"}
    env["LITELLM_MODE"] = "DEV"
    assert int(_run(env)) >= 1
