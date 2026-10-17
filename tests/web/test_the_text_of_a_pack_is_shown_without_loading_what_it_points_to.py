"""Opening a knowledge pack in the web must not make the viewer's browser call an
address the pack names.

A pack is somebody else's writing. Markdown images load from wherever the text
says, with the viewer's address and cookies' scope, just by being displayed. So:

* every place in the Codebase section that renders markdown asks for
  `images="alt"`, which shows an image as its alt text (and a link a click can
  follow) and emits no `<img>` — the behaviour itself is run in
  `web/components/markdown-images.test.ts` under vitest;
* the shared renderer has that mode, and its default is unchanged for the pages
  that show the assistant's own answers.
"""

from __future__ import annotations

import re

from tests.web.test_a_configured_reasoning_setting_survives_the_save import WEB

SECTIONS = [WEB / "components" / "codebase", WEB / "app" / "(app)" / "codebase"]
RENDERER = WEB / "components" / "markdown.tsx"


def _sources():
    for root in SECTIONS:
        yield from sorted(root.rglob("*.tsx"))


def test_every_markdown_in_the_codebase_section_shows_images_as_text():
    uses = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"<Markdown\b[^>]*>", text):
            uses.append((path.name, match.group(0)))
    assert uses, "the markdown viewer was not found"
    for name, tag in uses:
        assert 'images="alt"' in tag, f'{name}: {tag} would load the images a pack names'


def test_the_section_does_not_render_images_or_raw_html_itself():
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert "<img" not in text, path.name
        assert "dangerouslySetInnerHTML" not in text, path.name
        assert "rehype-raw" not in text and "rehypeRaw" not in text, path.name


def test_the_renderer_has_the_text_mode_and_keeps_images_for_everyone_else():
    code = RENDERER.read_text(encoding="utf-8")
    assert 'images?: "show" | "alt"' in code
    assert 'images = "show"' in code
    assert 'images === "alt"' in code
    assert "rehype-raw" not in code and "rehypeRaw" not in code and "dangerouslySetInnerHTML" not in code
