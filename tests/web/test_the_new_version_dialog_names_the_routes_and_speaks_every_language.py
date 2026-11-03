"""The upload-new-version dialog calls routes the server has and has words in every language."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MESSAGES = ROOT / "web" / "lib" / "i18n" / "messages"


def test_the_client_names_the_routes_the_router_serves():
    api = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
    router = (ROOT / "src" / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    for served, client in (
        ('"/{slug}/versions/sessions"', "/versions`"),
        ('"/{slug}/versions/sessions/{session_id}/complete"', "/complete`"),
        ('"/{slug}/versions/updates/{update_id}"', "/versions/updates/"),
        ('"/{slug}/versions"', "/versions`"),
    ):
        assert served in router and client in api, served


def test_every_key_the_dialog_uses_exists_in_every_locale_and_uk_is_ukrainian():
    src = (ROOT / "web" / "components" / "repo-versions.tsx").read_text(encoding="utf-8")
    import re

    used = set(re.findall(r't\("(repoVersions\.[A-Za-z.]+)"', src))
    used |= {f"repoVersions.phase.{p}" for p in (
        "uploading", "queued", "starting", "unpacking", "comparing", "indexing",
        "swapping", "cleaning", "working")}
    assert len(used) >= 15
    for f in MESSAGES.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        assert not used - data.keys(), (f.name, sorted(used - data.keys()))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    for key in used:
        assert uk[key] != en[key] and any("а" <= c.lower() <= "я" or c in "іїєґ" for c in uk[key]), key
