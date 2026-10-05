"""The /review-settings scope panel is a tree whose branches fold, and the
repository's full name never covers the panel's own text.

What is pinned:

  * Global folds like a repository does, and shows the same sections (minus
    the repository-only ones) with its own counts;
  * every fold is a real button with `aria-expanded` and `aria-controls`, in a
    `tree` of `treeitem`s, with ←/→ folding and unfolding;
  * the chevron folds and does nothing else; the title opens the scope and
    unfolds it; the open scope unfolds by itself and the chevron can still
    fold it;
  * what is unfolded is remembered through one storage helper whose every
    `localStorage` access sits in a try block, with a safe default for
    nothing stored or garbage stored;
  * the full-name bubble appears only when the slug is cut off, and only
    beside the panel — the old `title` bubble rose over "Per repository".

The decision functions are lifted out of the component, compiled and run —
not grepped — so a regression in the logic fails here rather than on screen.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _lift,
    _strip_comments,
)

NAV = WEB / "components" / "review-settings" / "scope-nav.tsx"
STORE = WEB / "lib" / "use-stored-string.ts"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


# ─── structure ───────────────────────────────────────────────────────


def test_every_chevron_is_a_button_with_expanded_and_controls():
    code = _code(NAV)
    chevron = code[code.index("function Chevron("):code.index("\n}\n", code.index("function Chevron("))]
    assert '<button' in chevron and 'type="button"' in chevron
    assert "aria-expanded={open}" in chevron
    assert "aria-controls={controls}" in chevron
    # The chevron folds; it is never a link that navigates.
    assert "<Link" not in chevron and "onNavigate" not in chevron
    # One for Global, one for the Per repository group, one per repository.
    assert len(re.findall(r"<Chevron\s", code)) == 3
    assert "toggle(GLOBAL_KEY)" in code and "toggle(REPOS_KEY)" in code and "toggle(key)" in code


def test_the_panel_has_tree_semantics_and_arrow_keys():
    code = _code(NAV)
    assert 'role="tree"' in code
    assert code.count('role="treeitem"') >= 4, "global, the group, a repo and a section"
    assert 'role="group"' in code
    assert re.search(r'role="treeitem"[^>]*aria-expanded=\{globalOpen\}', code)
    assert re.search(r'role="treeitem"[^>]*aria-expanded=\{reposOpen\}', code)
    assert "aria-expanded={open}" in code
    for key in ('"ArrowRight"', '"ArrowLeft"', '"ArrowDown"', '"ArrowUp"'):
        assert key in code, f"{key} is not handled"
    assert "onKeyDown={onTreeKeyDown}" in code


def test_global_unfolds_into_the_same_sections_with_its_own_counts():
    code = _code(NAV)
    assert "sectionList(null, globalCounts, isGlobal, 2)" in code
    assert "sectionsOfFields(overview?.workspace.set_fields" in code, (
        "Global's per-section counts must come from the overview when Global "
        "is not the open scope")
    # Global is not gated on being open any more — it folds like a repo.
    assert "isGlobal && sectionList(" not in code


def test_the_title_opens_and_unfolds():
    code = _code(NAV)
    assert "if (isGlobal) setOpen(GLOBAL_KEY, true)" in code
    assert "if (active) setOpen(key, true)" in code


def test_collapse_all_is_offered_for_several_repositories():
    code = _code(NAV)
    assert "repos.length >= BULK_FROM" in code
    assert 'reviewSettings.nav.collapseAll' in code and 'reviewSettings.nav.expandAll' in code


def test_folding_is_animated_and_respects_reduced_motion():
    code = _code(NAV)
    assert "useReducedMotion()" in code
    assert re.search(r"reduce \? \{ duration: 0 \}", code)
    assert 'height: "auto"' in code


def test_active_rows_use_selected_and_counts_use_attention():
    code = _code(NAV)
    assert "--color-selected" in code
    pill = code[code.index("function CountPill("):code.index("\n}\n", code.index("function CountPill("))]
    assert "--color-attention-soft" in pill and "--color-attention)" in pill
    assert "--color-primary-soft" not in code, "a count drifted back to the primary tone"
    assert code.count("<CountPill") == 3


def test_long_repository_names_truncate():
    code = _code(NAV)
    label = re.search(r'data-label=""\s*className=\{cn\("([^"]*)"', code)
    assert label and "truncate" in label.group(1) and "min-w-0" in label.group(1)


# ─── storage ─────────────────────────────────────────────────────────


def test_the_nav_reaches_storage_only_through_the_helper():
    code = _code(NAV)
    assert "localStorage" not in code
    assert "useStoredString(" in code
    assert "session?.user?.id" in code, "the remembered state is per person"


def test_every_localstorage_access_is_in_a_try_block():
    lines = _code(STORE).splitlines()
    hits = [i for i, ln in enumerate(lines) if "localStorage" in ln]
    assert len(hits) >= 2, "the helper no longer reads AND writes storage"
    for i in hits:
        before = "\n".join(lines[max(0, i - 2):i])
        assert "try {" in before, f"unguarded localStorage at line {i + 1}: {lines[i]}"
    assert "useSyncExternalStore" in "\n".join(lines), (
        "read as a store, not copied into state by a mount effect")


# ─── the full-name bubble ────────────────────────────────────────────


def test_no_native_title_on_repository_rows():
    code = _code(NAV)
    assert "title={r.full_name}" not in code
    assert "fullNameTip({" in code
    assert "createPortal(" in code, "the bubble must escape the scrolling panel"
    assert 'role="tooltip"' in code


# ─── the decisions, run ──────────────────────────────────────────────

LIFTED = ("TipBox", "TipMeasure", "parseExpanded", "isScopeOpen", "fullNameTip")

TIP_BASE = {"labelScrollWidth": 180, "labelClientWidth": 120, "rowTop": 300,
            "rowHeight": 32, "panelRight": 288, "viewportWidth": 1440}

CASES = [
    {"name": "parse_nothing", "fn": "parse", "arg": None},
    {"name": "parse_garbage", "fn": "parse", "arg": "{not json"},
    {"name": "parse_not_a_list", "fn": "parse", "arg": "{\"a\":1}"},
    {"name": "parse_empty_is_kept", "fn": "parse", "arg": "[]"},
    {"name": "parse_filters", "fn": "parse", "arg": "[1, \"repo:a\", null, \"global\"]"},
    {"name": "open_explicit", "fn": "open", "arg": ["repo:a", ["repo:a"], [], []]},
    {"name": "open_auto", "fn": "open", "arg": ["repo:a", [], ["repos", "repo:a"], []]},
    {"name": "open_auto_dismissed", "fn": "open", "arg": ["repo:a", [], ["repos", "repo:a"], ["repo:a"]]},
    {"name": "open_neither", "fn": "open", "arg": ["repo:b", [], ["repos", "repo:a"], []]},
    {"name": "tip_not_truncated", "fn": "tip",
     "arg": {**TIP_BASE, "labelScrollWidth": 120, "labelClientWidth": 120}},
    {"name": "tip_truncated", "fn": "tip", "arg": TIP_BASE},
    {"name": "tip_no_room", "fn": "tip",
     "arg": {**TIP_BASE, "panelRight": 380, "viewportWidth": 390}},
    {"name": "tip_wide_screen", "fn": "tip", "arg": {**TIP_BASE, "viewportWidth": 4000}},
]


def _harness() -> str:
    src = _strip_comments(NAV.read_text(encoding="utf-8"))
    lifted = "\n\n".join(_lift(name, src, NAV) for name in LIFTED)
    return (
        lifted
        + "\n\nconst cases: any[] = " + json.dumps(CASES) + ";\n"
        "const out = cases.map((c) => {\n"
        "  let got: any;\n"
        "  if (c.fn === 'parse') got = [...parseExpanded(c.arg)].sort();\n"
        "  else if (c.fn === 'open') got = isScopeOpen(c.arg[0], new Set(c.arg[1]), c.arg[2], c.arg[3]);\n"
        "  else got = fullNameTip(c.arg);\n"
        "  return { name: c.name, got };\n"
        "});\n"
        "console.log(JSON.stringify(out));\n"
    )


@pytest.fixture(scope="module")
def decisions(tmp_path_factory) -> dict[str, object]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    tmp = tmp_path_factory.mktemp("scope_nav")
    ts = tmp / "harness.ts"
    ts.write_text(_harness(), encoding="utf-8")
    build = subprocess.run(
        [str(TSC), str(ts), "--target", "es2020", "--module", "commonjs",
         "--strict", "--outDir", str(tmp)],
        capture_output=True, text=True, timeout=300,
    )
    js = tmp / "harness.js"
    if not js.exists() or build.returncode != 0:
        pytest.fail(f"harness did not compile:\n{build.stdout}\n{build.stderr}")
    run = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return {row["name"]: row["got"] for row in json.loads(run.stdout)}


def test_the_harness_actually_ran(decisions):
    assert set(decisions) == {c["name"] for c in CASES}, decisions


def test_stored_state_has_a_safe_default(decisions):
    assert decisions["parse_nothing"] == ["repos"]
    assert decisions["parse_garbage"] == ["repos"]
    assert decisions["parse_not_a_list"] == ["repos"]
    assert decisions["parse_empty_is_kept"] == [], "folding everything is a choice"
    assert decisions["parse_filters"] == ["global", "repo:a"]


def test_the_open_scope_unfolds_by_itself_and_can_still_be_folded(decisions):
    assert decisions["open_explicit"] is True
    assert decisions["open_auto"] is True
    assert decisions["open_auto_dismissed"] is False
    assert decisions["open_neither"] is False


def test_the_full_name_shows_only_when_truncated(decisions):
    assert decisions["tip_not_truncated"] is None


def test_the_full_name_never_covers_the_panel(decisions):
    tip = decisions["tip_truncated"]
    assert tip is not None
    # Beside the panel, never over it: the old bubble rose above the row and
    # covered the "Per repository" heading.
    assert tip["left"] > TIP_BASE["panelRight"]
    # Centred on the row it names, not above it.
    assert tip["top"] == TIP_BASE["rowTop"] + TIP_BASE["rowHeight"] / 2
    assert tip["left"] + tip["maxWidth"] <= TIP_BASE["viewportWidth"]
    assert decisions["tip_no_room"] is None, "no room beside the panel: no bubble over the text"
    assert decisions["tip_wide_screen"]["maxWidth"] <= 448


# ─── copy ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["sectionsOf", "repoList", "collapseAll", "expandAll"])
def test_the_new_labels_exist_in_every_locale(key):
    full = f"reviewSettings.nav.{key}"
    assert f't("{full}"' in _code(NAV)
    for p in MESSAGES.glob("*.json"):
        assert full in json.loads(p.read_text(encoding="utf-8")), f"{p.name} lacks {full}"
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    en = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
    assert uk[full] != en[full], f"{full} is not translated into Ukrainian"
