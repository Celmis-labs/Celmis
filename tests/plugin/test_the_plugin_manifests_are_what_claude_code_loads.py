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


def test_the_mcp_config_targets_the_dev_endpoint_with_a_token_from_the_environment() -> None:
    cfg = json.loads((C.PLUGIN / ".mcp.json").read_text())["mcpServers"]["celmis"]
    assert cfg["type"] == "http"
    assert cfg["url"] == "${CELMIS_URL}" + C.DEV_PATH + "/"
    assert cfg["url"].endswith("/mcp/dev/")
    assert cfg["headers"] == {"Authorization": "Bearer ${CELMIS_TOKEN}"}


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
                   "enabledPlugins", "CELMIS_URL", "CELMIS_TOKEN", "headersHelper"):
        assert needle in text


def test_the_headers_helper_recipe_uses_a_path_that_exists_outside_the_plugin() -> None:
    text = README.read_text(encoding="utf-8")
    recipe = re.search(r'"headersHelper":\s*"([^"]+)"', text)
    assert recipe, "the README shows no headersHelper recipe"
    path = recipe.group(1)
    assert "CLAUDE_PLUGIN_ROOT" not in path and path.startswith("/"), path
    assert path.endswith("/celmis-auth-header")
    # the install step copies the script that really ships, under that very name
    assert (C.PLUGIN / "bin" / "celmis-auth-header").is_file()
    assert 'celmis-auth-header" ~/.local/bin/celmis-auth-header' in text
