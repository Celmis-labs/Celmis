"""A Word document from blocks: real styles, real tables, real lists.

Hand-written WordprocessingML, like `src.docs.export` — python-docx pulls in
lxml, a C extension, into an image that has already run out of disk once. The
parts are a small fixed set (document, styles, numbering, footer, core
properties) and the tests parse every one of them as XML.

What the first converter got wrong, and what this does instead:

  * bullets were a "•" typed into the paragraph text, so a wrapped line went
    back under the bullet — now a numbering definition with a hanging indent;
  * emphasis was stripped, so `**CRITICAL**` lost its weight — now runs;
  * there was no table, so a finding was a 200-character bullet — now a table
    with a repeating header row, fixed column widths and a colour cue on the
    severity cell;
  * no page numbers, and headings that were bold 14 pt text — now a footer with
    "Page X of Y" and a heading hierarchy that has colour and keep-with-next.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

from src.docs.blocks import (
    BAND,
    HEADER_FILL,
    SEVERITY_COLOURS,
    TONE_COLOURS,
    Block,
    Bullets,
    Code,
    Heading,
    PageBreak,
    Para,
    Run,
    Stats,
    Table,
    Title,
    parse_inline,
    severity_of,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
#: A4 with 2 cm margins: 11906 - 2 * 1134 twips of text width.
PAGE_W, PAGE_H, MARGIN = 11906, 16838, 1134
TEXT_W = PAGE_W - 2 * MARGIN

_BODY_FONT = "Calibri"
_MONO_FONT = "Consolas"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>"""


def _style_p(sid: str, name: str, *, based: str | None = "Normal", ppr: str = "",
             rpr: str = "", nxt: str | None = None, extra: str = "") -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
        + (f'<w:basedOn w:val="{based}"/>' if based else "")
        + (f'<w:next w:val="{nxt}"/>' if nxt else "")
        + extra + '<w:qFormat/>'
        + (f"<w:pPr>{ppr}</w:pPr>" if ppr else "")
        + (f"<w:rPr>{rpr}</w:rPr>" if rpr else "")
        + "</w:style>"
    )


def _heading_style(level: int, size: int, colour: str, before: int, after: int) -> str:
    return _style_p(
        f"Heading{level}", f"heading {level}", nxt="Normal",
        ppr=(f'<w:keepNext/><w:keepLines/><w:spacing w:before="{before}" w:after="{after}"/>'
             f'<w:outlineLvl w:val="{level - 1}"/>'
             + ('<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="2" w:color="D1D5DB"/></w:pBdr>'
                if level == 1 else "")),
        rpr=f'<w:b/><w:color w:val="{colour}"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/>',
    )


