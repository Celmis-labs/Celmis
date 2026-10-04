"""The repo policy page edits what the server accepts — every agent, every field.

The "Agent system prompts (this repo)" card listed `["defect", "contract",
"security"]` by hand: the verifier, which the API has always accepted a
per-repo prompt for, had no box, and a roster change would have left the page
behind the server the way three literals in the router once were. The page now
renders a box per name in the policy's `overridable_agents` and a target chip
per name in `rule_target_agents` — both computed by the router from the
orchestrator's roster.

And the AI Agents pages said prompts were workspace-wide, full stop. They now
say a repository override wins, and list which repositories have one.

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

AGENTS_PAGE = WEB / "app" / "(app)" / "admin" / "agents" / "page.tsx"
AGENT_PAGE = WEB / "app" / "(app)" / "admin" / "agents" / "[name]" / "page.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_no_agent_list_is_hard_coded_for_prompts_or_rules():
    code = _code(POLICY)
    assert not re.search(
        r'\[\s*"defect"\s*,\s*"contract"\s*,\s*"security"\s*\]', code
    ), "the prompt card is back to a hand-written agent list"
    assert "overridable_agents" in code
    assert "rule_target_agents" in code


def test_the_server_lists_the_verifier_among_the_overridable_agents():
    from src.api.routers.review_policies import _OVERRIDABLE_AGENT_ORDER

    assert "verifier" in _OVERRIDABLE_AGENT_ORDER
    assert {"overridable_agents", "rule_target_agents",
            "review_languages"} <= set(ReviewPolicyOut.model_fields)


def test_every_new_field_the_page_saves_is_one_the_api_accepts():
    """The PUT model is extra="forbid": a key the page invents is a 422."""
    code = _code(POLICY)
    sent = ("suppressed_rules", "summary_enabled", "summary_instructions",
            "started_comment_enabled", "review_language", "max_inline_comments",
            "verifier_enabled", "folder_rules", "agent_prompt_overrides")
    for key in sent:
        assert re.search(rf"\b{key}:", code), f"the page no longer sends {key}"
        assert key in ReviewPolicyIn.model_fields, f"{key} is not accepted by the API"
    rule = ReviewPolicyIn.model_fields["folder_rules"].annotation.__args__[0]
    for key in ("title", "severity_hint", "agents"):
        assert key in rule.model_fields
        assert f"{key}:" in code or f"{key} ?" in code or f".{key}" in code


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


def test_the_tabs_the_agents_pages_link_to_exist():
    code = _code(POLICY)
    tabs = re.search(r"const POLICY_TABS: PolicyTab\[\] = \[([^\]]+)\]", code)
    assert tabs
    names = re.findall(r'"(\w+)"', tabs.group(1))
    for wanted in ("general", "agents", "rules", "comments", "ignore"):
        assert wanted in names
    for page in (AGENTS_PAGE, AGENT_PAGE):
        src = _code(page)
        assert "overridesSummary" in src, f"{page.name} does not ask who overrides"
        assert "?tab=agents" in src


def test_every_tab_has_a_label_in_every_locale():
    code = _code(POLICY)
    names = re.findall(
        r'"(\w+)"',
        re.search(r"const POLICY_TABS: PolicyTab\[\] = \[([^\]]+)\]", code).group(1),
    )
    for path in sorted(MESSAGES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for tab in names:
            assert f"admin.reviewPolicies.detail.tab.{tab}" in data, (path.name, tab)
        for source in ("repo", "workspace", "builtin"):
            assert f"admin.reviewPolicies.detail.promptSource.{source}" in data


def test_the_agents_copy_says_a_repository_override_wins():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for key in ("admin.agents.descriptionLead", "admin.agents.detail.systemPromptDesc"):
        assert "repository" in en[key] and "wins" in en[key], key
        assert "репозитор" in uk[key], key
        # The old claim, that a prompt here applies to every review, is gone.
        assert "Prompts here are workspace-wide" not in en[key]
