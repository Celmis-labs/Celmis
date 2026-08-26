"""Sixteen locales, and a sentence typed straight into JSX reaches none of them.

This is not a style rule. The product ships in sixteen languages, and a string
written inline is English for everybody — silently, with no missing-key warning
and nothing for `test_locale_has_every_key` to catch, because the key was never
created.

It happens in exactly one situation: a control added in a hurry. The agent
session composer arrived that way — placeholder, paperclip tooltip, "Finish &
push", and three toasts, all inline, on a page whose other nine strings all
went through `t()`.

What is deliberately allowed is a FORMAT EXAMPLE — `ghp_… / glpat-…`,
`github:owner/repo#42`, `http://qdrant:6333`. Translating those makes them
wrong: they show the literal shape of something the user has to type.

IT SCANS `web/components` TOO, AND THAT IS NOT AN EXTRA. The copy-a-command
box and the local-model setup guide were written inside the settings page and
were checked here as part of it; the day a second surface needed them they
moved into `web/components` unchanged, and left the guard's reach without a
single line of them being edited. A scan bounded by one directory always ends
that way — the strings do not have to change to escape it, only the file they
sit in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
APP = WEB / "app"
COMPONENTS = WEB / "components"

#: A string is a format example, not prose, if it is mostly punctuation and
#: identifiers: a URL, a token prefix, a slug, a list of alternatives.
_EXAMPLE = re.compile(
    r"^[^ ]*$"                       # one token, no spaces
    r"|https?://"                    # a URL
    r"|^[\w.\-]+ */ *[\w.\-]+"       # owner / repo, a / b
    r"|\.\.\."                       # an elision showing a shape
    r"|[{}\[\]<>|]"                  # a template or a pattern
    r"|^[A-Za-z0-9_\-]+(, ?[A-Za-z0-9_\-]+)+$"  # get_issue, list_issues
)

#: Where a real sentence would be, if somebody typed one.
_ATTRS = re.compile(r'(?:placeholder|title|aria-label)="([^"]{6,})"')
_TOASTS = re.compile(r'toast\.(?:info|success|error|warning)\("([^"]{6,})"')


def _strings(path: Path) -> list[str]:
    src = path.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    found = _ATTRS.findall(src) + _TOASTS.findall(src)
    return [s for s in found if " " in s and not _EXAMPLE.search(s)]


#: Everything a person reads, wherever it happens to live this week.
RENDERED = sorted(APP.rglob("*.tsx")) + sorted(COMPONENTS.rglob("*.tsx"))


def test_there_are_files_to_check():
    """Guards the guard: a bad glob makes every case below vacuous. Both roots
    are counted, because a typo in the second one would silently restore the
    single-directory scan this test was widened out of."""
    assert len(list(APP.rglob("*.tsx"))) > 10
    assert len(list(COMPONENTS.rglob("*.tsx"))) > 10


@pytest.mark.parametrize("rendered", RENDERED, ids=lambda p: str(p.relative_to(WEB)))
def test_nothing_rendered_hardcodes_a_sentence(rendered: Path):
    inline = _strings(rendered)
    assert not inline, (
        f"{rendered.relative_to(WEB)} shows these in English to all sixteen "
        f"locales: {inline}"
    )


def test_the_example_exemption_still_lets_prose_through():
    """An allow-list that allows everything is the usual way a guard like this
    stops working."""
    assert _EXAMPLE.search("https://xyz.cloud.qdrant.io  /  http://qdrant:6333")
    assert _EXAMPLE.search("ghp_... / glpat-...")
    assert _EXAMPLE.search("get_issue, list_issues")
    for prose in ("Every repository already has an index.",
                  "Attach a text file so the agent can read it",
                  "Wrapping up — pushing what was done."):
        assert not _EXAMPLE.search(prose), prose


def test_the_session_composer_speaks_every_language():
    """The control this test was written for."""
    page = APP / "(app)" / "claude" / "[id]" / "page.tsx"
    src = page.read_text()
    for key in ("claude.composerPlaceholder", "claude.attachHint",
                "claude.finish", "claude.finishing", "claude.send",
                "claude.attached"):
        assert key in src, f"{key} is not used"


def test_the_send_button_has_a_name():
    """It is an icon alone. Without a label a screen reader announces
    "button", and a tooltip is the only thing that says Enter is not the only
    way to send."""
    src = (APP / "(app)" / "claude" / "[id]" / "page.tsx").read_text()
    # The button, not the textarea's Enter handler above it — both call
    # sendDraft, and the first match is the wrong one.
    button = src[src.rfind("<Button", 0, src.rfind("void sendDraft()")):]
    assert "aria-label" in button[:600]