_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{W_NS}">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    f'<w:rFonts w:ascii="{_BODY_FONT}" w:hAnsi="{_BODY_FONT}" w:cs="{_BODY_FONT}" w:eastAsia="{_BODY_FONT}"/>'
    '<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    '</w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
    '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/></w:style>'
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:semiHidden/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar>'
    '<w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/>'
    '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    + _style_p("Title", "Title", nxt="Normal",
               ppr='<w:spacing w:before="0" w:after="80"/><w:pBdr><w:bottom w:val="single" '
                   f'w:sz="18" w:space="6" w:color="{HEADER_FILL}"/></w:pBdr>',
               rpr=f'<w:b/><w:color w:val="{HEADER_FILL}"/><w:sz w:val="48"/><w:szCs w:val="48"/>')
    + _style_p("Subtitle", "Subtitle", nxt="Normal",
               ppr='<w:spacing w:after="20"/>',
               rpr='<w:color w:val="4B5563"/><w:sz w:val="21"/>')
    + _heading_style(1, 32, HEADER_FILL, 360, 120)
    + _heading_style(2, 26, "1D4ED8", 280, 80)
    + _heading_style(3, 23, "111827", 200, 60)
    + _heading_style(4, 21, "374151", 160, 40)
    + _style_p("Note", "Note", ppr='<w:spacing w:after="120"/>',
               rpr='<w:i/><w:color w:val="6B7280"/><w:sz w:val="18"/><w:szCs w:val="18"/>')
    + _style_p("Callout", "Callout",
               ppr='<w:pBdr><w:left w:val="single" w:sz="24" w:space="8" w:color="EA580C"/></w:pBdr>'
                   '<w:shd w:val="clear" w:color="auto" w:fill="FFF7ED"/>'
                   '<w:spacing w:before="60" w:after="140"/><w:ind w:left="200"/>')
    + _style_p("ListBullet", "List Bullet",
               ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
                   '<w:spacing w:after="40"/>')
    + _style_p("ListNumber", "List Number", ppr='<w:spacing w:after="40"/>')
    + _style_p("TableText", "Table Text",
               ppr='<w:spacing w:before="30" w:after="30" w:line="240" w:lineRule="auto"/>',
               rpr='<w:sz w:val="17"/><w:szCs w:val="17"/>')
    + _style_p("TableHead", "Table Head", based="TableText", ppr="<w:keepNext/>",
               rpr='<w:b/><w:color w:val="FFFFFF"/>')
    + _style_p("StatValue", "Stat Value", ppr='<w:spacing w:before="80" w:after="0"/><w:jc w:val="center"/>',
               rpr='<w:b/><w:sz w:val="40"/><w:szCs w:val="40"/>')
    + _style_p("StatLabel", "Stat Label", ppr='<w:spacing w:before="0" w:after="80"/><w:jc w:val="center"/>',
               rpr='<w:color w:val="4B5563"/><w:sz w:val="17"/><w:szCs w:val="17"/>')
    + _style_p("Code", "Code",
               ppr='<w:shd w:val="clear" w:color="auto" w:fill="F4F4F5"/>'
                   '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>',
               rpr=f'<w:rFonts w:ascii="{_MONO_FONT}" w:hAnsi="{_MONO_FONT}" w:cs="{_MONO_FONT}"/>'
                   '<w:sz w:val="18"/><w:szCs w:val="18"/>')
    + _style_p("Footer", "footer", ppr='<w:spacing w:after="0"/><w:jc w:val="center"/>',
               rpr='<w:color w:val="6B7280"/><w:sz w:val="16"/><w:szCs w:val="16"/>')
    + '<w:style w:type="table" w:styleId="ReportTable"><w:name w:val="Report Table"/>'
      '<w:basedOn w:val="TableNormal"/><w:tblPr><w:tblBorders>'
      '<w:top w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
      '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
      '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E5E7EB"/>'
      '</w:tblBorders><w:tblCellMar><w:top w:w="20" w:type="dxa"/>'
      '<w:left w:w="80" w:type="dxa"/><w:bottom w:w="20" w:type="dxa"/>'
      '<w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
    + "</w:styles>"
)

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _t(text: str) -> str:
    return escape(_CTRL.sub("", text))


def _runs_xml(text: str, *, mono: bool = False, bold: bool = False,
              colour: str | None = None) -> str:
    out = []
    for run in parse_inline(text):
        out.append(_run_xml(run, mono=mono, bold=bold, colour=colour))
    return "".join(out)


def _run_xml(run: Run, *, mono: bool, bold: bool, colour: str | None) -> str:
    if not run.text:
        return ""
    props = ""
    if mono or run.code:
        props += (f'<w:rFonts w:ascii="{_MONO_FONT}" w:hAnsi="{_MONO_FONT}" '
                  f'w:cs="{_MONO_FONT}"/>')
    if bold or run.bold:
        props += "<w:b/>"
    if run.italic:
        props += "<w:i/>"
    if colour:
        props += f'<w:color w:val="{colour}"/>'
    if run.code and not mono:
        props += '<w:sz w:val="19"/><w:shd w:val="clear" w:color="auto" w:fill="F1F3F5"/>'
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_t(run.text)}</w:t></w:r>'


def _p(inner: str, style: str | None = None, extra_ppr: str = "") -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + extra_ppr
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{inner}</w:p>"


