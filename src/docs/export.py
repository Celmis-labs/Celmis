"""Turn generated notes into a document someone can send to a colleague.

The notes were always documentation — markdown with frontmatter, one file per
module, written by the model while it built the search index. Nothing read them
back. They fed Qdrant and then existed only as a side effect, which is why the
product kept telling people to "generate the vault" without ever showing what
that produced.

Two formats here, chosen for what they cost:

  * Markdown — the notes are already markdown, so this is assembly, not
    conversion: a title, a table of contents, then each note under its own
    heading. Nothing can be lost in translation because nothing is translated.

  * DOCX — written by hand rather than through python-docx, whose lxml
    dependency is a C extension in an image that has already run out of disk
    once. A .docx is a zip of XML with a fixed skeleton, and the subset needed
    here (headings, paragraphs, code blocks, bullets) is small enough to emit
    directly and verify.

PDF is deliberately absent. Doing it server-side means weasyprint and its
cairo/pango system libraries; the documentation page prints to PDF from the
browser with the same typography it shows on screen, which is both better and
free.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class Note:
    """One generated note: where it came from, and what it says."""

    path: str
    title: str
    body: str


def to_markdown(repo: str, notes: list[Note], *, generated_at: str = "") -> str:
    """One document: heading, contents, then every note in path order."""
    lines = [f"# {repo}", ""]
    if generated_at:
        lines += [f"_Generated {generated_at}_", ""]
    if len(notes) > 1:
        lines += ["## Contents", ""]
        for note in notes:
            # Anchors are how a table of contents survives being pasted into
            # anything that renders markdown; GitHub's slug rules are the ones
            # every renderer copied.
            lines.append(f"- [{note.title}](#{_anchor(note.title)})")
        lines.append("")
    for note in notes:
        lines += [f"## {note.title}", "", f"`{note.path}`", "", note.body.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def _anchor(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    return re.sub(r"[\s_]+", "-", slug).strip("-")


# ─── DOCX ────────────────────────────────────────────────────────────

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

#: Only the styles the converter emits. Word supplies its own defaults for
#: everything else, and a shorter styles part is one less thing to get wrong.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:pPr><w:spacing w:after="240"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="56"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="360" w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="280" w:after="100"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:pPr><w:outlineLvl w:val="2"/><w:spacing w:before="240" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/>
<w:pPr><w:shd w:val="clear" w:fill="F4F4F4"/><w:spacing w:after="0"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>
</w:styles>"""


def _para(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if not text:
        return f"<w:p>{props}</w:p>"
    # xml:space="preserve" keeps the indentation of a code line, which is the
    # one thing a reader needs from a code block in a document.
    return (
        f"<w:p>{props}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"
    )


def _markdown_to_paragraphs(md: str) -> list[str]:
    """A deliberately small subset: headings, code fences, bullets, text.

    Inline emphasis is stripped rather than rendered. A half-applied bold that
    swallows the rest of a paragraph reads worse than plain text, and these
    documents are read for what they say.
    """
    out: list[str] = []
    in_code = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append(_para(line, "Code"))
            continue
        if not line.strip():
            out.append(_para(""))
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            out.append(_para(_inline(heading.group(2)), f"Heading{level}"))
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            out.append(_para(f"•  {_inline(bullet.group(1))}"))
            continue
        out.append(_para(_inline(line)))
    return out


def _inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # links → their text
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    return text.replace("`", "")


def to_docx(repo: str, notes: list[Note], *, generated_at: str = "") -> bytes:
    """A .docx Word opens: zip + the five parts that make one valid."""
    body = [_para(repo, "Title")]
    if generated_at:
        body.append(_para(f"Generated {generated_at}"))
    for note in notes:
        body.append(_para(note.title, "Heading1"))
        body.append(_para(note.path, "Code"))
        body.extend(_markdown_to_paragraphs(note.body))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        "</w:sectPr></w:body></w:document>"
    )

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def to_docx_document(markdown: str) -> bytes:
    """A .docx from one markdown string, with no per-note scaffolding.

    `to_docx` above assembles a repo's notes and stamps each one's file path
    under its heading. A document that was authored as a single markdown blob —
    the dependency audit — already carries its own title and structure, and the
    path line would be noise. Same converter, same five parts, different
    envelope.
    """
    body = _markdown_to_paragraphs(markdown)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        "</w:sectPr></w:body></w:document>"
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


__all__ = ["Note", "to_docx", "to_docx_document", "to_markdown"]
