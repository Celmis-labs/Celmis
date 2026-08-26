"""Generated notes, assembled into a document someone can send on.

The notes were always documentation — markdown, one file per module, written
while the search index was built. Nothing read them back, so the product asked
people to "generate the vault" and then showed them nothing, while implying
Q&A depended on it. It does not: with source display on, answers are built from
the code itself.

Two properties are worth pinning. The document must not lose the content it
assembles, and the .docx must be a file Word actually opens — hand-written,
because python-docx pulls in lxml, a C extension, into an image that has
already run out of disk once.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

import pytest

from src.docs.export import Note, to_docx, to_markdown

NOTES = [
    Note(
        path="src/api/routers/qa.py",
        title="Q&A router",
        body=(
            "## Purpose\n\nStreams answers over SSE.\n\n"
            "- reads the vault\n- falls back to *grep*\n\n"
            "```python\nasync def ask(chat_id: str):\n    return 1 < 2 & 3\n```\n\n"
            "See [the guide](https://example.test/guide)."
        ),
    ),
    Note(path="src/llm/errors.py", title="Provider errors",
         body="Maps a provider failure to a code the UI translates."),
]


def test_markdown_keeps_every_note():
    md = to_markdown("acme-api", NOTES, generated_at="2026-08-14")
    for note in NOTES:
        assert note.title in md
        assert note.path in md
        assert note.body.splitlines()[0] in md


def test_markdown_carries_a_table_of_contents_that_links():
    """A document pasted into a wiki keeps working because the anchors follow
    the slug rules every markdown renderer copied from GitHub."""
    md = to_markdown("acme-api", NOTES)
    assert "## Contents" in md
    assert "(#qa-router)" in md          # "Q&A router" → punctuation dropped
    assert "(#provider-errors)" in md


def test_a_single_note_gets_no_contents_section():
    """A table of contents with one entry is noise."""
    assert "## Contents" not in to_markdown("acme-api", NOTES[:1])


def test_the_docx_is_a_readable_package():
    """Every part present, every part valid XML — the two ways a hand-written
    .docx fails to open at all."""
    data = to_docx("acme-api", NOTES, generated_at="2026-08-14")
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert archive.testzip() is None
    for required in (
        "[Content_Types].xml", "_rels/.rels",
        "word/_rels/document.xml.rels", "word/styles.xml", "word/document.xml",
    ):
        assert required in archive.namelist(), required
    for name in archive.namelist():
        ET.fromstring(archive.read(name))


def test_code_that_looks_like_markup_survives_intact():
    """`1 < 2 & 3` inside a code block is the shortest way to produce a .docx
    Word refuses to open, if the escaping is wrong."""
    doc = _document(to_docx("acme-api", NOTES))
    assert "1 &lt; 2 &amp; 3" in doc


def test_indentation_inside_a_code_block_is_preserved():
    """Without xml:space the leading spaces vanish and a Python listing
    becomes unreadable."""
    doc = _document(to_docx("acme-api", NOTES))
    assert 'xml:space="preserve"' in doc
    assert "    return" in doc


def test_headings_become_headings_and_code_becomes_code():
    doc = _document(to_docx("acme-api", NOTES))
    assert 'w:val="Heading1"' in doc     # note title
    assert 'w:val="Heading2"' in doc     # "## Purpose" inside the body
    assert 'w:val="Code"' in doc


def test_a_link_keeps_its_text_and_drops_its_url():
    """A URL read aloud in a printed document helps nobody."""
    doc = _document(to_docx("acme-api", NOTES))
    assert "the guide" in doc
    assert "example.test" not in doc


@pytest.mark.parametrize("body", ["", "   \n\n  ", "# only a heading"])
def test_a_thin_note_does_not_break_the_document(body: str):
    data = to_docx("acme-api", [Note(path="x.py", title="X", body=body)])
    ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))


def _document(data: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
