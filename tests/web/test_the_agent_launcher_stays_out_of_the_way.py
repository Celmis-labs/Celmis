"""The floating agent is on every page, so what it covers and what it carries
from one sitting to the next are pinned here.

IT DOES NOT SIT ON A SAVE BUTTON. The launcher is fixed 16px in from the
bottom-right corner. A sticky bottom bar puts its right-hand control (Save, on
the policy and agent pages the guide sends people to) in that same corner, so
every such bar either lives on a route where the launcher is hidden or pads
itself clear with `clear-agent-launcher`.

A NOTE IS MARKDOWN ONLY WHEN IT WAS WRITTEN AS MARKDOWN. A plan's one-liner
that echoes `services/*` back is shown verbatim; the half-written answer has
no pressable links, because the server has not cleaned it yet.

NOTHING OUTLIVES THE PERSON OR THE WORKSPACE. Every sign-out and every
workspace switch forgets the agent's session id and the panel's open state.

THE COLLAPSED SIDEBAR SURVIVES THE MOVE TO A COOKIE. A browser with no cookie
reads the old localStorage value once.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
APP = WEB / "app" / "(app)"
WIDGET = (WEB / "components" / "agent-widget.tsx").read_text(encoding="utf-8")
THREAD = (WEB / "components" / "automation" / "thread.tsx").read_text(encoding="utf-8")
SHELL = (WEB / "components" / "app-shell.tsx").read_text(encoding="utf-8")
LAYOUT = (APP / "layout.tsx").read_text(encoding="utf-8")
CSS = (WEB / "app" / "globals.css").read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """Comments here name the thing they explain; a guard must not pass on
    the comment alone."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def _hidden_on() -> list[re.Pattern[str]]:
    block = WIDGET[WIDGET.index("const HIDDEN_ON"):]
    block = block[:block.index("];")]
    return [re.compile(src.replace(r"\/", "/"))
            for src in re.findall(r"^\s*/(.+)/,\s*$", block, flags=re.M)]


def _route_of(page: Path) -> str:
    parts = page.relative_to(APP).parent.parts
    return "/" + "/".join(p.strip("[]") for p in parts)


# ─── the launcher and sticky bars ────────────────────────────────────


def test_hidden_on_is_parsed():
    pats = _hidden_on()
    assert len(pats) >= 3, "HIDDEN_ON changed shape; this guard reads nothing"
    assert any(p.search("/automation") for p in pats)


def test_the_clearance_utility_exists():
    assert re.search(r"@utility clear-agent-launcher\s*\{[^}]*padding-right",
                     _strip_comments(CSS))


def test_no_sticky_bottom_bar_sits_under_the_launcher():
    hidden = _hidden_on()
    offenders = []
    for page in APP.rglob("*.tsx"):
        body = _strip_comments(page.read_text(encoding="utf-8"))
        for cls in re.findall(r'className="([^"]*\bsticky\b[^"]*\bbottom-0\b[^"]*)"', body):
            route = _route_of(page)
            if "clear-agent-launcher" in cls.split():
                continue
            if any(p.search(route) for p in hidden):
                continue
            offenders.append(f"{route} ({page.name})")
    assert not offenders, f"sticky bottom bar under the agent launcher: {offenders}"


def test_the_pages_the_guide_sends_people_to_keep_the_launcher():
    hidden = _hidden_on()
    for route in ("/admin/review-policies/default", "/admin/agents/security"):
        assert not any(p.search(route) for p in hidden), route


# ─── how a note is rendered ──────────────────────────────────────────


def test_only_a_markdown_note_is_parsed_as_markdown():
    body = _strip_comments(THREAD)
    note = body[body.index("export function NoteText"):]
    note = note[:note.index("function GuideLinks")]
    assert "if (!markdown)" in note, "every note is parsed as markdown again"
    assert 'whitespace-pre-wrap">{text}' in note, "the plain note is not verbatim"
    assert re.search(r'action === "help"', body), "a help answer is not recognised"
    for call in re.findall(r"<NoteText\b[^>]*/>", body):
        assert "markdown=" in call, f"NoteText without a markdown decision: {call}"


def test_a_half_written_answer_has_no_live_links():
    body = _strip_comments(THREAD)
    note = body[body.index("export function NoteText"):]
    a = note[note.index("a: ({ href, children })"):]
    a = a[:a.index("<Link")]
    assert "streaming ?" in a, "a streaming link is pressable before the server cleaned it"


# ─── what is forgotten ───────────────────────────────────────────────


def _sources_with_sign_out() -> list[Path]:
    out = subprocess.run(
        ["git", "grep", "-l", "signOut(", "--", "web/app", "web/components", "web/lib"],
        cwd=ROOT, capture_output=True, text=True, check=False).stdout
    return [ROOT / p for p in out.split()]


def test_every_sign_out_forgets_the_agent():
    files = _sources_with_sign_out()
    assert files, "no signOut call found; this guard reads nothing"
    for f in files:
        body = _strip_comments(f.read_text(encoding="utf-8"))
        for m in re.finditer(r"\bsignOut\(", body):
            before = body[max(0, m.start() - 120):m.start()]
            assert "forgetAgentSession()" in before, (
                f"{f.relative_to(ROOT)}: signOut without forgetting the agent session")


def test_a_workspace_switch_forgets_the_agent():
    body = _strip_comments(SHELL)
    switch = body[body.index("const switchWs"):]
    switch = switch[:switch.index("location.reload()")]
    assert "forgetAgentSession()" in switch


def test_the_keys_are_spelled_once():
    for src in (THREAD, WIDGET):
        body = _strip_comments(src)
        assert '"automation_session_id"' not in body
        assert '"celmis:agent-widget"' not in body


# ─── the sidebar migration ───────────────────────────────────────────


def test_a_browser_without_the_cookie_reads_the_old_setting_once():
    assert "sidebarRemembered={sidebar !== undefined}" in _strip_comments(LAYOUT)
    body = _strip_comments(SHELL)
    assert "localStorage.getItem(LEGACY_SIDEBAR_KEY)" in body
    assert "localStorage.removeItem(LEGACY_SIDEBAR_KEY)" in body
    assert "localStorage.setItem(\"celmis:sidebar\"" not in body, "dead write is back"
