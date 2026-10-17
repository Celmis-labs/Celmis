"""Every word the Codebase pages show is a key, every key is used, and every
language has it.

`test_ui_strings_are_translatable` catches a sentence typed into JSX and
`test_locale_completeness` catches a key one language lacks. Neither catches
the two failures in between, which are how a feature ships half-finished:

* a page asks for `codebase.pack.tab.usage` and no file defines it — the page
  shows the key id, in sixteen languages, and no test fails;
* a key outlives the control that used it — a sentence nobody sees, translated
  sixteen times.

The pages also build keys at run time (`codebase.kind.${kind}`), so the
families those expansions reach are read off the code that enumerates them (the
`PackItemKind` union, `PACK_AUDIENCES`, the table of who sees what, the agent's
tool list) and held against the message files: a new audience, a new kind or a
new tool without its words fails here.

Placeholders are checked at the call site as well as across languages: a page
that passes `{ count }` to a sentence which has no `{count}` renders without the
number, and one that forgets to pass it renders the braces.

`en` and `uk` are written for real. The other fourteen carry English until they
are translated, so this test does not ask them to differ — only to exist.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    WEB,
    _strip_comments,
)

MESSAGES = WEB / "lib" / "i18n" / "messages"
APP = WEB / "app" / "(app)" / "codebase"
COMPONENTS = WEB / "components" / "codebase"
SHELL = WEB / "components" / "app-shell.tsx"
TABS = WEB / "components" / "section-tabs.tsx"
LIB = WEB / "lib" / "codebase.ts"
API = WEB / "lib" / "api.ts"

# `lib/codebase.ts` names the private-pack key (`audienceKey`), so it is read too.
SOURCES = sorted([*APP.rglob("*.tsx"), *COMPONENTS.rglob("*.tsx"), SHELL, TABS, LIB])

#: Keys whose uk text is legitimately the same as en: the section's own name,
#: and example values that show the shape of what to type.
SAME_IN_UK = {
    "nav.codebase",
    "codebase.title",
    "codebase.create.slugPlaceholder",
    "codebase.sharing.personPlaceholder",
}

PLACEHOLDER = re.compile(r"\{(\w+)\}")
KEY = re.compile(r"""["'`]((?:nav\.codebase|codebase\.[\w.$\{\}]*))["'`]""")


def _code(path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _en() -> dict[str, str]:
    return json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))


def _owned(keys) -> set[str]:
    return {k for k in keys if k == "nav.codebase" or k.startswith("codebase.")}


def _union(path, name: str) -> list[str]:
    m = re.search(rf"type {name} =\s*([^;]+);", _code(path))
    assert m, f"{name} is not declared in {path.name}"
    return re.findall(r'"(\w+)"', m.group(1))


def _array(path, name: str) -> list[str]:
    m = re.search(rf"{name}[^=]*=\s*\[([^\]]*)\]", _code(path))
    assert m, f"{name} is not declared in {path.name}"
    return re.findall(r'"(\w+)"', m.group(1))


# ─── what the code asks for ──────────────────────────────────────────


def _literals() -> tuple[set[str], set[str]]:
    """(plain keys, template patterns with `*` for each `${…}`) across the
    section's sources."""
    plain: set[str] = set()
    patterns: set[str] = set()
    for source in SOURCES:
        for key in KEY.findall(_code(source)):
            if "$" in key:
                patterns.add(re.sub(r"\$\{[^}]*\}", "*", key))
            else:
                plain.add(key)
    return plain, patterns


def _families() -> dict[str, list[str]]:
    audiences = _array(LIB, "PACK_AUDIENCES")
    who = re.findall(r'\{ who: "(\w+)"', _code(COMPONENTS / "audience.tsx"))
    return {
        "codebase.kind.*": _union(API, "PackItemKind"),
        "codebase.kindPlural.*": _array(LIB, "KIND_ORDER"),
        "codebase.kindDesc.*": re.findall(
            r'\{ key: "(\w+)", icon', _code(APP / "page.tsx")),
        "codebase.pack.tab.*": _union(APP / "packs" / "[id]" / "page.tsx", "Tab"),
        "codebase.sharing.state.*": _union(LIB, "InviteState"),
        "codebase.diff.*": re.findall(
            r'\{ key: "(\w+)", variant', _code(COMPONENTS / "versions-tab.tsx")),
        "codebase.permission.*": _array(LIB, "PACK_PERMISSIONS"),
        "codebase.connect.tool.*": _array(APP / "connect" / "page.tsx", "AGENT_STEPS"),
        "codebase.guide.*": who,
        "codebase.audience.*": audiences,
        "codebase.audience.*.short": audiences,
        "codebase.audience.*.desc": audiences,
    }


def _expand() -> set[str]:
    return {pattern.replace("*", name)
            for pattern, names in _families().items() for name in names}


def _referenced() -> set[str]:
    plain, _ = _literals()
    return plain | _expand()


# ─── the three ways a key goes wrong ─────────────────────────────────


def test_the_code_that_enumerates_each_family_was_found():
    """Guards the guard: a parser that returns nothing makes every family check
    below vacuous."""
    families = _families()
    for pattern, names in families.items():
        assert names, f"nothing found behind {pattern}"
    assert set(families["codebase.kind.*"]) >= {"skill", "api", "checklist", "note", "doc"}
    assert set(families["codebase.audience.*"]) == {"workspace", "teams", "restricted"}
    assert len(families["codebase.guide.*"]) == 7


