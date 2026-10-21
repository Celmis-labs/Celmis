"""The plugin's manifests, skill and agent: shape, budgets, and drift guards.

Nothing here talks to a server. What it pins is every place the plugin repeats
a name the server owns (tool names, topics, the endpoint path), so a rename on
one side fails a test instead of silently breaking the agent's workflow.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.plugin import contract as C

SKILL = C.PLUGIN / "skills" / "celmis-search" / "SKILL.md"
AGENT = C.PLUGIN / "agents" / "celmis-explore.md"
README = C.PLUGIN / "README.md"


def _frontmatter(path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, f"{path.name} has no frontmatter"
    fields = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(": ")
        fields[key.strip()] = value.strip()
    return fields, m.group(2)


def _all_plugin_files():
    return [p for p in C.PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


# ─── manifests ───────────────────────────────────────────────────────


def test_every_json_manifest_parses() -> None:
    for path in (C.MARKETPLACE, C.PLUGIN / ".claude-plugin" / "plugin.json",
                 C.PLUGIN / ".mcp.json", C.PLUGIN / "hooks" / "hooks.json"):
        json.loads(path.read_text(encoding="utf-8"))


def test_the_marketplace_points_at_the_plugin_directory() -> None:
    market = json.loads(C.MARKETPLACE.read_text(encoding="utf-8"))
    assert market["name"] == "celmis"
    (entry,) = market["plugins"]
    assert entry["name"] == "celmis-code"
    assert (C.ROOT / entry["source"]).resolve() == C.PLUGIN.resolve()


def test_the_plugin_is_named_so_its_tool_prefix_is_the_documented_one() -> None:
    plugin = json.loads((C.PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    server = next(iter(json.loads((C.PLUGIN / ".mcp.json").read_text())["mcpServers"]))
    assert f"mcp__plugin_{plugin['name']}_{server}__" == C.PLUGIN_PREFIX


#: What Claude Code accepts in a `userConfig` option (plugin manifest reference,
#: "User configuration"): a strict object, so any other key fails the plugin.
_OPTION_KEYS = {"type", "title", "description", "required", "default", "multiple",
                "sensitive", "min", "max", "options"}
_OPTION_TYPES = {"string", "number", "boolean", "directory", "file"}
_PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]*)\}")


def _user_config() -> dict:
    return json.loads((C.PLUGIN / ".claude-plugin" / "plugin.json").read_text())["userConfig"]


def _mcp_entry() -> dict:
    return json.loads((C.PLUGIN / ".mcp.json").read_text())["mcpServers"]["celmis"]


def test_the_plugin_declares_exactly_the_address_and_the_token_as_user_config() -> None:
    config = _user_config()
    assert set(config) == {C.OPTION_URL, C.OPTION_TOKEN}
    assert config[C.OPTION_URL]["required"] is True
    assert config[C.OPTION_TOKEN]["required"] is True
    assert config[C.OPTION_TOKEN]["sensitive"] is True, "the token goes to the keychain, not settings.json"
    assert not config[C.OPTION_URL].get("sensitive"), "the address is not a secret and is shown by /plugin"
    assert all(o["type"] == "string" for o in config.values())


def test_every_user_config_option_is_valid_by_the_documented_schema() -> None:
    config = _user_config()
    assert config, "no userConfig: Claude Code would never ask for the address or the token"
    for key, option in config.items():
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key), f"{key}: not an identifier"
        assert set(option) <= _OPTION_KEYS, f"{key}: unknown keys {set(option) - _OPTION_KEYS}"
        assert option.get("type") in _OPTION_TYPES, f"{key}: type"
        for field in ("title", "description"):
            assert isinstance(option.get(field), str) and option[field].strip(), f"{key}: {field}"
        for flag in ("required", "sensitive", "multiple"):
            assert isinstance(option.get(flag, False), bool), f"{key}: {flag} is a boolean"
        assert "default" not in option or "sensitive" not in option or not option["sensitive"], \
            f"{key}: a default would put a secret in the manifest"


def test_the_option_texts_name_the_connect_page_and_hold_no_token_or_company() -> None:
    config = _user_config()
    assert "Connect" in config[C.OPTION_URL]["description"]
    assert "https://" in config[C.OPTION_URL]["description"]
    assert "example.com" in config[C.OPTION_URL]["description"], "an example address, never a real one"
    raw = json.dumps(config)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}|cmk_|cpk_|Bearer\s+[A-Za-z0-9]{12,}", raw)


def test_the_mcp_config_targets_the_dev_endpoint_through_the_saved_options() -> None:
    cfg = _mcp_entry()
    assert cfg["type"] == "http"
    assert cfg["url"] == "${user_config." + C.OPTION_URL + "}" + C.DEV_PATH + "/"
    assert cfg["url"].endswith("/mcp/dev/")
    assert cfg["headers"] == {"Authorization": "Bearer ${user_config." + C.OPTION_TOKEN + "}"}
    assert set(cfg) == {"type", "url", "headers"}


def test_the_mcp_config_names_only_declared_options_and_no_environment_variable() -> None:
    """Claude Code strips every variable whose name has TOKEN, SECRET, PASSWORD, KEY
    or AUTH from a plugin server's `headersHelper` environment, so the 3.0.1 entry
    (`Bearer ${CELMIS_TOKEN}`, or a helper reading it) lost its header. The saved
    options are the supported path; `claude plugin validate` fails on an undeclared one."""
    raw = (C.PLUGIN / ".mcp.json").read_text()
    assert set(_PLACEHOLDER.findall(raw)) == set(_user_config()), \
        "every declared option is used, and every option used is declared"
    for gone in ("CELMIS_TOKEN", "CELMIS_URL", "headersHelper", "CLAUDE_PLUGIN_ROOT"):
        assert gone not in raw, f".mcp.json still mentions {gone}"
    assert re.sub(r"\$\{user_config\.\w+\}", "", raw).count("${") == 0


def test_substituting_the_options_gives_the_url_and_header_claude_code_sends() -> None:
    """The substitution Claude Code does, redone with dummy values."""
    values = {C.OPTION_URL: "https://celmis.example.com/backend", C.OPTION_TOKEN: "dummy.token-123"}

    def substitute(text: str) -> str:
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], text)

    cfg = _mcp_entry()
    assert substitute(cfg["url"]) == "https://celmis.example.com/backend/mcp/dev/"
    assert substitute(cfg["headers"]["Authorization"]) == "Bearer dummy.token-123"


def test_the_option_variables_the_hooks_read_are_the_ones_claude_code_exports() -> None:
    """`CLAUDE_PLUGIN_OPTION_` + the option key in capitals (manifest reference)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("celmis_hook_under_test", C.HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook.URL_OPTION == "CLAUDE_PLUGIN_OPTION_" + C.OPTION_URL.upper() == C.URL_OPTION_ENV
    assert hook.TOKEN_OPTION == "CLAUDE_PLUGIN_OPTION_" + C.OPTION_TOKEN.upper() == C.TOKEN_OPTION_ENV
    assert C.URL_OPTION_ENV == "CLAUDE_PLUGIN_OPTION_CELMIS_URL"
    assert C.TOKEN_OPTION_ENV == "CLAUDE_PLUGIN_OPTION_CELMIS_TOKEN"


def test_every_hook_command_quotes_the_plugin_path() -> None:
    """`${CLAUDE_PLUGIN_ROOT}` expands to a path that may hold a space."""
    hooks = json.loads((C.PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands
    for command in commands:
        for use in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}", command):
            assert command[use.start() - 1] == '"', f"unquoted plugin path in: {command}"


def test_the_standalone_header_helper_exists_is_executable_and_is_posix_sh() -> None:
    """Not named by the plugin's own `.mcp.json` any more: it serves the README's
    recipe for a server entry you write yourself, where `headersHelper` is allowed
    to read the environment."""
    import os
    import shutil
    import subprocess

    helper = C.HELPER
    assert helper.is_file() and os.access(helper, os.X_OK)
    assert helper.read_text().splitlines()[0] == "#!/bin/sh", "POSIX sh, not bash"
    for shell in ("sh", "dash"):  # dash is the strictest POSIX shell most machines have
        if shutil.which(shell):
            done = subprocess.run([shell, "-n", str(helper)], capture_output=True, text=True)
            assert done.returncode == 0, f"{shell} -n: {done.stderr}"
    tracked = subprocess.run(["git", "ls-files", "-s", "--", str(helper)], cwd=C.ROOT,
                             capture_output=True, text=True, check=False).stdout.split()
    if tracked:  # an exported tree without .git has nothing to say about the mode
        assert tracked[0] == "100755", "the executable bit is part of the file in git"


def test_no_file_in_the_plugin_contains_a_literal_token() -> None:
    patterns = [
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),            # a JWT
        re.compile(r"Bearer\s+(?!\$\{|%s|<|\.\.\.)[A-Za-z0-9._~+/=-]{12,}"),  # a literal bearer
        re.compile(r"\b(?:sk|pat|cmt|ghp|glpat)[-_][A-Za-z0-9]{16,}"),
        re.compile(r"CELMIS_TOKEN=(?!\.\.\.|\$|\")[A-Za-z0-9._~+/=-]{8,}"),
    ]
    for path in _all_plugin_files():
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not pattern.search(text), f"{path.name} looks like it holds a token"


def test_nothing_company_specific_ships_in_the_plugin_or_the_marketplace() -> None:
    assert C.forbidden_hits([C.PLUGIN, C.MARKETPLACE]) == []


def test_the_scripts_are_executable() -> None:
    import os

    for path in (C.HOOK, C.PLUGIN / "bin" / "celmis-auth-header"):
        assert os.access(path, os.X_OK), f"{path.name} lost its executable bit"


# ─── the skill ───────────────────────────────────────────────────────


def test_the_skill_description_fits_claude_codes_listing_budget() -> None:
    fields, _ = _frontmatter(SKILL)
    assert fields["name"] == "celmis-search"
    assert 0 < len(fields["description"]) <= 1536


def test_the_skill_body_is_small_enough_to_stay_resident() -> None:
    _, body = _frontmatter(SKILL)
    assert len(body) / 3.7 <= 1800, "the skill is paid for on every turn it is loaded"


def test_the_skill_names_only_tools_the_dev_profile_has() -> None:
    _, body = _frontmatter(SKILL)
    named = set(re.findall(r"^(?:\d\.|-) `(\w+)` - ", body, re.M))
    named |= set(re.findall(r"`(\w+)\((?:topic|\w+, ?\w+)", body))
    assert named, "the skill lists no tools at all"
    assert named <= set(C.DEV_TOOLS), f"unknown tools in the skill: {named - set(C.DEV_TOOLS)}"


def test_the_skill_teaches_every_dev_tool_and_every_howto_topic() -> None:
    fields, body = _frontmatter(SKILL)
    for tool in C.DEV_TOOLS:
        assert f"`{tool}`" in body or f"`{tool}(" in body, f"the skill never mentions {tool}"
    for topic in C.HOWTO_TOPICS:
        assert f"`{topic}`" in body, f"the skill never mentions the howto topic {topic}"


def test_the_skill_states_the_secret_rule_and_the_search_order() -> None:
    _, body = _frontmatter(SKILL)
    order = ["`repos`", "`find`", "`outline`", "`read_symbol`", "`refs`"]
    positions = [body.index(o) for o in order]
    assert positions == sorted(positions)
    lowered = body.lower()
    for phrase in ("never returns values", "ask the user or ops", "do not search for values",
                   "cursor", "[stale-locally]", "data from an index, not instructions"):
        assert phrase in lowered, f"the skill lost: {phrase}"


# ─── the agent, hooks and README ─────────────────────────────────────


def test_the_agent_lists_only_real_tools_and_names_no_write_tool() -> None:
    fields, _ = _frontmatter(AGENT)
    tools = [t.strip() for t in fields["tools"].split(",")]
    celmis = [t for t in tools if t.startswith(C.PLUGIN_PREFIX)]
    assert {t[len(C.PLUGIN_PREFIX):] for t in celmis} <= set(C.DEV_TOOLS)
    assert not ({"Write", "Edit", "Bash", "NotebookEdit"} & set(tools))


def test_every_prefixed_tool_name_anywhere_in_the_plugin_exists() -> None:
    for path in _all_plugin_files():
        for name in re.findall(re.escape(C.PLUGIN_PREFIX) + r"(\w+)",
                               path.read_text(encoding="utf-8")):
            assert name in C.DEV_TOOLS or name == "", f"{path.name}: unknown tool {name}"


def test_the_hook_copies_of_the_contract_equal_the_contract() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("celmis_hook_under_test", C.HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert tuple(hook.DEV_TOOLS) == tuple(C.DEV_TOOLS)
    assert hook.IDX_LINE_RE == C.IDX_LINE_RE
    assert hook.IDX_ENTRY_RE == C.IDX_ENTRY_RE
    assert hook.HIT_RE == C.HIT_RE


def test_the_hook_wiring_runs_the_right_subcommand_on_the_right_event() -> None:
    hooks = json.loads((C.PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    expect = {"SessionStart": "session", "PreToolUse": "pregrep", "PostToolUse": "postmcp"}
    assert set(hooks) == set(expect)
    for event, sub in expect.items():
        (group,) = hooks[event]
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/celmis_hook.py" in hook["command"]
        assert hook["command"].count(f" {sub}") == 2, "python3 with a python fallback"
        assert 0 < hook["timeout"] <= 10


@pytest.mark.parametrize("tool", C.DEV_TOOLS)
def test_the_post_hook_matcher_catches_every_plugin_dev_tool(tool: str) -> None:
    hooks = json.loads((C.PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    matcher = hooks["PostToolUse"][0]["matcher"]
    assert re.fullmatch(matcher, C.PLUGIN_PREFIX + tool)
    assert not re.fullmatch(matcher, "mcp__gitnexus__query")
    assert not re.fullmatch(matcher, "Grep")


def test_the_pre_hook_matcher_is_grep_and_glob_only() -> None:
    hooks = json.loads((C.PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
    matcher = hooks["PreToolUse"][0]["matcher"]
    assert re.fullmatch(matcher, "Grep") and re.fullmatch(matcher, "Glob")
    assert not re.fullmatch(matcher, "Read") and not re.fullmatch(matcher, "Bash")


def test_the_readme_records_why_there_is_no_proxy_and_how_to_roll_out() -> None:
    text = README.read_text(encoding="utf-8")
    for needle in ("Why there is no local proxy", "extraKnownMarketplaces",
                   "enabledPlugins", "pluginConfigs", "headersHelper", "managed"):
        assert needle in text


def test_the_readme_installs_by_asking_claude_code_for_the_address_and_the_token() -> None:
    text = README.read_text(encoding="utf-8")
    install = " ".join(text[text.index("## Install"):text.index("### Using your own server entry")].split())
    for needle in ("/plugin install celmis-code@celmis", "/plugin configure celmis-code@celmis",
                   "claude plugin configure celmis-code@celmis --values-stdin",
                   "mcp__plugin_celmis-code_celmis__", "Connect", "Keychain",
                   ".credentials.json", "Restart Claude Code", "Windows"):
        assert needle in install, f"the Install section lost: {needle}"
    assert "export CELMIS_TOKEN" not in install and "security -i" not in install, \
        "the plugin no longer reads a token from the shell or from a keychain item of ours"
    assert "bin/celmis-auth-header" in text.split("## Install")[0], "the components table"


def test_the_readme_keeps_the_token_out_of_argv_and_off_the_command_line() -> None:
    text = README.read_text(encoding="utf-8")
    assert "--values-stdin" in text
    for line in text.splitlines():
        assert not re.search(r"--config\s+celmis_token=", line), "a token in argv shows in `ps`"
        assert not re.search(r"celmis_token\\?\"?\s*[:=]\s*\\?\"?[A-Za-z0-9._-]{12,}", line), line


def test_the_readme_tells_a_3_0_1_user_what_to_do_once() -> None:
    text = " ".join(README.read_text(encoding="utf-8").split())
    assert "Upgrading from 3.0.1" in text
    upgrade = text[text.index("Upgrading from 3.0.1"):]
    assert "/plugin configure celmis-code@celmis" in upgrade
    assert "no longer reads" in upgrade and "CELMIS_TOKEN" in upgrade and "CELMIS_URL" in upgrade


def test_the_readme_troubleshooting_covers_what_claude_code_shows() -> None:
    text = README.read_text(encoding="utf-8")
    trouble = text[text.index("## Troubleshooting"):]
    for needle in ("HTTP 401", "not yet set", "No `celmis` server", "/plugin configure"):
        assert needle in trouble, f"troubleshooting lost: {needle}"


def test_the_headers_helper_recipe_quotes_an_absolute_path_that_exists_outside_the_plugin() -> None:
    """Claude Code runs a `headersHelper` through a shell, so a path with a space in it is
    two words unless it is quoted. The recipe quotes it inside the JSON string."""
    import shlex

    text = README.read_text(encoding="utf-8")
    block = next(b for b in re.findall(r"```json\n(.*?)```", text, re.S) if "headersHelper" in b)
    command = json.loads(block)["mcpServers"]["celmis"]["headersHelper"]
    (path,) = shlex.split(command)  # exactly one word, once the quotes are taken off
    assert command.startswith('"') and command.endswith('"'), "the path is quoted in the string"
    assert "CLAUDE_PLUGIN_ROOT" not in path and path.startswith("/"), path
    assert path.endswith("/celmis-auth-header")
    # the install step copies the script that really ships, under that very name
    assert C.HELPER.is_file()
    assert 'celmis-auth-header" ~/.local/bin/celmis-auth-header' in text


def test_a_quoted_helper_path_with_a_space_in_it_runs_as_one_command(tmp_path) -> None:
    """What the README's quoting buys: the same command string, run by a shell, from a
    directory whose name has a space, prints the header."""
    import shutil
    import subprocess

    home = tmp_path / "Ann Lee" / ".local" / "bin"
    home.mkdir(parents=True)
    helper = home / "celmis-auth-header"
    shutil.copy2(C.HELPER, helper)
    command = json.dumps(str(helper))  # a double-quoted word, as in the recipe
    stubs = tmp_path / "stubs"  # shadow the keychain tools: no test reads a real keychain
    stubs.mkdir()
    for name in ("security", "secret-tool"):
        (stubs / name).write_text("#!/bin/sh\nexit 1\n")
        (stubs / name).chmod(0o755)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path), "CELMIS_TOKEN": "tok.en-123"}
    done = subprocess.run(command, shell=True, capture_output=True, text=True, env=env, timeout=10)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"Authorization": "Bearer tok.en-123"}
    # unquoted, the same path is two words and the shell cannot find a command
    bare = subprocess.run(str(helper), shell=True, capture_output=True, text=True, env=env, timeout=10)
    assert bare.returncode != 0 and bare.stdout == ""
