"""A repository's settings edit what the server accepts — every agent, every field.

The "Agent system prompts (this repo)" card once listed `["defect",
"contract", "security"]` by hand: the verifier, which the API has always
accepted a per-repo prompt for, had no box, and a roster change would have
left the page behind the server the way three literals in the router once
were. The settings page renders a box per name in the policy's
`overridable_agents` — computed by the router from the orchestrator's roster.

And the AI Agents pages said prompts were workspace-wide, full stop. The
workspace prompts now sit beside the repository ones (/review-settings ›
Custom prompts), say a repository override wins, and list which
repositories have one.

Everything here reads the source with its comments STRIPPED: these files are
full of prose about the very literals that were removed, and a name surviving
in a comment must not count as surviving in code.
"""

from __future__ import annotations

import json
import re

from src.api.schemas import ReviewPolicyIn, ReviewPolicyOut
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    POLICY,
    WEB,
    _strip_comments,
)

SETTINGS = WEB / "components" / "review-settings"
PROMPTS = SETTINGS / "section-prompts.tsx"
SHELL = SETTINGS / "review-settings.tsx"
ROUTES = WEB / "lib" / "review-settings-routes.ts"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _function(code: str, name: str) -> str:
    start = code.index(f"export function {name}(")
    return code[start:code.index("\n}\n", start)]


def test_no_agent_list_is_hard_coded_for_prompts():
    for path in (PROMPTS, POLICY):
        assert not re.search(
            r'\[\s*"defect"\s*,\s*"contract"\s*,\s*"security"\s*\]', _code(path)
        ), f"{path.name}: the prompt boxes are back to a hand-written agent list"
    assert "overridable_agents" in _code(POLICY)
    assert "overridable_agents" in _code(SHELL)
    assert "meta.overridableAgents" in _code(PROMPTS)


def test_the_server_lists_the_verifier_among_the_overridable_agents():
    from src.api.routers.review_policies import _OVERRIDABLE_AGENT_ORDER

    assert "verifier" in _OVERRIDABLE_AGENT_ORDER
    assert {"overridable_agents", "rule_target_agents",
            "review_languages"} <= set(ReviewPolicyOut.model_fields)


def test_every_field_the_repository_save_sends_is_one_the_api_accepts():
    """The PUT model is extra="forbid": a key the page invents is a 422. And
    every key the API takes is sent — a full replace drops what is not."""
    body = _function(_code(POLICY), "policyPayload")
    body = body[body.index("return {"):]
    sent = set(re.findall(r"^\s{4}(\w+):", body, re.M))
    accepted = set(ReviewPolicyIn.model_fields)
    assert sent, "policyPayload sends nothing?"
    assert sent <= accepted, f"not accepted by the API: {sorted(sent - accepted)}"
    assert accepted <= sent, f"never sent, so a save resets them: {sorted(accepted - sent)}"
    rule = ReviewPolicyIn.model_fields["folder_rules"].annotation.__args__[0]
    for key in ("title", "severity_hint", "agents"):
        assert key in rule.model_fields
        assert f"r.{key}" in body, f"a legacy rule's {key} is not carried through a save"


def test_what_the_form_cannot_show_survives_a_save():
    body = _function(_code(POLICY), "policyPayload")
    assert "tests_model: policy.tests_model" in body
    # LLM entries of agents without a row on this page (compliance) ride along.
    assert "POLICY_LLM_AGENTS as readonly string[]).includes(agent)) overrides[agent] = entry" in body


def test_the_read_only_fields_never_travel_back():
    """`ReviewPolicyUpdate` omits what the server computes; each of those must
    really be output-only, or the omission would drop a setting."""
    api = _code(API)
    m = re.search(r"type ReviewPolicyReadOnly =([^;]+);", api)
    assert m, "ReviewPolicyReadOnly is gone from lib/api.ts"
    names = re.findall(r'"(\w+)"', m.group(1))
    server_only = {"repo_slug", "created_at", "updated_at", "updated_by",
                   "agent_llm_overrides"}
    for name in names:
        assert name in ReviewPolicyOut.model_fields, name
        if name not in server_only:
            assert name not in ReviewPolicyIn.model_fields, (
                f"{name} is writable but the page's PUT type omits it")


def test_the_old_agent_pages_land_on_the_prompts():
    routes = _code(ROUTES)
    assert re.search(r'section: "prompts", agent: name', routes)
    for page in ("agents/page.tsx", "agents/[name]/page.tsx"):
        src = _code(WEB / "app" / "(app)" / "admin" / page)
        assert "legacyAgentHref(" in src and "redirect(" in src, page


def test_the_workspace_prompts_say_which_repositories_override_them():
    code = _code(PROMPTS)
    assert "overridesSummary" in code
    assert 'section: "prompts", agent: agent.name' in code


def test_every_prompt_source_has_a_label_in_every_locale():
    for path in sorted(MESSAGES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for source in ("repo", "workspace", "builtin"):
            assert f"admin.reviewPolicies.detail.promptSource.{source}" in data


def test_the_agents_copy_says_a_repository_override_wins():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    key = "reviewSettings.prompts.desc"
    assert "repository" in en[key] and "workspace" in en[key], key
    assert "репозитор" in uk[key], key
    assert "Prompts here are workspace-wide" not in en[key]
