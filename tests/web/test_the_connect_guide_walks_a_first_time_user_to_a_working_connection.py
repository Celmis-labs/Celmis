"""The Connect page is a guide a person who has never connected an agent follows to the end.

It is written from what went wrong when a real person connected the Claude Code plugin to
a production install, and each thing that went wrong is pinned here so that a rewrite of
the page cannot quietly bring it back:

* the token step said "create one on the MCP settings page" on an install where only the
  superadmin issues tokens (self-service off): the step now follows the install's own
  answer and, when a person must ask, gives the request with the workspace filled in;
* a token was typed into `claude mcp add ... ${<token>}` ("bad substitution"), where it
  stayed on screen and in the shell history: no sentence shows a shell command that
  holds or reads the token, and the "do not" list says why;
* Discover found nothing for "celm" because the marketplace was not added: both ways of
  adding it are on the page, and so is the note;
* installing from a terminal does not ask for the address and token: the page sends the
  person to `/plugin configure` (a masked field), and says how to see what is saved;
* a multi-line paste put a line of the command in the token field: one command per box,
  and the troubleshooting says what that looks like;
* a refused token shows as `JSON Parse error: Unrecognized token '<'` and an
  Authenticate button, which cannot help: the page says what it means and what to do.

A review of the finished page added: a token made with "Allow changes" ticked answers 403
with the plugin; the owner revokes an exposed token themselves; the by-hand route fills its
variable with a line that does not echo (`read -rs`, the one shell line that reads a token,
in a box of its own); the page must not guess when the server cannot say who issues tokens;
the address comes from a store so the server and the browser render the same first frame
(a mismatch rebuilt the tree and lost the theme class); the Plugin / By hand switch keeps
its width; the commands inside the answers are shown as code; installing from a terminal
is the only way seen not to ask.

Text assertions, like the other tests of this page: the page is a browser component and a
test that needs a browser is the test that does not run. The decisions it makes are read
from the same source the page reads them from.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    WEB,
    _strip_comments,
)
from tests.web.test_the_codebase_pages_speak_every_language import _array

APP = WEB / "app" / "(app)"
PAGE = APP / "codebase" / "connect" / "page.tsx"
SETTINGS = APP / "settings" / "mcp" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"
ROOT = WEB.parent
PLUGIN = ROOT / "packaging" / "claude-plugin" / "celmis-code"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

P = "codebase.connect."
LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))

#: Troubleshooting entries, by what the person sees: every failure of the list above plus
#: the three the server's own answers give (403, a missing share, a name it cannot find).
TROUBLES = {"substitution", "discover", "noserver", "saved", "failed",
            "forbidden", "shared", "notfound", "unreachable"}

#: Symptoms that ARE a program's English output, quoted so the line can be found.
VERBATIM_SYMPTOMS = {P + "trouble.saved.symptom", P + "trouble.notfound.symptom"}


def _code(path=PAGE) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _messages(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))


EN = _messages("en")


def _commands() -> dict[str, str]:
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    repo = plugin["repository"].removeprefix("https://github.com/")
    ref = f"{plugin['name']}@{market['name']}"
    return {
        "mkt": f"/plugin marketplace add {repo}",
        "install": f"/plugin install {ref}",
        "configure": f"/plugin configure {ref}",
        "cli_mkt": f"claude plugin marketplace add {repo}",
        "cli_install": f"claude plugin install {ref}",
        "cli_configure": f"claude plugin configure {ref}",
    }


# ─── the commands ────────────────────────────────────────────────────


def test_the_page_shows_both_ways_of_installing_and_of_entering_the_values() -> None:
    code = _code()
    for name, command in _commands().items():
        assert f'"{command}"' in code, f"the page does not show ({name}): {command}"
    # every command appears in its own box, one per box: a paste of two lines is how a
    # line of the command ended up in the token field
    assert "\\n" not in "".join(re.findall(r'"((?:/plugin|claude plugin) [^"]*)"', code))


def test_the_marketplace_is_the_repository_the_manifest_names() -> None:
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["repository"].endswith("/Celmis-labs/Celmis")
    assert _commands()["mkt"] == "/plugin marketplace add Celmis-labs/Celmis"


def test_the_guide_has_the_steps_in_the_order_they_have_to_be_done() -> None:
    code = _code()
    order = [code.index(marker) for marker in (
        "codebase.connect.step1.title", "codebase.connect.step2.title",
        "codebase.connect.enter.title", "codebase.connect.check.title",
        "codebase.connect.step3.title", "codebase.connect.trouble.title")]
    assert order == sorted(order), "token, install, enter, restart and check, use it, troubleshooting"


# ─── the token step ──────────────────────────────────────────────────


def test_the_token_step_asks_the_install_who_issues_tokens() -> None:
    code, settings = _code(), _code(SETTINGS)
    # the same question, the same cache entry, as the page that issues tokens
    assert 'queryKey: ["mcp-tokens-me"]' in code and 'queryKey: ["mcp-tokens-me"]' in settings
    assert "mcpTokensApi.mine(" in code
    assert "self_service_enabled === true" in code and "self_service_enabled === true" in settings
    assert 'href="/settings/mcp"' in code, "with self-service on the link to token settings stays"
    # the answer is not claimed before the server gives it
    assert "mine.isPending" in code


def test_without_self_service_the_page_gives_a_request_with_the_workspace_filled_in() -> None:
    code = _code()
    assert "workspacesApi.me(" in code and "active_id" in code
    assert re.search(r't\("codebase\.connect\.step1\.request",\s*\{\s*workspace:', code)
    assert "{workspace}" in EN[P + "step1.request"]
    ask = EN[P + "step1.request"]
    for wanted in ("developer", "read-only", "Repositories", "Shared with us"):
        assert wanted in ask, f"the request does not ask for: {wanted}"
    assert "privately" in ask


def test_when_the_server_cannot_say_who_issues_tokens_the_page_does_not_guess() -> None:
    code = _code()
    # an error is not "tokens are issued by your administrator": a person on an install with
    # self-service would be sent to ask for what they can make themselves
    assert "mine.isError" in code
    assert '"codebase.connect.step1.descUnknown"' in code
    unknown = EN[P + "step1.descUnknown"]
    assert "token settings" in unknown and "administrator" in unknown and "below" in unknown
    assert "administrator issues" not in unknown
    # the request names the workspace, so it waits for the workspace's name; and the
    # superadmin's view waits for the session, not the other way round
    assert "workspaces.isPending" in code and "sessionStatus" in code
    for locale in LOCALES:
        assert _messages(locale)[P + "step1.descUnknown"].strip(), locale


def test_a_superadmin_is_told_how_to_issue_one() -> None:
    code = _code()
    assert "session?.isSuperadmin" in code
    assert 'href="/admin/mcp-tokens"' in code
    hint = EN[P + "step1.superadmin"]
    for wanted in ("Allow write tools", "read-only", "Shared with us", "30 seconds"):
        assert wanted in hint, wanted
    # the labels are the admin page's own, in every language
    for locale in LOCALES:
        mine = _messages(locale)
        assert mine["mcpTokens.allowWrite"] in mine[P + "step1.superadmin"], locale
        assert mine["mcpTokens.shared.title"] in mine[P + "step1.superadmin"], locale
        assert mine["mcpTokens.shared.title"] in mine[P + "step1.request"], locale
        assert mine["mcpTokens.shared.title"] in mine[P + "trouble.shared.fix"], locale


# ─── nothing puts a token in a shell ─────────────────────────────────


def test_no_sentence_shows_a_shell_command_that_holds_or_reads_the_token() -> None:
    code = _code()
    banned = ("pbpaste", "xclip", "--values-stdin", "echo $")
    for text in [code] + [v for k, v in EN.items() if k.startswith(P)]:
        for word in banned:
            assert word not in text, f"shows a command that touches the token: {word}"
    # no sentence of any language carries a shell line that reads or exports a value
    for locale in LOCALES:
        for key, text in _messages(locale).items():
            if key.startswith(P):
                for word in ("read -s", "read -rs", "export CELMIS", "export "):
                    assert word not in text, (locale, key, word)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}", code)


def test_the_one_shell_line_that_reads_a_token_is_the_manual_routes_and_does_not_echo() -> None:
    """The by-hand route cannot avoid a shell variable, so it gets the safe way to fill one:
    `read -s` takes the token without echoing it (not on screen, not in the history), in a
    box of its own, run alone. The plugin route has no such line."""
    for path in (PAGE, SETTINGS):
        code = _code(path)
        line = 'READ_TOKEN = "read -rs CELMIS_MCP_TOKEN && export CELMIS_MCP_TOKEN"'
        assert line in code, path.name
        assert "\n" not in line
        # it exports only the variable the command names, and nothing else is read
        assert set(re.findall(r"export (CELMIS\w+)", code)) == {"CELMIS_MCP_TOKEN"}, path.name
        assert len(re.findall(r"\bread -", code)) == 1, path.name
    code = _code()
    # on this page it is part of the manual route only
    manual = code.index("codebase.connect.manual.title")
    uses = [m.start() for m in re.finditer(r"text=\{READ_TOKEN\}", code)]
    assert len(uses) == 1 and uses[0] > manual
    assert code.index("codebase.connect.manual.prepare") < uses[0] < code.index("text={manualCommand}")
    prepare = EN[P + "manual.prepare"]
    assert "on its own" in prepare and "Enter" in prepare and "same window" in prepare


def test_the_dollar_brace_form_is_only_ever_named_as_what_not_to_do() -> None:
    uses = sorted(k for k, v in EN.items() if k.startswith(P) and "${" in v)
    assert uses == [P + "dont.shell", P + "manual.note", P + "trouble.substitution.meaning"]
    assert EN[P + "dont.shell"].startswith("Do not")
    assert "do not" in EN[P + "manual.note"]


def test_the_do_not_list_covers_the_four_mistakes_that_happened() -> None:
    assert _array(PAGE, "DONTS") == ["shell", "multi", "both", "chat"]
    en = {k: EN[P + f"dont.{k}"] for k in ("shell", "multi", "both", "chat")}
    assert "bad substitution" in en["shell"] and "history" in en["shell"]
    assert "several lines" in en["multi"]
    assert "claude mcp add" in en["both"]
    assert "revoke it yourself" in en["chat"] and "chat" in en["chat"] and "screenshot" in en["chat"]


def test_the_manual_route_is_marked_as_not_to_be_combined_with_the_plugin() -> None:
    assert "recommended" in EN[P + "route.plugin"]
    assert "one route only" in EN[P + "route.hint"] and "two Celmis servers" in EN[P + "route.hint"]
    note = EN[P + "manual.note"]
    assert "CELMIS_MCP_TOKEN" in note and "Never use both" in note
    # the shell expands the name before Claude Code sees it: the value is what gets saved
    assert "fills in the value before Claude Code sees" in note and "own config" in note
    assert "refers to it by name" not in note
    code = _code()
    assert 'Authorization: Bearer \\${CELMIS_MCP_TOKEN}' in code and "CELMIS_MCP_TOKEN" in code
    # the literal header-with-bearer template (a JS template string) stays out of the web
    # tree (tests/security); a shell line shown on screen is not one, and needs no detour
    assert not re.search(r"Authorization: `Bearer", code)
    assert "AUTH_HEADER" not in code


# ─── enter the values, restart, check ────────────────────────────────


def test_the_values_are_entered_with_the_masked_prompt_inside_claude_code() -> None:
    assert "masked" in EN[P + "enter.token"]
    assert "inside Claude Code" in EN[P + "enter.desc"]
    # seen not to ask when installing from a terminal (Claude Code 2.1.309); the /plugin menu
    # was not tried, so the page does not say it never asks
    desc = EN[P + "enter.desc"]
    assert "from a terminal does not ask" in desc and "may not either" in desc, desc
    assert "nothing asked" in desc
    verify = EN[P + "enter.verify"]
    assert '"set"' in verify and "does not check that the token is right" in verify


def test_the_restart_and_the_check_name_both_places_the_agent_runs() -> None:
    restart = EN[P + "check.restart"]
    assert "terminal" in restart and "VS Code" in restart and "Developer: Reload Window" in restart
    assert "/mcp" in EN[P + "check.mcp"] and "connected" in EN[P + "check.mcp"]
    assert _array(PAGE, "CHECK_PROMPTS") == ["repos", "skills", "brief"]
    code = _code()
    for prompt in ("call celmis repos", "call celmis skills", "call celmis brief"):
        assert f'"{prompt}' in code
    assert "shared" in EN[P + "check.prompt.repos"]


def test_the_use_it_step_and_the_tool_order_are_kept() -> None:
    assert _array(PAGE, "AGENT_STEPS") == ["brief", "skills", "find", "read_symbol"]
    assert 'href="/codebase/search"' in _code()


def test_the_discover_note_is_on_the_page_and_in_the_troubleshooting() -> None:
    note = EN[P + "plugin.discover"]
    assert "Discover" in note and "celm" in note and "marketplace" in note
    assert "Discover" in EN[P + "trouble.discover.symptom"]


# ─── troubleshooting ─────────────────────────────────────────────────


def test_every_failure_that_happened_has_a_symptom_a_meaning_and_a_remedy() -> None:
    assert set(_array(PAGE, "TROUBLES")) == TROUBLES
    for tid in TROUBLES:
        for part in ("symptom", "meaning", "fix"):
            assert EN[P + f"trouble.{tid}.{part}"].strip(), (tid, part)


def test_the_symptoms_are_worded_as_the_person_sees_them() -> None:
    sym = {tid: EN[P + f"trouble.{tid}.symptom"] for tid in TROUBLES}
    assert "bad substitution" in sym["substitution"]
    assert sym["saved"] == "Failed to save configuration: Celmis token is required but not provided"
    assert "plugin:celmis-code:celmis" in sym["failed"]
    assert "JSON Parse error: Unrecognized token '<'" in sym["failed"] and "Authenticate" in sym["failed"]
    assert "403" in sym["forbidden"] and "read:code" in sym["forbidden"]
    assert sym["notfound"] == "repo not found or not accessible"
    assert "repos" in sym["shared"]


def test_the_remedies_say_what_to_do() -> None:
    fix = {tid: EN[P + f"trouble.{tid}.fix"] for tid in TROUBLES}
    cmds = _commands()
    assert "revoke it yourself" in fix["substitution"]
    assert cmds["mkt"] in fix["discover"] and cmds["cli_mkt"] in fix["discover"] and cmds["install"] in fix["discover"]
    assert cmds["configure"] in fix["noserver"] and cmds["cli_configure"] in fix["noserver"]
    assert cmds["configure"] in fix["saved"] and "once" in fix["saved"]
    assert "Do not press Authenticate" in fix["failed"] and cmds["configure"] in fix["failed"]
    assert "Reconnect" in fix["failed"]
    assert "developer token" in fix["forbidden"]
    assert "Shared with us" in fix["shared"] and "30 seconds" in fix["shared"]
    assert "celmis repos" in fix["notfound"]
    assert cmds["configure"] in fix["unreachable"]
    # the owner revokes an exposed token at once; the settings page is one click away
    assert "revoke it yourself" in fix["substitution"]
    assert 'id === "substitution"' in _code() and _code().count('href="/settings/mcp"') >= 3
    # the page that says a token lists its shares comes before the administrator
    assert "MCP settings" in fix["shared"] and fix["shared"].index("settings") < fix["shared"].index("administrator")
    assert "make a new one" in fix["forbidden"] and "ask your administrator" in fix["forbidden"]
    assert "membership" in EN[P + "trouble.forbidden.meaning"] and "membership" in fix["forbidden"]
    # what the refused token means: the server said 401, not a broken page
    assert "401" in EN[P + "trouble.failed.meaning"]
    # both texts Claude Code shows for it, word for word, and that tokens expire
    assert "Server rejected the configured Authorization header (HTTP 401)" in EN[P + "trouble.failed.symptom"]
    assert "Tokens expire" in EN[P + "trouble.failed.meaning"]
    assert "For the plugin with a token" in EN[P + "trouble.failed.meaning"]
    assert "scope" in EN[P + "trouble.forbidden.meaning"] or "read:code" in EN[P + "trouble.forbidden.meaning"]
    # a token that can change things is the wrong kind for the plugin's endpoint
    assert "can change things" in EN[P + "trouble.forbidden.meaning"]
    # no `claude` on the path is a symptom people meet before any other
    assert 'command not found: claude' in EN[P + "plugin.terminalNote"]


# ─── the languages ───────────────────────────────────────────────────


@pytest.mark.parametrize("locale", LOCALES)
def test_every_language_keeps_the_commands_and_labels_of_claude_code_in_english(locale: str) -> None:
    mine, cmds = _messages(locale), _commands()
    assert cmds["mkt"] in mine[P + "trouble.discover.fix"]
    assert cmds["cli_mkt"] in mine[P + "trouble.discover.fix"]
    assert cmds["install"] in mine[P + "trouble.discover.fix"]
    assert cmds["configure"] in mine[P + "trouble.noserver.fix"]
    assert cmds["cli_configure"] in mine[P + "trouble.noserver.fix"]
    for tid in ("saved", "failed", "forbidden", "unreachable"):
        assert cmds["configure"] in mine[P + f"trouble.{tid}.fix"], (locale, tid)
    assert "Developer: Reload Window" in mine[P + "check.restart"]
    assert "Discover" in mine[P + "plugin.discover"] and "Discover" in mine[P + "trouble.discover.symptom"]
    failed = mine[P + "trouble.failed.symptom"]
    assert "JSON Parse error: Unrecognized token '<'" in failed and "Authenticate" in failed
    assert "Reconnect" in mine[P + "trouble.failed.fix"]
    assert "Authenticate" in mine[P + "trouble.failed.fix"]
    assert "bad substitution" in mine[P + "trouble.substitution.symptom"]
    assert "bad substitution" in mine[P + "dont.shell"]
    for key in (P + "dont.shell", P + "manual.note", P + "trouble.substitution.meaning"):
        assert "${...}" in mine[key], (locale, key)
    assert "CELMIS_MCP_TOKEN" in mine[P + "manual.note"]
    assert "claude mcp add" in mine[P + "dont.both"]
    for key in (P + "check.mcp",):
        assert "/mcp" in mine[key] and "connected" in mine[key]
    assert "shared" in mine[P + "check.prompt.repos"]
    assert "read:code" in mine[P + "trouble.forbidden.symptom"]
    assert '30' in mine[P + "trouble.shared.fix"] and '30' in mine[P + "step1.superadmin"]


def _label(mine: dict[str, str], key: str) -> str:
    """The label of a control as the person sees it, without its parenthesis."""
    return re.sub(r"\s*[(（].*$", "", mine[key]).strip()


@pytest.mark.parametrize("locale", LOCALES)
def test_every_language_quotes_the_labels_of_the_controls_it_points_to(locale: str) -> None:
    mine = _messages(locale)
    allow, revoke = _label(mine, "mcp.writeScopes"), _label(mine, "mcp.revoke")
    assert allow and revoke
    # a token that can change things does not work with the plugin: where the token is
    # made and where it is replaced, the switch is named as the person sees it
    assert allow in mine[P + "step1.desc"], locale
    assert allow in mine[P + "trouble.forbidden.fix"], locale
    # an exposed token is revoked by its owner at once
    assert revoke in mine[P + "dont.chat"], locale
    assert revoke in mine[P + "trouble.substitution.fix"], locale
    # the refused token, verbatim, whichever text Claude Code shows
    assert "Server rejected the configured Authorization header (HTTP 401)" in mine[P + "trouble.failed.symptom"]
    assert 'command not found: claude' in mine[P + "plugin.terminalNote"], locale


@pytest.mark.parametrize("locale", LOCALES)
def test_the_commands_in_the_answers_are_shown_as_code_in_every_language(locale: str) -> None:
    mine, cmds = _messages(locale), _commands()
    for tid in TROUBLES:
        for part in ("meaning", "fix"):
            text = mine[P + f"trouble.{tid}.{part}"]
            assert text.count("`") % 2 == 0, (locale, tid, part)
            for name, command in cmds.items():
                if command in text:
                    assert f"`{command}`" in text, (locale, tid, part, name)
    code = _code()
    assert "function Inline(" in code and 'split("`")' in code
    assert code.count("<Inline text={t(`codebase.connect.trouble.${id}.") == 2


# ─── the page itself ─────────────────────────────────────────────────


def test_the_address_does_not_depend_on_the_window_while_rendering() -> None:
    """Reading `window` during render gave the server and the first browser render two
    different addresses; React threw ("Hydration failed"), rebuilt the tree from the root
    and the `dark` class on <html> went with it. A store that never changes gives each
    side its own snapshot without a mismatch."""
    for path in (PAGE, SETTINGS):
        code = _code(path)
        assert "typeof window" not in code, path.name
        assert "useSyncExternalStore(noopSubscribe" in code, path.name
        assert "const noopSubscribe = () => () => {};" in code, path.name
    code = _code()
    assert re.search(r'useSyncExternalStore\(noopSubscribe, \(\) => publicMcpUrl\(""\), \(\) => ""\)', code)
    assert re.search(r'useSyncExternalStore\(noopSubscribe, \(\) => publicMcpUrl\("/mcp/dev/"\), \(\) => "/mcp/dev/"\)', code)


def test_the_route_switch_keeps_its_width_and_the_commands_fit_their_boxes() -> None:
    code = _code()
    # the control scrolls inside itself with no scrollbar: squeezed in a row it hid "By hand"
    assert re.search(r'<SegmentedControl<Route>\s+className="shrink-0"', code)
    # two columns beside the side panel cut a command short; a short command wraps
    assert "md:grid-cols-2" not in code and "xl:grid-cols-2" in code
    for command in ("addMarketplace", "install", "cliAddMarketplace", "cliInstall"):
        assert f"<CodeBlock text={{CMD.{command}}} wrap />" in code, command
    assert "<CodeBlock text={manualCommand} />" in code, "the multi-line command scrolls, on purpose"


def test_a_troubleshooting_row_shows_that_it_opens() -> None:
    code = _code()
    summary = code[code.index("<summary"):code.index("</summary>")]
    assert "ChevronDownIcon" in summary and "group-open:rotate-180" in summary


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_no_sentence_of_the_guide_is_left_in_english(locale: str) -> None:
    mine = _messages(locale)
    left = sorted(k for k, v in EN.items()
                  if k.startswith(P) and k not in VERBATIM_SYMPTOMS and mine[k] == v)
    assert not left, f"{locale} still has the English text for {left}"


@pytest.mark.parametrize("locale", LOCALES)
def test_the_request_to_the_administrator_names_the_workspace_in_every_language(locale: str) -> None:
    mine = _messages(locale)
    assert re.findall(r"\{(\w+)\}", mine[P + "step1.request"]) == ["workspace"]
    assert "\n" in mine[P + "step1.request"], "one item to a line, so it can be read and copied"
    assert mine[P + "step1.workspaceFallback"].strip()


# ─── the MCP settings page agrees ────────────────────────────────────


def test_the_settings_page_points_at_the_guide_and_never_puts_the_token_in_the_command() -> None:
    code = _code(SETTINGS)
    assert 'href="/codebase/connect"' in code
    for key in ("mcp.guideHint", "mcp.guideLink"):
        assert f'"{key}"' in code
    command = re.search(r"const claudeCode = `(.*?)`;", code, re.S)
    assert command, "the claude mcp add command moved"
    assert "${secret}" not in command.group(1), "a token typed into a shell command stays in its history"
    assert "CELMIS_MCP_TOKEN" in command.group(1)
    assert not re.search(r"Authorization: `Bearer", code)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_settings_page_text_does_not_contradict_the_guide(locale: str) -> None:
    mine = _messages(locale)
    assert "CELMIS_MCP_TOKEN" in mine["mcp.step2Body"], locale
    assert mine["mcp.guideHint"].strip() and mine["mcp.guideLink"].strip()
    if locale != "en":
        assert mine["mcp.guideHint"] != EN["mcp.guideHint"]
        assert mine["mcp.step2Body"] != EN["mcp.step2Body"]
    assert "plugin" in EN["mcp.guideHint"] and "recommended" in EN["mcp.guideHint"]
    assert "Do not combine" in EN["mcp.step2Body"]
