"""A PDF from blocks — a document, not a printout of a web page.

The dependencies page used to make its PDF with `window.print()`: the whole
interactive page, selectors and buttons included, came out as 58 pages with the
report somewhere inside. This renders the same blocks the Word and markdown
exports use, so the three formats cannot drift apart.

fpdf2 is pure Python (Pillow + fonttools are its only compiled neighbours, and
both ship wheels), so unlike weasyprint it needs no cairo/pango in the image.
The text is set in Noto Sans, bundled in `src/docs/fonts/` under the SIL Open
Font License (see OFL.txt there) and embedded as a subset, so Cyrillic — the UI
has a Ukrainian locale — renders without any system font. Latin, Cyrillic and
Greek are covered; a CJK report would show missing-glyph boxes rather than
fail.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

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
    Stats,
    Table,
    Title,
    parse_inline,
    severity_of,
    strip_inline,
)

FONT_DIR = Path(__file__).parent / "fonts"
SANS, MONO = "NotoSans", "NotoMono"

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)


def _clean(text: str) -> str:
    return _CTRL.sub("", text)


class _Report(FPDF):
    def __init__(self, footer_label: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.footer_label = footer_label
        self.set_margins(15, 16, 15)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages("{nb}")
        for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic")):
            self.add_font(SANS, style, str(FONT_DIR / f"NotoSans-{name}.ttf"))
        self.add_font(MONO, "", str(FONT_DIR / "NotoSansMono-Regular.ttf"))
        # Noto Sans has no arrows or ≥ / ≤; the mono face does. Missing glyphs
        # fall back to it instead of printing as blanks.
        self.set_fallback_fonts([MONO], exact_match=False)

    def footer(self) -> None:
        self.set_y(-11)
        self.set_font(SANS, "", 7.5)
        self.set_text_color(*_rgb("6B7280"))
        self.set_draw_color(*_rgb("D1D5DB"))
        self.line(self.l_margin, self.get_y() - 1.5, self.w - self.r_margin, self.get_y() - 1.5)
        self.cell(self.epw * 0.7, 5, _clean(self.footer_label), align="L")
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # ── inline text ──────────────────────────────────────────────────
    def runs(self, text: str, size: float, h: float, colour: str = "111827") -> None:
        """Write inline-marked text, wrapping across lines, with style changes."""
        self.set_text_color(*_rgb(colour))
        for run in parse_inline(_clean(text)):
            if run.code:
                self.set_font(MONO, "", size - 0.8)
            else:
                self.set_font(SANS, "B" if run.bold else ("I" if run.italic else ""), size)
            self.write(h, run.text)
        self.set_text_color(0, 0, 0)

    def keep_with_next(self, needed: float) -> None:
        if self.get_y() + needed > self.h - self.b_margin:
            self.add_page()


def _heading(pdf: _Report, b: Heading) -> None:
    size, colour, before, after = {
        1: (15, HEADER_FILL, 7, 2.5), 2: (12, "1D4ED8", 5, 1.5),
        3: (10.5, "111827", 4, 1), 4: (9.5, "374151", 3, 0.5),
    }[min(max(b.level, 1), 4)]
    pdf.keep_with_next(before + 24)
    pdf.ln(before)
    pdf.set_font(SANS, "B", size)
    pdf.set_text_color(*_rgb(colour))
    pdf.multi_cell(0, size * 0.5, _clean(strip_inline(b.text)), new_x="LMARGIN", new_y="NEXT")
    if b.level == 1:
        pdf.set_draw_color(*_rgb("D1D5DB"))
        pdf.line(pdf.l_margin, pdf.get_y() + 0.5, pdf.w - pdf.r_margin, pdf.get_y() + 0.5)
        pdf.ln(1.5)
    pdf.ln(after)
    pdf.set_text_color(0, 0, 0)


def _title(pdf: _Report, b: Title) -> None:
    pdf.set_fill_color(*_rgb(HEADER_FILL))
    pdf.rect(0, 0, pdf.w, 30, style="F")
    pdf.set_xy(pdf.l_margin, 10)
    pdf.set_font(SANS, "B", 21)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, _clean(strip_inline(b.text)))
    pdf.set_y(36)
    for key, value in b.meta:
        pdf.set_font(SANS, "B", 9)
        pdf.set_text_color(*_rgb("374151"))
        pdf.cell(26, 5, _clean(f"{key}:"))
        pdf.set_font(SANS, "", 9)
        pdf.multi_cell(0, 5, _clean(strip_inline(value)), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _para(pdf: _Report, b: Para) -> None:
    if b.kind == "note":
        pdf.set_font(SANS, "I", 8)
        pdf.set_text_color(*_rgb("6B7280"))
        pdf.multi_cell(0, 4, _clean(strip_inline(b.text)), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
    elif b.kind == "callout":
        pdf.set_fill_color(*_rgb("FFF7ED"))
        pdf.set_draw_color(*_rgb("EA580C"))
        pdf.set_font(SANS, "", 9.5)
        pdf.set_text_color(*_rgb("111827"))
        pdf.multi_cell(0, 5, _clean(strip_inline(b.text)), border="L", fill=True,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    else:
        pdf.runs(b.text, 9.5, 4.8)
        pdf.ln(5.6)
    pdf.set_text_color(0, 0, 0)


def _bullets(pdf: _Report, b: Bullets) -> None:
    margin = pdf.l_margin
    indent = 6
    for n, item in enumerate(b.items, 1):
        pdf.keep_with_next(10)
        pdf.set_x(margin)
        pdf.set_font(SANS, "", 9.5)
        pdf.cell(indent, 4.8, f"{n}." if b.ordered else "•")
        pdf.set_left_margin(margin + indent)
        pdf.set_x(margin + indent)
        pdf.runs(item, 9.5, 4.8)
        pdf.ln(5)
        pdf.set_left_margin(margin)
    pdf.ln(1.5)


def _stats(pdf: _Report, b: Stats, per_row: int = 3) -> None:
    gap = 3
    w = (pdf.epw - gap * (per_row - 1)) / per_row
    h = 17
    items = list(b.items)
    for start in range(0, len(items), per_row):
        pdf.keep_with_next(h + 2)
        y = pdf.get_y()
        for i, s in enumerate(items[start:start + per_row]):
            x = pdf.l_margin + i * (w + gap)
            fill, accent = TONE_COLOURS.get(s.tone, TONE_COLOURS["neutral"])
            pdf.set_fill_color(*_rgb(fill))
            pdf.rect(x, y, w, h, style="F")
            pdf.set_xy(x, y + 2)
            pdf.set_font(SANS, "B", 15)
            pdf.set_text_color(*_rgb(accent))
            pdf.cell(w, 8, _clean(strip_inline(s.value)), align="C")
            pdf.set_xy(x, y + 10.5)
            pdf.set_font(SANS, "", 7.5)
            pdf.set_text_color(*_rgb("4B5563"))
            pdf.cell(w, 4, _clean(strip_inline(s.label)), align="C")
        pdf.set_y(y + h + gap)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _table(pdf: _Report, b: Table) -> None:
    n = len(b.header)
    widths = b.widths or [1.0] * n
    total = sum(widths) or 1.0
    col_widths = tuple(w / total * pdf.epw for w in widths)
    pdf.set_font(SANS, "", 8)
    pdf.set_draw_color(*_rgb("E5E7EB"))
    head = FontFace(family=SANS, emphasis="BOLD", color=_rgb("FFFFFF"),
                    fill_color=_rgb(HEADER_FILL), size_pt=8)
    pdf.keep_with_next(24)
    with pdf.table(
        col_widths=col_widths, width=pdf.epw, text_align="LEFT",
        line_height=4.2, headings_style=head, borders_layout="HORIZONTAL_LINES",
        padding=(0.9, 1.2), repeat_headings=1,
    ) as table:
        header = table.row()
        for h in b.header:
            header.cell(_clean(strip_inline(h)))
        for r, row in enumerate(b.rows):
            tr = table.row()
            for i in range(n):
                text = _clean(strip_inline(row[i] if i < len(row) else ""))
                sev = severity_of(text)
                if sev:
                    fill, fg = SEVERITY_COLOURS[sev]
                    style = FontFace(family=SANS, emphasis="BOLD", color=_rgb(fg),
                                     fill_color=_rgb(fill), size_pt=7.5)
                    tr.cell(text.upper(), style=style, align="C")
                else:
                    style = FontFace(
                        family=MONO if i in b.mono else SANS, size_pt=7.5 if i in b.mono else 8,
                        color=_rgb("111827"),
                        fill_color=_rgb(BAND) if r % 2 else (255, 255, 255))
                    tr.cell(text, style=style)
    pdf.ln(3)


def _code(pdf: _Report, b: Code) -> None:
    pdf.set_font(MONO, "", 7.5)
    pdf.set_fill_color(*_rgb("F4F4F5"))
    for line in b.lines or [""]:
        pdf.multi_cell(0, 3.8, _clean(line) or " ", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def blocks_to_pdf(blocks: list[Block], *, footer: str = "", title: str = "",
                  author: str = "Celmis") -> bytes:
    """Render blocks to PDF bytes (page numbers in the footer, fonts embedded)."""
    # fpdf2 logs a warning per missing glyph; a CJK report would flood the log.
    logging.getLogger("fpdf").setLevel(logging.ERROR)
    first = next((b for b in blocks if isinstance(b, Title)), None)
    title = title or (first.text if first else "")
    pdf = _Report(footer or title)
    pdf.set_title(title)
    pdf.set_author(author)
    pdf.set_creator(author)
    pdf.add_page()
    for b in blocks:
        if isinstance(b, PageBreak):
            if pdf.get_y() > pdf.t_margin + 1:
                pdf.add_page()
        elif isinstance(b, Title):
            _title(pdf, b)
        elif isinstance(b, Heading):
            _heading(pdf, b)
        elif isinstance(b, Para):
            _para(pdf, b)
        elif isinstance(b, Bullets):
            _bullets(pdf, b)
        elif isinstance(b, Table):
            _table(pdf, b)
        elif isinstance(b, Stats):
            _stats(pdf, b)
        elif isinstance(b, Code):
            _code(pdf, b)
    return bytes(pdf.output())


__all__ = ["blocks_to_pdf"]