def _shd(fill: str) -> str:
    return f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'


def _grid(widths: list[float], total: int) -> list[int]:
    n = len(widths)
    s = sum(widths) or 1
    cols = [max(300, int(total * w / s)) for w in widths]
    cols[-1] += total - sum(cols)         # the grid must add up exactly
    return cols if n else []


def _table_xml(tbl: Table) -> str:
    n = len(tbl.header)
    cols = _grid(tbl.widths or [1.0] * n, TEXT_W)
    out = [
        '<w:tbl><w:tblPr><w:tblStyle w:val="ReportTable"/>'
        f'<w:tblW w:w="{TEXT_W}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
        '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" '
        'w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid>'
        + "".join(f'<w:gridCol w:w="{c}"/>' for c in cols) + "</w:tblGrid>"
    ]
    head = "".join(
        f'<w:tc><w:tcPr><w:tcW w:w="{cols[i]}" w:type="dxa"/>{_shd(HEADER_FILL)}'
        f'<w:vAlign w:val="center"/></w:tcPr>'
        f'{_p(_runs_xml(h, bold=True, colour="FFFFFF"), "TableHead")}</w:tc>'
        for i, h in enumerate(tbl.header)
    )
    out.append(f'<w:tr><w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>{head}</w:tr>')
    for r, row in enumerate(tbl.rows):
        cells = []
        for i in range(n):
            text = row[i] if i < len(row) else ""
            sev = severity_of(text)
            fill = SEVERITY_COLOURS[sev][0] if sev else (BAND if r % 2 else None)
            colour = SEVERITY_COLOURS[sev][1] if sev else None
            body = _runs_xml(text, mono=i in tbl.mono, bold=bool(sev), colour=colour)
            align = '<w:jc w:val="center"/>' if sev else ""
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{cols[i]}" w:type="dxa"/>'
                f'{_shd(fill) if fill else ""}</w:tcPr>'
                f'{_p(body, "TableText", align)}</w:tc>'
            )
        out.append(f'<w:tr><w:trPr><w:cantSplit/></w:trPr>{"".join(cells)}</w:tr>')
    out.append("</w:tbl>")
    # A paragraph after a table keeps two tables from fusing in Word.
    out.append(_p("", None, '<w:spacing w:after="60"/>'))
    return "".join(out)


def _stats_xml(stats: Stats, per_row: int = 3) -> str:
    cols = _grid([1.0] * per_row, TEXT_W)
    white = ('<w:tcBorders>' + "".join(
        f'<w:{side} w:val="single" w:sz="24" w:space="0" w:color="FFFFFF"/>'
        for side in ("top", "left", "bottom", "right")) + "</w:tcBorders>")
    rows = []
    items = list(stats.items)
    for start in range(0, len(items), per_row):
        chunk = items[start:start + per_row]
        cells = []
        for i in range(per_row):
            if i < len(chunk):
                s = chunk[i]
                fill, accent = TONE_COLOURS.get(s.tone, TONE_COLOURS["neutral"])
                cells.append(
                    f'<w:tc><w:tcPr><w:tcW w:w="{cols[i]}" w:type="dxa"/>{white}{_shd(fill)}</w:tcPr>'
                    f'{_p(_runs_xml(s.value, colour=accent), "StatValue")}'
                    f'{_p(_runs_xml(s.label), "StatLabel")}</w:tc>')
            else:
                cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{cols[i]}" w:type="dxa"/></w:tcPr>'
                             f'{_p("")}</w:tc>')
        rows.append(f'<w:tr><w:trPr><w:cantSplit/></w:trPr>{"".join(cells)}</w:tr>')
    return (
        '<w:tbl><w:tblPr>'
        f'<w:tblW w:w="{TEXT_W}" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>'
        + "".join(f'<w:gridCol w:w="{c}"/>' for c in cols) + "</w:tblGrid>"
        + "".join(rows) + "</w:tbl>" + _p("", None, '<w:spacing w:after="60"/>')
    )


