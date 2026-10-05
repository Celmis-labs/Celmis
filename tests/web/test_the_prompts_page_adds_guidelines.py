"""The Prompts section edits guidelines first and keeps replacement behind a warning.

The page used to offer one box per agent, and that box REPLACED the agent's
whole system prompt — the incident this fixes. Now every agent card leads
with "Guidelines (added to the built-in prompt)", capped like the server,
with what the built-in prompt already covers as help; replacing the prompt is
folded under "Advanced: replace the built-in prompt" with a warning; the
repository card can extend the workspace's guidelines instead of replacing
them; the preview folds the agent's own prompt and highlights what was
added.

Read with comments STRIPPED: a name surviving in a comment must not count.
"""

from __future__ import annotations

import json
import re

from src.api.schemas import ReviewPolicyIn, ReviewPolicyOut
from src.review.prompt_guidelines import GUIDELINES_MAX_CHARS
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    POLICY,
    WEB,
    _strip_comments,
)

SETTINGS = WEB / "components" / "review-settings"
PROMPTS = SETTINGS / "section-prompts.tsx"
SHELL = SETTINGS / "review-settings.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_the_cap_is_the_servers():
    m = re.search(r"export const GUIDELINES_MAX = ([\d_]+);", _code(POLICY))
    assert m and int(m.group(1).replace("_", "")) == GUIDELINES_MAX_CHARS


def test_the_save_sends_both_new_fields_and_the_api_takes_them():
    model = _code(POLICY)
    body = model[model.index("export function policyPayload("):]
    assert "agent_prompt_guidelines:" in body and "agent_guidelines_extend:" in body
    for name in ("agent_prompt_guidelines", "agent_guidelines_extend"):
        assert name in ReviewPolicyIn.model_fields
        assert name in ReviewPolicyOut.model_fields
    assert "p.agent_prompt_guidelines" in model and "p.agent_guidelines_extend" in model


def test_every_agent_card_leads_with_guidelines():
    code = _code(PROMPTS)
    # Both scopes render the guidelines editor, counted against the cap.
    assert code.count("<GuidelinesEditor") == 2
    editor = code[code.index("function GuidelinesEditor("):code.index("function AdvancedReplace(")]
    assert "GUIDELINES_MAX" in editor and "<CharCount" in editor
    assert 'reviewSettings.prompts.guidelines"' in editor
    assert "reviewSettings.prompts.guidelinesCovers" in editor
    # The short "what it already checks" description is the help text.
    assert code.count("guidelines_hint") == 2


def test_replacing_is_folded_behind_a_warning_at_both_scopes():
    code = _code(PROMPTS)
    advanced = code[code.index("function AdvancedReplace("):]
    advanced = advanced[:advanced.index("\n}\n")]
    assert "<details" in advanced
    assert 'tone="warning"' in advanced and "reviewSettings.prompts.replaceWarning" in advanced
    assert code.count("<AdvancedReplace") == 2
    # The replacement textareas live inside it.
    for card in ("function RepoAgentPrompt(", "function WorkspaceAgentPrompt("):
        body = code[code.index(card):]
        body = body[:body.index("\n}\n")]
        start = body.index("<AdvancedReplace")
        end = body.index("</AdvancedReplace>")
        assert "draft.agentPrompts" in body or "workspacePrompts" in body
        assert "<Textarea" in body[start:end], card


def test_the_badges_say_custom_inherited_or_not_set():
    code = _code(PROMPTS)
    for key in ("reviewSettings.prompts.custom", "reviewSettings.prompts.inherited",
                "reviewSettings.prompts.notSet", "reviewSettings.prompts.reset",
                "reviewSettings.prompts.replaces"):
        assert key in code, key


def test_a_repository_can_extend_the_workspace_guidelines():
    code = _code(PROMPTS)
    repo = code[code.index("function RepoAgentPrompt("):]
    repo = repo[:repo.index("\n}\n")]
    assert "<Switch" in repo and "guidelinesExtend" in repo
    assert "reviewSettings.prompts.extendWorkspace" in repo


def test_the_preview_folds_the_base_and_highlights_the_added_blocks():
    code = _code(PROMPTS)
    part = code[code.index("function PreviewPart("):]
    part = part[:part.index("\n}\n")]
    base = part[part.index('part.kind === "base"'):part.index("const kind")]
    assert "<details" in base, "the agent's own prompt is not folded"
    assert 'kind === "guidelines"' in part and "color-primary" in part
    assert "reviewSettings.prompts.previewAdded" in part
    # Both scopes preview through the server's composition.
    assert "reviewPoliciesApi.promptPreview(" in code
    assert "reviewPoliciesApi.workspacePromptPreview(" in code


def test_the_api_client_names_the_servers_routes():
    api = _code(API)
    assert "/api/agents/${name}/guidelines" in api
    assert "/api/review-policies/prompt-preview?agent=" in api
    from src.api.routers import agents, review_policies

    paths = {r.path for r in agents.router.routes} | {r.path for r in review_policies.router.routes}
    assert "/api/agents/{name}/guidelines" in paths
    assert "/api/review-policies/prompt-preview" in paths


def test_the_workspace_save_writes_and_clears_guidelines():
    shell = _code(SHELL)
    save = shell[shell.index("const save = useMutation("):]
    save = save[:save.index("onSuccess")]
    assert "agentsApi.setGuidelines(" in save and "agentsApi.resetGuidelines(" in save
    assert "GUIDELINES_MAX" in shell and "reviewSettings.save.blockedGuidelines" in shell


def _keys_used() -> set[str]:
    code = _code(PROMPTS)
    keys = set(re.findall(r'"(reviewSettings\.[\w.]+)"', code))
    keys |= {f"reviewSettings.prompts.previewBase.{s}" for s in ("builtin", "workspace", "repo")}
    kinds = re.search(r"const PART_KINDS = \[([^\]]+)\]", code)
    assert kinds, "PART_KINDS is gone"
    keys |= {f"reviewSettings.prompts.previewPart.{k}"
             for k in re.findall(r'"(\w+)"', kinds.group(1))}
    return keys


def test_every_label_the_section_uses_is_in_every_locale():
    keys = _keys_used()
    assert "reviewSettings.prompts.guidelines" in keys
    for path in sorted(MESSAGES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        missing = sorted(k for k in keys if k not in data)
        assert not missing, f"{path.name}: {missing}"


def test_english_and_ukrainian_are_written_not_copied():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    new = [k for k in _keys_used()
           if k.startswith(("reviewSettings.prompts.guidelines",
                            "reviewSettings.prompts.advanced",
                            "reviewSettings.prompts.replace",
                            "reviewSettings.prompts.extend",
                            "reviewSettings.prompts.preview"))]
    assert len(new) >= 10
    for key in new:
        assert en[key] != uk[key], key
    assert "added" in en["reviewSettings.prompts.guidelines"].lower()
    assert "додаються" in uk["reviewSettings.prompts.guidelines"]
    assert "guidelines" in en["reviewSettings.prompts.desc"]
