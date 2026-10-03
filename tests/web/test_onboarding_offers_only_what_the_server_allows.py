"""Onboarding step 1 must not offer a Create button the server refuses.

POST /api/workspaces is superadmin-only. The admin/workspaces page and the
switcher were gated; the onboarding wizard was missed, so a user left with
only the "default" workspace got a name field, a button, and a bare 403.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
ONBOARDING = (WEB / "app" / "(app)" / "onboarding" / "page.tsx").read_text()
MESSAGES = WEB / "lib" / "i18n" / "messages"
KEY = "onboarding.ws.askSuperadmin"


def _strip_comments(source: str) -> str:
    """Comments name what they guard; grepping them proves nothing."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def _workspace_step() -> str:
    body = _strip_comments(ONBOARDING)
    start = body.find("function WorkspaceStep(")
    assert start > 0, "WorkspaceStep is gone"
    return body[start:body.find("\nfunction ", start + 1)]


def test_the_create_form_renders_only_for_the_superadmin():
    step = _workspace_step()
    assert "session?.isSuperadmin" in step
    form = step.find("workspacesApi.create")
    assert form > 0
    # The form's JSX block is guarded by the flag.
    assert re.search(r"\{!done && isSuperadmin && \(\s*<div", step), (
        "the create form is drawn without checking isSuperadmin")


def test_everyone_else_is_told_what_to_do_instead():
    step = _workspace_step()
    assert re.search(r"\{!done && !isSuperadmin && \(", step)
    assert f't("{KEY}")' in step


def test_the_hint_exists_in_every_locale():
    for path in sorted(MESSAGES.glob("*.json")):
        msgs = json.loads(path.read_text(encoding="utf-8"))
        assert msgs.get(KEY), f"{path.name} lacks {KEY}"
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))[KEY]
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))[KEY]
    assert uk != en, "the Ukrainian hint is still English"