def _numbering(n_ordered: int) -> str:
    def lvl(fmt: str, text: str, font: str | None) -> str:
        return (
            f'<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
            f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/>'
            '<w:pPr><w:ind w:left="454" w:hanging="284"/></w:pPr>'
            + (f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:hint="default"/></w:rPr>'
               if font else "")
            + "</w:lvl>")
    nums = ['<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>']
    for k in range(n_ordered):
        nums.append(
            f'<w:num w:numId="{k + 2}"><w:abstractNumId w:val="1"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>')
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="{W_NS}">'
        f'<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="singleLevel"/>'
        f'{lvl("bullet", chr(0x2022), None)}</w:abstractNum>'
        f'<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="singleLevel"/>'
        f'{lvl("decimal", "%1.", None)}</w:abstractNum>'
        + "".join(nums) + "</w:numbering>"
    )


def _footer(label: str) -> str:
    def field(code: str) -> str:
        return (
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText xml:space="preserve"> {code} </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    def text(s: str) -> str:
        return f'<w:r><w:t xml:space="preserve">{_t(s)}</w:t></w:r>'
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr xmlns:w="{W_NS}">'
        + _p(text(f"{label}  ·  Page ") + field("PAGE") + text(" of ") + field("NUMPAGES"),
             "Footer")
        + "</w:ftr>"
    )


def _core(title: str, author: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:title>{_t(title)}</dc:title><dc:creator>{_t(author)}</dc:creator>'
        '</cp:coreProperties>'
    )


def blocks_to_docx(blocks: list[Block], *, footer: str = "", author: str = "Celmis") -> bytes:
    """Render blocks to a .docx. `footer` is the left part of the page footer."""
    body: list[str] = []
    n_ordered = 0
    page_break = False
    title = ""

    def before() -> str:
        nonlocal page_break
        flag, page_break = page_break, False
        return "<w:pageBreakBefore/>" if flag else ""

    for b in blocks:
        if isinstance(b, PageBreak):
            page_break = True
        elif isinstance(b, Title):
            title = title or b.text
            body.append(_p(_runs_xml(b.text), "Title"))
            for key, value in b.meta:
                body.append(_p(_runs_xml(f"**{key}:** {value}"), "Subtitle"))
            body.append(_p(""))
        elif isinstance(b, Heading):
            body.append(_p(_runs_xml(b.text), f"Heading{min(max(b.level, 1), 4)}", before()))
        elif isinstance(b, Para):
            style = {"note": "Note", "callout": "Callout"}.get(b.kind)
            body.append(_p(_runs_xml(b.text), style, before()))
        elif isinstance(b, Bullets):
            if b.ordered:
                n_ordered += 1
                num = (f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{n_ordered + 1}"/></w:numPr>')
                for item in b.items:
                    body.append(_p(_runs_xml(item), "ListNumber", num))
            else:
                for item in b.items:
                    body.append(_p(_runs_xml(item), "ListBullet"))
        elif isinstance(b, Table):
            body.append(_table_xml(b))
        elif isinstance(b, Stats):
            body.append(_stats_xml(b))
        elif isinstance(b, Code):
            for line in b.lines or [""]:
                body.append(_p(
                    f'<w:r><w:t xml:space="preserve">{_t(line)}</w:t></w:r>' if line else "",
                    "Code"))
            body.append(_p("", None, '<w:spacing w:after="60"/>'))

    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<w:body>{"".join(body)}'
        '<w:sectPr><w:footerReference w:type="default" r:id="rId3"/>'
        f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN + 200}" '
        f'w:left="{MARGIN}" w:header="567" w:footer="567" w:gutter="0"/>'
        '</w:sectPr></w:body></w:document>'
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/numbering.xml", _numbering(n_ordered))
        z.writestr("word/footer1.xml", _footer(footer or title))
        z.writestr("docProps/core.xml", _core(title, author))
        z.writestr("word/document.xml", document)
    return buf.getvalue()


__all__ = ["blocks_to_docx"]
