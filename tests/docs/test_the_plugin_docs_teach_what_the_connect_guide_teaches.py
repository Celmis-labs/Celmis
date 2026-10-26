"""The plugin README and `docs/MCP_DEV.md` say what the Connect page says.

The page was rewritten from a real first connection, where each of these went wrong: the
token came from the superadmin only, a token typed inside `${...}` was shown back, Discover
found nothing before the marketplace was added, installing from a terminal asked for
nothing, a clipboard one-liner saved the wrong text, a refused token showed as a JSON parse
error with an Authenticate button, shared repositories needed a tick on the token, and in
VS Code the menu is in the terminal and a restart is a reload. A reader of the README or of
the developer notes meets the same people, so the same facts have to be there, and no
document may recommend a shell command that holds or reads the token.

A review of the page added what the first version missed: a token made with "Allow changes"
ticked answers 403 with the plugin, a person revokes an exposed token themselves, only the
installing from a terminal was seen not to ask (the menu may not either), and the table of
symptoms is one table in the README, not three copies.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "packaging" / "claude-plugin" / "celmis-code" / "README.md"
DEV = ROOT / "docs" / "MCP_DEV.md"
CHANGELOG = ROOT / "CHANGELOG.md"

CONFIGURE = "/plugin configure celmis-code@celmis"
MARKETPLACE = "/plugin marketplace add Celmis-labs/Celmis"
INSTALL = "/plugin install celmis-code@celmis"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _plugin_section() -> str:
    text = _flat(DEV)
    start = text.index("## Use it from Claude Code (plugin)")
    return text[start:text.index("## \"Do it like service X\"", start)]


def _install_section() -> str:
    text = _flat(README)
    return text[text.index("## Install"):text.index("### Upgrading from 3.0.1")]


def _troubleshooting() -> str:
    text = _flat(README)
    return text[text.index("## Troubleshooting"):]


def _fences(path: Path) -> list[str]:
    """The text of every fenced block of a document."""
    lines, blocks, cur = path.read_text(encoding="utf-8").splitlines(), [], None
    for line in lines:
        if line.strip().startswith("```"):
            if cur is None:
                cur = []
            else:
                blocks.append("\n".join(cur))
                cur = None
        elif cur is not None:
            cur.append(line.strip())
    return blocks


def test_both_documents_install_in_both_places_and_add_the_marketplace_first() -> None:
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        for command in (MARKETPLACE, INSTALL, "claude plugin marketplace add Celmis-labs/Celmis",
                        "claude plugin install celmis-code@celmis"):
            assert command in text, f"{name} lost: {command}"
        assert text.index(MARKETPLACE) < text.index(INSTALL), f"{name}: the marketplace comes first"
        assert "Discover" in text and "celm" in text, f"{name}: the Discover note"


def test_both_documents_say_that_installing_does_not_ask_and_what_does() -> None:
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        assert CONFIGURE in text, name
        assert re.search(r"install(ing)?( itself| from a terminal)? does not ask", text, re.I), name
        assert "masked" in text, name
        assert "claude plugin configure celmis-code@celmis" in text, name
    for text in (_plugin_section(), _install_section()):
        assert "inside claude code" in text.lower()


def test_the_token_comes_from_the_superadmin_with_the_share_ticked() -> None:
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        assert "self-service" in text and "superadmin" in text, name
        assert "Allow write tools" in text and "read-only" in text, name
        assert "Shared with us" in text and "30 seconds" in text, name


def test_a_token_that_can_change_things_is_said_not_to_work_with_the_plugin() -> None:
    # `mcp.writeScopes` on the settings page: a write-scope token gets the full profile and
    # no `read:code`, so the plugin's endpoint answers 403 for the first person who ticks it
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        assert "Allow changes" in text, name
        assert re.search(r"does not work with the plugin", text), name
    assert "Allow changes" in _troubleshooting()


def test_a_person_revokes_an_exposed_token_themselves() -> None:
    # `GET /api/mcp/tokens/me` and `POST /api/mcp/tokens/{id}/revoke` do not wait for
    # an administrator; waiting leaves an exposed token valid
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        assert re.search(r"revoke it yourself", text), name
    assert "revoke it yourself" in _troubleshooting()


def test_neither_document_says_the_menu_does_not_ask() -> None:
    # only installing from a terminal was seen not to ask (Claude Code 2.1.309); for the
    # menu it is not known, so no document asserts it
    for path in (README, DEV):
        text = _flat(path)
        assert "or from the menu" not in text, path.name
        assert "from a terminal or from the" not in text, path.name
        assert "Installing never asks" not in text, path.name


def test_every_command_of_the_install_is_a_block_of_its_own() -> None:
    # "one at a time" means one to a box: Copy on a box of two lines pastes both, the
    # multi-line paste that put a line of a command in the token field
    for path in (README, DEV):
        for block in _fences(path):
            commands = [line for line in block.splitlines()
                        if line.startswith(("/plugin ", "claude plugin "))]
            assert len(commands) <= 1, f"{path.name}: two commands in one block: {commands}"


def test_the_documents_say_what_not_to_do_with_the_token() -> None:
    for name, text in (("README", _install_section()), ("MCP_DEV", _plugin_section())):
        assert "bad substitution" in text, name
        assert "${...}" in text, name
        assert "claude mcp add" in text, name
        assert "two Celmis servers" in text, name
        assert "history" in text, name
        assert "revoked" in text, name


def test_neither_document_recommends_a_shell_command_that_reads_the_token() -> None:
    # `--values-stdin` stays, for provisioning scripts: the token comes from a secret
    # store, never from the clipboard or a literal. The one-liner for a person is gone.
    for path in (README, DEV):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "plugin configure" in line and "pbpaste" in line:
                raise AssertionError(f"{path.name}: a clipboard one-liner for the token: {line}")
            assert "read -rs" not in line and "read -s" not in line, (path.name, line)
    assert "provisioning" in _flat(README)
    assert "--values-stdin" in _flat(README) and "--values-stdin" in _flat(DEV)


def test_the_readme_troubleshooting_table_has_no_blank_line_in_it() -> None:
    # a blank line ends a table: every row after it shows as raw text with its pipes
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Troubleshooting"))
    rows = [i for i, line in enumerate(lines) if i > start and line.startswith("|")]
    assert len(rows) > 12 and rows[0] == start + 2, "the table moved"
    blank = [i + 1 for i in range(rows[0], rows[-1]) if not lines[i].strip()]
    assert not blank, f"blank line(s) inside the troubleshooting table: {blank}"
    # and the separator row comes right after the header
    assert lines[rows[0] + 1].startswith("|---"), lines[rows[0] + 1]


def test_the_readme_troubleshooting_has_every_symptom_of_the_guide() -> None:
    text = _troubleshooting()
    for needle in (
        "bad substitution",
        "Discover finds nothing",
        "without asking for the address and the token",
        "Failed to save configuration: Celmis token is required but not provided",
        "plugin:celmis-code:celmis",
        "JSON Parse error: Unrecognized token '<'",
        "Authenticate",
        "Reconnect",
        "read:code",
        "Shared with us",
        "repo not found or not accessible",
        "Developer: Reload Window",
        "HTTP 401",
        "HTTP 404",
        "command not found: claude",
        "Server rejected the configured Authorization header (HTTP 401)",
        "membership of the workspace",
    ):
        assert needle in text, f"the README's troubleshooting lost: {needle}"
    assert "Do not press Authenticate" in text


def test_the_developer_notes_point_to_the_readme_table_instead_of_copying_it() -> None:
    # three copies of the table (the page, the README, these notes) drift; two are enough
    text = _plugin_section()
    assert "packaging/claude-plugin/celmis-code/README.md#troubleshooting" in text
    assert "| You see |" not in text and "|---|" not in text
    for needle in ("bad substitution", "Developer: Reload Window", "Authenticate"):
        assert needle in text, f"MCP_DEV lost: {needle}"
    target = DEV.parent / "../packaging/claude-plugin/celmis-code/README.md"
    assert target.resolve().is_file(), "the link in MCP_DEV points nowhere"


def test_no_document_names_a_real_host_or_company() -> None:
    # the repository is public: addresses in the examples are the reserved example domain
    for path in (README, DEV, CHANGELOG):
        text = path.read_text(encoding="utf-8")
        hosts = set(re.findall(r"https://([a-z0-9.-]+)", text))
        for host in hosts:
            if path is CHANGELOG:
                continue
            allowed = host.endswith("example.com") or host in {
                "github.com", "keepachangelog.com", "semver.org", "claude.ai", "code.claude.com",
                "docs.claude.com", "docs.anthropic.com", "bitbucket.org", "api.github.com",
            } or host.startswith("localhost")
            assert allowed, f"{path.name} names a host that may be real: {host}"


def test_the_changelog_records_the_guide_in_its_release() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    unreleased = text[text.index("## [3.1.3]"):text.index("## [3.1.1]")]
    assert "Connect" in unreleased and "/plugin configure celmis-code@celmis" in unreleased
    assert "CELMIS_MCP_TOKEN" in unreleased
