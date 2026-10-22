"""A first visit is in the browser's language and the system's theme, with no flash.

Before this, the language came from one place only, the `locale` cookie, so
every first visit was English: a browser set to Ukrainian opened an English
login page. The theme was nearly right (an inline script in <head> read the
system's preference when nothing was saved) but it read it once, so a laptop
that went dark at sunset stayed light until it was reloaded.

The matching itself is pure and is held by vitest, in
`web/lib/i18n/locales.test.ts` and `web/lib/theme.test.ts`. This file holds the
seams those tests cannot see, which is where the behaviour was lost before:

* THE SERVER MUST BE ABLE TO RUN THE DETECTION. The root layout paints the
  first byte, so the locale list, the matching and the theme script live in
  plain modules. Put them back into a "use client" file and the layout gets a
  reference where it needs a string or a function.
* THE LIST IS THE FILES. Detection can only offer a language there is a
  dictionary for, and a dictionary only exists once its code is in the list.
* DETECTION IS NEVER A SAVED CHOICE. If the first visit wrote the `locale`
  cookie, the browser's language would be frozen at the first visit and
  changing it later would do nothing. Only the switcher writes the cookie, only
  the toggle writes the theme.
* THE FIRST PAINT. The theme script runs in <head>, before <body>, and the
  language is in the HTML the server sends.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
LAYOUT = WEB / "app" / "layout.tsx"
LOCALES = WEB / "lib" / "i18n" / "locales.ts"
INDEX = WEB / "lib" / "i18n" / "index.tsx"
THEME = WEB / "lib" / "theme.ts"
TOGGLE = WEB / "components" / "theme-toggle.tsx"
SYNC = WEB / "components" / "theme-sync.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"


def _code(path: Path) -> str:
    """The file minus its prose: these modules explain themselves at length,
    and a name surviving in a comment must not count as the name in the code."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _web_sources() -> list[Path]:
    return sorted(
        p
        for folder in ("app", "components", "lib", "ee")
        for pattern in ("*.ts", "*.tsx")
        for p in (WEB / folder).rglob(pattern)
        if "node_modules" not in p.parts and ".test." not in p.name
    )


# ─── the list of languages is the dictionaries ───────────────────────


def test_the_detectable_languages_are_the_dictionaries_there_are():
    body = _code(LOCALES)
    listed = re.search(r"export const LOCALES: Locale\[\] = \[(.*?)\];", body, flags=re.S)
    assert listed, "LOCALES changed shape; this guard reads nothing"
    codes = set(re.findall(r'"([a-z]{2})"', listed.group(1)))
    files = {p.stem for p in MESSAGES.glob("*.json")}
    assert codes == files, (
        f"a dictionary with no entry in LOCALES is never offered or detected: "
        f"{sorted(files - codes)}; an entry with no dictionary is English under "
        f"another name: {sorted(codes - files)}"
    )
    union = re.search(r"export type Locale =(.*?);", body, flags=re.S)
    assert union and set(re.findall(r'"([a-z]{2})"', union.group(1))) == codes


def test_english_is_what_nothing_matching_falls_back_to():
    assert re.search(r'export const DEFAULT_LOCALE: Locale = "en";', _code(LOCALES))
    assert (MESSAGES / "en.json").exists()


# ─── plain modules, so the server can run them ───────────────────────


@pytest.mark.parametrize("path", [LOCALES, THEME], ids=lambda p: p.name)
def test_the_detection_lives_in_a_module_the_server_can_import(path):
    src = path.read_text(encoding="utf-8")
    assert '"use client"' not in src and "'use client'" not in src
    assert not re.search(r'^import .* from "', _code(path), flags=re.M), (
        f"{path.name} imports something; if that is a client module the layout "
        "gets a reference instead of the value"
    )


def test_the_provider_gets_its_locale_list_from_the_plain_module():
    body = _code(INDEX)
    assert re.search(r'from "\./locales"', body)
    assert "export const LOCALES" not in body, "a second copy of the list"
    assert "export const DEFAULT_LOCALE" not in body


# ─── the first paint ─────────────────────────────────────────────────


