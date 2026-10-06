"""The token instructions that ship in the registry manifest, the agent skill and
the access guide must describe the command the product has.

`analyzer mcp issue-token` once took `--scopes` and `--duration`; per-person
grants replaced them with `--user --workspace --repos --days`. The manifest
(server.json) and the skill kept teaching the old flags, so a person following
them got a usage error, and the guide said refused tokens get 401 while the
server answers 403 with the reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_JSON = ROOT / "server.json"
SKILL = ROOT / ".claude" / "skills" / "celmis-mcp" / "SKILL.md"
ACCESS = ROOT / "docs" / "mcp-access.md"
REMOVED_FLAGS = ("--scopes", "--duration", "--subject")


def _cli_flags() -> set[str]:
    from typer.main import get_command

    from src.cli import mcp_app

    cmd = get_command(mcp_app).commands["issue-token"]
    return {opt for p in cmd.params for opt in p.opts}


def test_the_command_has_the_flags_the_docs_use_and_not_the_removed_ones():
    flags = _cli_flags()
    assert {"--user", "--workspace", "--repos", "--days"} <= flags
    assert not set(REMOVED_FLAGS) & flags


def test_the_manifest_teaches_the_current_command():
    manifest = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    headers = manifest["packages"][0]["transport"]["headers"]
    text = " ".join(h["description"] for h in headers)
    assert "issue-token" in text
    for flag in ("--user", "--workspace", "--repos", "--days"):
        assert flag in text
    for flag in REMOVED_FLAGS:
        assert flag not in text


def test_the_manifest_names_the_version_the_package_has():
    """Bumping `src.__version__` without the manifest ships a registry entry
    pointing at the previous image."""
    from src import __version__

    manifest = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    image = manifest["packages"][0]["identifier"]
    assert image.endswith(f":v{__version__}")


def test_the_skill_does_not_teach_the_removed_token_flow():
    body = SKILL.read_text(encoding="utf-8")
    for flag in REMOVED_FLAGS:
        assert flag not in body, f"the skill still shows {flag}"
    assert "issue-token" in body and "--repos" in body
    assert "expired (default lifetime 1 hour)" not in body


def test_a_refused_token_is_documented_as_403_with_a_reason():
    for path in (ACCESS, SKILL, ROOT / "CHANGELOG.md"):
        body = path.read_text(encoding="utf-8")
        assert not re.search(r"predates per-person\s+grants[^.]{0,40}\b401", body)
    assert re.search(r"refused with 403", ACCESS.read_text(encoding="utf-8"))