def test_every_key_built_at_run_time_belongs_to_a_known_family():
    _, patterns = _literals()
    assert patterns == set(_families()), (
        "a page builds a key this test does not know how to enumerate: "
        f"{sorted(patterns - set(_families()))}; or a family lost its last use: "
        f"{sorted(set(_families()) - patterns)}")


def test_every_key_the_pages_ask_for_is_defined():
    missing = sorted(_referenced() - set(_en()))
    assert not missing, f"asked for in code, defined nowhere: {missing}"


def test_no_key_outlives_the_control_that_used_it():
    orphans = sorted(_owned(_en()) - _referenced())
    assert not orphans, f"defined but never used: {orphans}"


# ─── placeholders at the call site ───────────────────────────────────


def _names(obj: str) -> set[str]:
    """The keys of an object literal's text (without the outer braces):
    `a: 1, b, c: f(x, y)` -> {a, b, c}."""
    parts, depth, cur, quote = [], 0, [], None
    for ch in obj:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    names = set()
    for part in parts:
        m = re.match(r"\s*(\w+)\s*(?::|$)", part)
        if m:
            names.add(m.group(1))
    return names


def _object_after(code: str, start: int) -> str | None:
    """The text of the `{…}` that starts at `start`, or None."""
    if code[start:start + 1] != "{":
        return None
    depth, quote = 0, None
    for i in range(start, len(code)):
        ch = code[i]
        if quote:
            if ch == quote and code[i - 1] != "\\":
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
            if depth == 0:
                return code[start + 1:i]
    return None


def _calls() -> list[tuple[str, set[str]]]:
    """Every `t("key", { … })` and `t("key")` in the section, with the names
    passed (empty for a bare call). Template keys keep their `${…}`."""
    found: list[tuple[str, set[str]]] = []
    for source in SOURCES:
        code = _code(source)
        for m in re.finditer(r"\bt\(\s*([`\"])(nav\.codebase|codebase\.[^`\"]*)\1\s*([,)])", code):
            key, after = m.group(2), m.group(3)
            if after == ")":
                found.append((key, set()))
                continue
            obj = _object_after(code, m.end() + len(code[m.end():]) - len(code[m.end():].lstrip()))
            found.append((key, _names(obj) if obj is not None else set()))
    return found


def test_the_call_scan_finds_the_calls():
    calls = dict(_calls())
    assert calls.get("codebase.upload.done") == {"number", "files", "skills"}
    assert calls.get("codebase.diff.${list.key}") == {"count"}
    assert calls.get("codebase.empty.descReader") == {"roles"}
    assert len(calls) > 60


def test_every_call_passes_exactly_the_placeholders_its_sentence_has():
    en = _en()
    wrong = []
    for key, passed in _calls():
        keys = ([key] if "$" not in key
                else [k for k in _expand() if re.fullmatch(
                    re.sub(r"\\\$\\\{[^}]*\\\}", r"[A-Za-z]+", re.escape(key)), k)])
        assert keys, f"{key} matches no key"
        for k in keys:
            wanted = set(PLACEHOLDER.findall(en[k]))
            if wanted != passed:
                wrong.append((k, sorted(wanted), sorted(passed)))
    assert not wrong, f"(key, sentence has, call passes): {wrong}"


# ─── the languages ───────────────────────────────────────────────────


def _locales() -> list[str]:
    return sorted(p.stem for p in MESSAGES.glob("*.json"))


@pytest.mark.parametrize("locale", _locales())
def test_every_language_has_every_codebase_key_with_the_same_placeholders(locale):
    en = _en()
    other = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    mine = _owned(en)
    assert len(mine) > 250, "the section's keys went missing"
    assert not (mine - set(other)), sorted(mine - set(other))[:10]
    for key in mine:
        assert sorted(PLACEHOLDER.findall(en[key])) == sorted(PLACEHOLDER.findall(other[key])), key
        assert other[key].strip(), f"{key} is empty in {locale}"


def test_ukrainian_is_written_not_copied():
    en = _en()
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    copied = sorted(k for k in _owned(en) - SAME_IN_UK if uk[k] == en[k])
    assert not copied, f"uk is still the English text: {copied}"
    no_cyrillic = sorted(k for k in _owned(en) - SAME_IN_UK
                         if not re.search(r"[А-Яа-яЄєІіЇїҐґ]", uk[k]))
    assert not no_cyrillic, f"uk has no Ukrainian letters: {no_cyrillic}"


def test_the_section_names_no_assistant_after_a_vendor():
    """The terms allow saying a product runs Claude Code; they do not allow a
    feature to be named after it. The section is `Codebase`, and the only
    place an assistant is named is the sentence that says which command to
    run in it."""
    en = _en()
    for key in _owned(en):
        if "Claude" in en[key] or "Anthropic" in en[key]:
            assert key == "codebase.connect.plugin.note", key
    for key in ("nav.codebase", "codebase.title", "codebase.tab.packs",
                "codebase.tab.search", "codebase.tab.connect"):
        assert "Claude" not in en[key]
