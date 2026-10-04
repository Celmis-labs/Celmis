"""Every code review setting lives on one page, and every one is editable there.

The settings were spread over three pages, each with a third of them:
/admin/review-defaults (the workspace layer, three tabs), /admin/review-
policies/<repo> (the repository layer, seven tabs, some named like the
workspace's and holding different things) and /admin/agents (the workspace
agent prompts). Twelve of the 2.3.0 settings had no control on any of them —
the workspace page carried them from GET to PUT unseen.

/review-settings is the one page: Global and every repository on the left,
eight sections on the right, the same row for a field at either scope. What
is pinned here:

  * it is one tab of Code review, and the old tabs are gone from the row;
  * every inheritable setting the API takes is edited in a section, derived
    from the server's own field lists, so a field added there fails here
    until it has a control;
  * both saves send every field their PUT accepts (a repository PUT is a
    full replace — a field not sent is reset);
  * the old routes are redirects that land on the section holding what the
    old tab held, compiled and run, not grepped;
  * the target-branch matcher and the message-template parser on screen
    answer exactly what the server answers, run side by side;
  * an agent switch writes the right list (opt-in agents are named in
    `enabled_agents`) and flipping twice leaves no override behind;
  * who may not edit is told why, and the controls are disabled.

Read with comments stripped: a name surviving in prose must not count as the
name surviving in code.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

import pytest

from src.api.schemas import WorkspaceReviewDefaultsIn
from src.review.review_defaults import INHERITABLE_FIELDS, V23_FIELDS
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _lift,
    _strip_comments,
)

SETTINGS = WEB / "components" / "review-settings"
MODEL = SETTINGS / "model.ts"
SHELL = SETTINGS / "review-settings.tsx"
FIELD = SETTINGS / "field.tsx"
PAGE = WEB / "app" / "(app)" / "review-settings" / "page.tsx"
ROUTES = WEB / "lib" / "review-settings-routes.ts"
BRANCHES = WEB / "lib" / "branch-patterns.ts"
TEMPLATES = WEB / "lib" / "message-templates.ts"
TABS = WEB / "components" / "section-tabs.tsx"
API = WEB / "lib" / "api.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"
ADMIN = WEB / "app" / "(app)" / "admin"

SECTION_FILE = {
    "general": "section-general.tsx",
    "categories": "section-categories.tsx",
    "filters": "section-filters.tsx",
    "prompts": "section-prompts.tsx",
    "summary": "section-summary.tsx",
    "rules": "section-rules.tsx",
    "messages": "section-messages.tsx",
    "advanced": "section-advanced.tsx",
}


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _ts_list(code: str, name: str) -> list[str]:
    m = re.search(rf"export const {name} = \[(.*?)\] as const;", code, re.S)
    assert m, f"{name} is not an `export const … as const` list"
    return re.findall(r'"(\w+)"', m.group(1))


def _field_section() -> dict[str, str]:
    code = _code(MODEL)
    m = re.search(r"export const FIELD_SECTION[^=]*= \{(.*?)\};", code, re.S)
    assert m, "FIELD_SECTION is gone from the model"
    return dict(re.findall(r"(\w+): \"(\w+)\"", m.group(1)))


def _function(code: str, name: str) -> str:
    start = code.index(f"export function {name}(")
    return code[start:code.index("\n}\n", start)]


# ─── one page, one tab ───────────────────────────────────────────────


def test_settings_is_one_tab_of_code_review():
    review = re.search(r"review: \[(.*?)\n  \],", _code(TABS), re.S)
    assert review, "the review tab set changed shape"
    block = review.group(1)
    assert '"/review-settings"' in block
    for gone in ("/admin/review-defaults", "/admin/review-policies", '"/admin/agents"'):
        assert gone not in block, f"{gone} is a separate tab again"
    for rare in ("/admin/compliance", "/admin/deprecations"):
        line = next(ln for ln in block.splitlines() if rare in ln)
        assert "more: true" in line, f"{rare} left the More menu"
    assert PAGE.is_file()


def test_the_more_menu_is_drawn_and_reachable_by_keyboard():
    code = _code(TABS)
    assert "DropdownMenuTrigger" in code and "DropdownMenuItem" in code
    assert 't("nav.more")' in code
    assert "!tab.more" in code, "the More entries are drawn as tabs as well"


def test_every_section_has_a_view_and_a_label():
    sections = _ts_list(_code(ROUTES), "SECTION_IDS")
    assert sections == list(SECTION_FILE)
    shell = _code(SHELL)
    view = re.search(r"const SECTION_VIEW[^{]*\{(.*?)\};", shell, re.S)
    assert view and sorted(re.findall(r"(\w+):", view.group(1))) == sorted(sections)
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    for s in sections:
        assert (SETTINGS / SECTION_FILE[s]).is_file(), s
        assert f"reviewSettings.section.{s}" in en


# ─── every setting is editable somewhere ─────────────────────────────


def test_the_model_knows_every_inheritable_field():
    keys = _ts_list(_code(MODEL), "INHERITABLE_KEYS")
    assert set(keys) == {*INHERITABLE_FIELDS, "review_language"}, (
        "the form and src/review/review_defaults.py disagree about which "
        "settings inherit")
    assert set(_field_section()) == {*keys, "agents"}


@pytest.mark.parametrize("field", [*INHERITABLE_FIELDS, "review_language"])
def test_every_inheritable_field_has_a_control(field):
    """Derived from the server's list: a setting added there fails here until
    the section the model files it under actually names it."""
    section = _field_section()[field]
    code = _code(SETTINGS / SECTION_FILE[section])
    assert f'"{field}"' in code, f"{field} is filed under {section} but nothing there edits it"


def test_every_v23_setting_is_among_them():
    assert set(V23_FIELDS) <= set(_field_section())


def test_the_global_save_sends_every_field_the_api_takes():
    body = _function(_code(MODEL), "defaultsPayload")
    body = body[body.index("const payload"):]
    sent = set(re.findall(r"^\s{4}(\w+):", body, re.M)) | {"agents"}
    assert "payload.agents = map" in body
    accepted = set(WorkspaceReviewDefaultsIn.model_fields)
    assert sent == accepted, (sorted(accepted - sent), sorted(sent - accepted))


def test_the_api_client_names_the_routes():
    api = _code(API)
    for route in ('"/api/review-defaults"', '"/api/review-settings/overview"'):
        assert route in api, route
    shell = _code(SHELL)
    for call in ("reviewSettingsApi.overview(", "reviewDefaultsApi.get(", "reviewDefaultsApi.save(",
                 "reviewPoliciesApi.get(", "reviewPoliciesApi.upsert(", "reviewPoliciesApi.reset(",
                 "agentsApi.list(", "agentsApi.overridePrompt(", "agentsApi.resetPrompt("):
        assert call in shell, call


def test_a_field_says_where_its_value_comes_from():
    field = _code(FIELD)
    assert "inh.sources[field]" in field
    for key in ("reviewSettings.origin.overridden", "reviewSettings.origin.inheritedWorkspace",
                "reviewSettings.origin.workspaceIs", "reviewSettings.reset.toInherited"):
        assert key in field, key
    assert "inherited_sources" in _code(MODEL)


# ─── who may edit ────────────────────────────────────────────────────


def test_who_cannot_edit_is_told_why_and_cannot_press():
    shell = _code(SHELL)
    assert "defaults.data?.can_edit === true" in shell
    assert "useCanEditPrompts()" in shell
    for key in ("reviewSettings.access.repoReadOnly", "reviewSettings.access.globalReadOnly",
                "reviewSettings.access.globalEditor"):
        assert key in shell, key
    # Every control answers to the same switch: the shared rows read it in
    # field.tsx, and a section drawing its own inputs reads it itself.
    assert _code(FIELD).count("canEdit") >= 6
    for name in SECTION_FILE.values():
        code = _code(SETTINGS / name)
        if re.search(r"<(Input|Textarea|Switch|Select)\b", code):
            assert "canEdit" in code, f"{name} ignores who may edit"


# ─── the old routes ──────────────────────────────────────────────────


@pytest.mark.parametrize(("rel", "call"), [
    ("review-defaults/page.tsx", "legacyDefaultsHref("),
    ("review-policies/page.tsx", "settingsHref("),
    ("review-policies/[slug]/page.tsx", "legacyPolicyHref("),
    ("agents/page.tsx", "legacyAgentHref("),
    ("agents/[name]/page.tsx", "legacyAgentHref("),
])
def test_an_old_route_is_a_server_redirect(rel, call):
    src = (ADMIN / rel).read_text(encoding="utf-8")
    assert '"use client"' not in src, f"{rel} renders in the browser before redirecting"
    code = _strip_comments(src)
    assert 'from "next/navigation"' in code and "redirect(" in code
    assert call in code, rel


def _run_ts(tmp: Path, sources: dict[str, str], main: str) -> object:
    for name, text in sources.items():
        (tmp / name).write_text(text, encoding="utf-8")
    (tmp / "main.ts").write_text(main, encoding="utf-8")
    build = subprocess.run(
        [str(TSC), "main.ts", *sources, "--target", "es2020", "--module", "commonjs",
         "--lib", "es2020,dom", "--outDir", "out", "--skipLibCheck"],
        cwd=tmp, capture_output=True, text=True, timeout=300,
    )
    js = tmp / "out" / "main.js"
    if not js.exists():  # pragma: no cover - tsc itself failed
        pytest.fail(f"tsc emitted nothing:\n{build.stdout}\n{build.stderr}")
    run = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.fixture
def _web(tmp_path):
    if not TSC.exists():
        pytest.skip("web deps not installed")
    return tmp_path


#: Every tab the old pages had, and where its first card lives now.
OLD_TABS = {
    ("defaults", None): "/review-settings",
    ("defaults", "agents"): "/review-settings?section=categories",
    ("defaults", "comments"): "/review-settings?section=filters",
    ("defaults", "ignore"): "/review-settings?section=filters",
    ("defaults", "bogus"): "/review-settings",
    ("policy", None): "/review-settings?repo=acme%2Fapi",
    ("policy", "general"): "/review-settings?repo=acme%2Fapi",
    ("policy", "agents"): "/review-settings?repo=acme%2Fapi&section=categories",
    ("policy", "rules"): "/review-settings?repo=acme%2Fapi&section=rules",
    ("policy", "comments"): "/review-settings?repo=acme%2Fapi&section=filters",
    ("policy", "ignore"): "/review-settings?repo=acme%2Fapi&section=filters",
    ("policy", "models"): "/review-settings?repo=acme%2Fapi&section=categories",
    ("policy", "mcp"): "/review-settings?repo=acme%2Fapi&section=advanced",
    ("agent", None): "/review-settings?section=prompts",
    ("agent", "security"): "/review-settings?section=prompts&agent=security",
}


def test_every_old_tab_lands_on_its_section(_web):
    cases = [[kind, tab] for kind, tab in OLD_TABS]
    got = _run_ts(
        _web, {"routes.ts": ROUTES.read_text(encoding="utf-8")},
        'import { legacyDefaultsHref, legacyPolicyHref, legacyAgentHref, sectionFromParam }'
        ' from "./routes";\n'
        f"const cases: any[] = {json.dumps(cases)};\n"
        "const out = cases.map(([kind, tab]) => {\n"
        "  const p: any = tab === null ? {} : { tab };\n"
        "  if (kind === 'defaults') return legacyDefaultsHref(p);\n"
        "  if (kind === 'policy') return legacyPolicyHref('acme/api', p);\n"
        "  return legacyAgentHref(tab);\n"
        "});\n"
        "out.push(sectionFromParam('advanced', false), sectionFromParam('advanced', true),\n"
        "         sectionFromParam('nope', true));\n"
        "console.log(JSON.stringify(out));\n",
    )
    assert got[:-3] == list(OLD_TABS.values())
    assert got[-3:] == ["general", "advanced", "general"]


# ─── the screen and the server answer the same ───────────────────────


BRANCH_CASES = [
    ("main", []), ("main", ["main"]), ("main-old", ["main"]),
    ("release/1.2", ["release/*"]), ("release/1.2/hotfix", ["release/*"]),
    ("releases/1", ["release/*"]), ("Main", ["main"]),
    ("staging", ["!master", "!main"]), ("main", ["!master", "!main"]),
    ("staging", ["staging", "!master", "!main"]), ("develop", ["staging", "!master", "!main"]),
    ("release/old", ["release/*", "!release/old"]), ("hotfix-9", ["hotfix-?"]),
    ("feature/a1", ["feature/[a-c]*"]), ("feature/d1", ["feature/[!a-c]*"]),
    ("weird[", ["weird["]), ("a.b", ["a?b"]), ("a+b", ["a+b"]), ("", ["main"]),
]


def test_the_branch_tester_answers_what_the_gate_answers(_web):
    from src.review.branch_patterns import match_branch

    got = _run_ts(
        _web, {"branches.ts": BRANCHES.read_text(encoding="utf-8")},
        'import { matchBranch } from "./branches";\n'
        f"const cases: any[] = {json.dumps(BRANCH_CASES)};\n"
        "console.log(JSON.stringify(cases.map(([b, p]) => {\n"
        "  const m = matchBranch(b, p); return [m.targeted, m.reason, m.pattern];\n"
        "})));\n",
    )
    for (branch, patterns), (targeted, reason, pattern) in zip(BRANCH_CASES, got, strict=True):
        py = match_branch(branch, patterns)
        assert (targeted, reason, pattern) == (py.targeted, py.reason, py.pattern), (branch, patterns)


def test_the_glob_translation_is_fnmatchs(_web):
    globs = ["*", "a*b", "?x", "[ab]c", "[!ab]c", "[a-c]?", "x[", "a.b", "a/b/**"]
    names = ["ab", "aXb", "zx", "bc", "cc", "b1", "x[", "a.b", "axb", "a/b/c/d", ""]
    got = _run_ts(
        _web, {"branches.ts": BRANCHES.read_text(encoding="utf-8")},
        'import { fnmatchCase } from "./branches";\n'
        f"const g: string[] = {json.dumps(globs)}; const n: string[] = {json.dumps(names)};\n"
        "console.log(JSON.stringify(g.map((p) => n.map((x) => fnmatchCase(x, p)))));\n",
    )
    want = [[fnmatch.fnmatchcase(x, p) for x in names] for p in globs]
    assert got == want


TEMPLATES_CASES = [
    "Reviewing {commit}", "{agents} on #{pr_number} ({files} files)", "{{literal}}",
    "{commit:>8}", "{files!r}", "{nope}", "{}", "open {", "close }", "a }} b {{ c",
    "{commit}}", "plain text",
]


def test_the_template_check_refuses_what_the_server_refuses(_web):
    from src.review.review_defaults import (
        MESSAGE_PLACEHOLDERS,
        message_template_error,
        render_message_template,
    )

    values = {"commit": "4f2c9a1", "agents": "defect", "files": "3", "pr_number": "7"}
    got = _run_ts(
        _web, {"templates.ts": TEMPLATES.read_text(encoding="utf-8")},
        'import { templateError, renderTemplate } from "./templates";\n'
        f"const cases: string[] = {json.dumps(TEMPLATES_CASES)};\n"
        f"const ph: string[] = {json.dumps(list(MESSAGE_PLACEHOLDERS))};\n"
        f"const v: any = {json.dumps(values)};\n"
        "console.log(JSON.stringify(cases.map((c) => {\n"
        "  const e = templateError(c, ph); return [e ? e.key : null, e ? null : renderTemplate(c, v)];\n"
        "})));\n",
    )
    for text, (err, rendered) in zip(TEMPLATES_CASES, got, strict=True):
        server = message_template_error(text)
        assert (err is not None) == (server is not None), (text, err, server)
        if server is None:
            assert rendered == render_message_template(text, values), text


# ─── an agent switch writes the right list ───────────────────────────


DEFAULTS = {"defect": True, "security": True, "business_logic": False}


def _switch(_web, cases):
    model = _code(MODEL)
    lifted = "\n\n".join(_lift(n, model, MODEL)
                         for n in ("participation", "sameSet", "switchAgent"))
    return _run_ts(
        _web, {},
        "type Draft = any; type Inheritance = any; type OwnValues = any;\n" + lifted + "\n\n"
        f"const cases: any[] = {json.dumps(cases)};\n"
        f"const defaults: any = {json.dumps(DEFAULTS)};\n"
        "console.log(JSON.stringify(cases.map(([own, inh, agent, on]) => {\n"
        "  const draft = { own };\n"
        "  const next = switchAgent(draft, { values: inh }, defaults, agent, on);\n"
        "  const lists = { ...own, ...next };\n"
        "  return [next, participation(lists.disabled_agents ?? inh.disabled_agents ?? [],\n"
        "    lists.enabled_agents ?? inh.enabled_agents ?? [], defaults)];\n"
        "})));\n",
    )


def test_an_opt_in_agent_is_switched_on_through_enabled_agents(_web):
    none = {"disabled_agents": None, "enabled_agents": None}
    inh = {"disabled_agents": [], "enabled_agents": []}
    (next_, runs), = _switch(_web, [[none, inh, "business_logic", True]])
    assert next_ == {"disabled_agents": None, "enabled_agents": ["business_logic"]}
    assert runs["business_logic"] is True


def test_a_default_agent_is_switched_off_through_disabled_agents(_web):
    none = {"disabled_agents": None, "enabled_agents": None}
    inh = {"disabled_agents": [], "enabled_agents": []}
    (next_, runs), = _switch(_web, [[none, inh, "security", False]])
    assert next_ == {"disabled_agents": ["security"], "enabled_agents": None}
    assert runs["security"] is False


def test_flipping_back_leaves_no_override(_web):
    """A list equal to what the scope inherits goes back to null — the
    repository keeps following the workspace instead of pinning a copy."""
    inh = {"disabled_agents": ["security"], "enabled_agents": []}
    own = {"disabled_agents": [], "enabled_agents": None}
    (next_, runs), = _switch(_web, [[own, inh, "security", False]])
    assert next_ == {"disabled_agents": None, "enabled_agents": None}
    assert runs["security"] is False


def test_switching_an_opt_in_agent_on_clears_its_off_entry(_web):
    """An off switch beats an on switch on the server, so turning an opt-in
    agent on must also take it out of the off list."""
    inh = {"disabled_agents": ["business_logic"], "enabled_agents": []}
    own = {"disabled_agents": None, "enabled_agents": None}
    (next_, runs), = _switch(_web, [[own, inh, "business_logic", True]])
    assert "business_logic" not in (next_["disabled_agents"] or [])
    assert runs["business_logic"] is True


# ─── words ───────────────────────────────────────────────────────────


def test_every_new_label_is_translated_and_uk_is_real():
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    keys = [k for k in en if k.startswith("reviewSettings.")]
    assert len(keys) > 150
    used = set()
    for path in [*SETTINGS.glob("*.tsx"), BRANCHES, TEMPLATES]:
        used |= set(re.findall(r'"(reviewSettings\.[\w.]+)"', _code(path)))
    assert used <= set(en), sorted(used - set(en))
    for key in [*keys, "nav.reviewSettings", "nav.more"]:
        assert uk[key] != en[key], f"{key} is not translated to Ukrainian"
    for key in keys:
        assert "—" not in en[key] and "—" not in uk[key], f"{key}: em dash"
