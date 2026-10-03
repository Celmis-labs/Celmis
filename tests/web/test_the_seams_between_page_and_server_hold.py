"""Four places where what the browser does and what the server means differ.

Each decision is compiled by the web app's own tsc and executed on node, so
what is asserted is what the browser runs:

* an agent-written href is a link only if it stays on this origin —
  `/\\evil.com` starts with one slash and is another site to a browser;
* a daily bucket "2026-10-01" is the 1st of October in New York too;
* a list page emptied under the reader moves back to a page with rows;
* the ignore-glob editor refuses what `validate_ignore_globs` refuses, so
  Save is not offered for a list the server will answer with a 422.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    POLICY,
    TSC,
    WEB,
    _lift,
)


def _compile_and_run(tmp: Path, sources: dict[str, str], main: str,
                     env: dict | None = None) -> object:
    for name, text in sources.items():
        (tmp / name).write_text(text, encoding="utf-8")
    (tmp / "main.ts").write_text(main, encoding="utf-8")
    build = subprocess.run(
        [str(TSC), "main.ts", *sources, "--target", "es2020", "--module",
         "commonjs", "--lib", "es2020,dom", "--outDir", "out", "--skipLibCheck"],
        cwd=tmp, capture_output=True, text=True, timeout=300,
    )
    js = tmp / "out" / "main.js"
    if not js.exists():  # pragma: no cover - tsc itself failed
        pytest.fail(f"tsc emitted nothing:\n{build.stdout}\n{build.stderr}")
    run = subprocess.run(["node", str(js)], capture_output=True, text=True,
                         timeout=120, env={**os.environ, **(env or {})})
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.fixture(autouse=True)
def _needs_web_deps():
    if not TSC.exists():
        pytest.skip("web deps not installed")


# ─── agent links ─────────────────────────────────────────────────────

HREFS = {
    "/connections": True,
    "/admin/review-policies/default?tab=general#x": True,
    "/\\evil.com": False,
    "/\\\\evil.com": False,
    "//evil.com": False,
    "/\t/evil.com": False,
    "/\n/evil.com": False,
    "javascript:alert(1)": False,
    "https://evil.com/": False,
    "": False,
}


def test_only_same_origin_paths_become_links(tmp_path):
    src = (WEB / "lib" / "in-app-href.ts").read_text(encoding="utf-8")
    got = _compile_and_run(
        tmp_path, {"in-app-href.ts": src},
        'import { isInAppHref } from "./in-app-href";\n'
        f"const cases: string[] = {json.dumps(list(HREFS))};\n"
        "console.log(JSON.stringify(cases.map((h) => isInAppHref(h))));\n",
    )
    assert dict(zip(HREFS, got, strict=True)) == HREFS


def test_the_note_renderer_uses_the_origin_check():
    """The helper only matters if the markdown `a` renderer is what calls it."""
    thread = (WEB / "components" / "automation" / "thread.tsx").read_text()
    assert ": isInAppHref(href) ? (" in thread
    assert 'href.startsWith("/") && !href.startsWith("//")' not in thread


# ─── calendar days ───────────────────────────────────────────────────


@pytest.mark.parametrize("tz", ["America/New_York", "Pacific/Auckland", "UTC"])
def test_a_calendar_day_is_the_same_day_everywhere(tmp_path, tz):
    src = (WEB / "lib" / "format.ts").read_text(encoding="utf-8")
    got = _compile_and_run(
        tmp_path, {"format.ts": src},
        'import { formatDate } from "./format";\n'
        "Object.defineProperty(globalThis, 'navigator', "
        "{ value: { language: 'en-US' }, configurable: true });\n"
        "console.log(JSON.stringify([formatDate('2026-10-01'), "
        "formatDate('2026-01-31')]));\n",
        env={"TZ": tz},
    )
    assert got == ["10/1/26", "1/31/26"], (tz, got)


# ─── a page emptied under the reader ─────────────────────────────────


PAGING = [
    # offset, total, items, expected
    (50, 50, 0, 0),        # closed the only row of page 2 → page 1
    (100, 60, 0, 50),      # shrank by more than a page
    (50, 0, 0, 0),         # everything gone → first page
    (0, 0, 0, None),       # first page, empty: the empty state is right
    (50, 51, 1, None),     # a page with rows is left alone
]


def test_an_emptied_page_moves_back_to_one_with_rows(tmp_path):
    src = (WEB / "lib" / "paging.ts").read_text(encoding="utf-8")
    cases = [c[:3] for c in PAGING]
    got = _compile_and_run(
        tmp_path, {"paging.ts": src},
        'import { clampedOffset } from "./paging";\n'
        f"const cases: number[][] = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(([o, t, n]) => "
        "clampedOffset(o, t, n, 50))));\n",
    )
    assert got == [c[3] for c in PAGING]


@pytest.mark.parametrize("page", ["issues", "pull-requests"])
def test_both_list_pages_apply_it(page):
    text = (WEB / "app" / "(app)" / page / "page.tsx").read_text()
    assert "clampedOffset(list.data.offset, list.data.total" in text
    assert "if (shrunkTo !== null) setOffset(shrunkTo);" in text


# ─── ignore globs ────────────────────────────────────────────────────


GLOBS = [
    "*a*b*c*d*e*f*g*h*i*",   # 9 stars
    "a\tb",                  # tab inside
    "src/[z-a].py",          # reversed range
    "vendor/**",
    "*.min.js",
    "src/[!a-c]x.py",
    "a[b",
]


def test_the_editor_refuses_what_the_server_refuses(tmp_path):
    from src.review.ignore_globs import validate_ignore_globs

    src = POLICY.read_text(encoding="utf-8")
    lifted = "\n\n".join(_lift(n, src, POLICY) for n in ("globBadClass", "globError"))
    got = _compile_and_run(
        tmp_path, {},
        lifted + "\n\n"
        f"const cases: string[] = {json.dumps(GLOBS)};\n"
        "console.log(JSON.stringify(cases.map((g) => globError([g])?.key ?? null)));\n",
    )
    for glob, key in zip(GLOBS, got, strict=True):
        try:
            validate_ignore_globs([glob])
            server_refuses = False
        except ValueError:
            server_refuses = True
        assert (key is not None) == server_refuses, (glob, key)


def test_every_refusal_has_words_in_every_language():
    messages = WEB / "lib" / "i18n" / "messages"
    for path in messages.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("review.settings.globTooManyStars",
                    "review.settings.globWhitespace",
                    "review.settings.globBadClass"):
            assert "{line}" in data[key], (path.name, key)


# ─── the agent panel and a screen reader ─────────────────────────────

WIDGET = WEB / "components" / "agent-widget.tsx"

ANNOUNCE = [
    # name, thread, settled-at-open ids (None = not loaded), expected
    ("not_loaded", [{"id": "a", "status": "answered", "note": "x"}], None, ""),
    ("old_answer_on_reopen", [{"id": "a", "status": "answered", "note": "x"}], ["a"], ""),
    ("still_streaming", [{"id": "b", "status": "reading", "note": "half"}], [], ""),
    ("landed", [{"id": "a", "status": "answered", "note": "old"},
                {"id": "b", "status": "answered",
                 "note": "Open [Git connections](/connections)."}],
     ["a"], "Open Git connections."),
    ("failed", [{"id": "b", "status": "failed", "note": "", "error": "boom"}], [], "boom"),
]


def test_only_a_reply_that_landed_while_open_is_announced(tmp_path):
    src = WIDGET.read_text(encoding="utf-8")
    lifted = _lift("replyToAnnounce", src, WIDGET)
    cases = [[c[1], c[2]] for c in ANNOUNCE]
    got = _compile_and_run(
        tmp_path, {},
        "type Run = any;\n" + lifted + "\n\n"
        f"const cases: any[] = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(([t, s]) => "
        "replyToAnnounce(t, s === null ? null : new Set(s)))));\n",
    )
    assert dict(zip([c[0] for c in ANNOUNCE], got, strict=True)) == {
        c[0]: c[3] for c in ANNOUNCE}


def test_the_transcript_is_not_a_live_region():
    """One small status element is live; the 400 ms-polled transcript is not."""
    src = WIDGET.read_text(encoding="utf-8")
    assert src.count('aria-live="polite"') == 1
    status = src.index('role="status" aria-live="polite"')
    assert "{announcement}" in src[status:status + 200]


def test_every_way_out_of_the_panel_returns_focus():
    src = WIDGET.read_text(encoding="utf-8")
    assert "setOpen(false)" not in src.replace(
        "  setOpen(false);\n  if (inside) launcher?.focus();", "")
    assert src.count("onClick={onClose}") == 2  # the X and "full view"
    assert "ref={launcher}" in src