def test_the_root_layout_decides_the_language_from_the_cookie_then_the_browser():
    body = _code(LAYOUT)
    assert 'from "@/lib/i18n/locales"' in body and "detectLocale(" in body
    assert 'headerStore.get("accept-language")' in body or 'get("accept-language")' in body
    assert 'cookieStore.get("locale")' in body
    assert " as Locale" not in body, "a cookie is cast to a locale again, unchecked"
    assert "|| DEFAULT_LOCALE" not in body
    assert "lang={locale}" in body, "the language is not in the HTML the server sends"
    assert "askBrowser={source" in body, "the client is never asked when the server had nothing"


def test_a_language_outside_the_bundle_reaches_the_first_paint_from_the_server():
    """Only en and uk are in the client bundle. Without the dictionary on the
    server, a German browser got `lang="de"` around English text until the
    download finished: the vitest case renders it, this holds the wiring."""
    layout, providers, index = _code(LAYOUT), _code(WEB / "components" / "providers.tsx"), _code(INDEX)
    assert 'from "@/lib/i18n/server-dict"' in layout and "serverDictionary(locale)" in layout
    assert "initialDict={dictionary}" in layout
    assert "initialDict" in providers and "initialDict={initialDict}" in providers
    assert "initialDict" in index
    server = (WEB / "lib" / "i18n" / "server-dict.ts").read_text(encoding="utf-8")
    assert '"use client"' not in server, "the layout would get a reference, not the dictionary"


def test_the_layout_takes_the_theme_script_from_the_plain_module():
    body = _code(LAYOUT)
    assert 'from "@/lib/theme"' in body
    assert "theme-toggle" not in body, "THEME_INIT_SCRIPT is back in a client module"


def test_the_theme_script_runs_in_head_before_the_body_paints():
    body = _code(LAYOUT)
    head = body.index("<head>")
    script = body.index("__html: THEME_INIT_SCRIPT")
    assert head < script < body.index("<body"), "the theme is decided after the first paint"
    assert "suppressHydrationWarning" in body, "the script's class on <html> is a hydration mismatch"


def test_the_theme_follows_the_system_on_every_page_not_only_where_the_toggle_is():
    layout = _code(LAYOUT)
    assert "<ThemeSync />" in layout
    sync = _code(SYNC)
    assert sync.lstrip().startswith('"use client"')
    assert "followSystemTheme()" in sync
    theme = _code(THEME)
    assert 'addEventListener("change"' in theme and 'addEventListener("storage"' in theme
    assert "removeEventListener" in theme, "the listeners are never taken off"


# ─── detection is not a saved choice ─────────────────────────────────


def test_only_the_switcher_writes_the_language_cookie():
    writers = [
        p.relative_to(WEB).as_posix()
        for p in _web_sources()
        if re.search(r"document\.cookie\s*=\s*`locale=", _code(p))
    ]
    assert writers == ["lib/i18n/index.tsx"], writers
    body = _code(INDEX)
    cookie = body.index("document.cookie = `locale=")
    assert body.rindex("const setLocale", 0, cookie) > body.rindex("askBrowser", 0, cookie), (
        "the cookie is written outside setLocale, in the browser-detection path"
    )
    assert "document.cookie" not in _code(LOCALES)


def test_the_browser_fallback_does_not_go_through_setlocale():
    body = _code(INDEX)
    effect = body[body.index("if (!askBrowser) return;"):]
    effect = effect[: effect.index("}, [askBrowser]);")]
    assert "setLocaleState(detected)" in effect
    assert "setLocale(" not in effect, "a language the browser chose is saved as the person's"


def test_only_the_toggle_saves_a_theme_and_only_light_or_dark():
    writers = [
        p.relative_to(WEB).as_posix()
        for p in _web_sources()
        if re.search(r"localStorage\.setItem\(", _code(p))
        and re.search(r"setItem\(\s*(THEME_KEY|[\"']theme[\"'])", _code(p))
    ]
    assert writers == ["components/theme-toggle.tsx"], writers
    assert re.search(r'setItem\(THEME_KEY, next \? "dark" : "light"\)', _code(TOGGLE))


def test_a_stored_value_that_is_not_light_or_dark_follows_the_system():
    # The pure function is held by vitest; this pins that nothing else decides.
    assert re.search(r'saved === "light" \|\| saved === "dark"', _code(THEME))
    assert not re.search(r"classList\.(toggle|add|remove)\(\s*[\"']dark", _code(TOGGLE)), (
        "the toggle sets the class by hand again, past applyTheme"
    )

