"""One document model, three renderers.

A report used to be a markdown string that each exporter re-parsed with its own
idea of what a table or a bullet was; the .docx path stripped emphasis, drew
bullets as a "•" typed into the text, and had no table at all. Here the report
is built once as a short list of blocks, and markdown, Word and PDF are three
renderings of the same list — so a table is a table in all of them.

Inline text uses a tiny markup, the same subset the generated AI report uses:
``**bold**``, ``*italic*`` / ``_italic_`` and ```code```. `parse_inline` turns
it into runs; nothing downstream ever sees the raw markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Severity words that get a colour cue wherever they stand alone in a cell.
SEVERITIES = ("critical", "high", "medium", "low")

#: severity -> (fill, text) as hex RGB. Shared by Word and PDF so the colour
#: cue means the same thing in both. Contrast is chosen per fill, not global.
SEVERITY_COLOURS = {
    "critical": ("B91C1C", "FFFFFF"),
    "high": ("EA580C", "FFFFFF"),
    "medium": ("FACC15", "1F2937"),
    "low": ("BFDBFE", "1F2937"),
}
#: Stat-card tone -> (fill, accent text).
TONE_COLOURS = {
    "neutral": ("F3F4F6", "111827"),
    "bad": ("FEE2E2", "991B1B"),
    "warn": ("FFEDD5", "9A3412"),
    "good": ("DCFCE7", "166534"),
}
HEADER_FILL = "1F2937"
BAND = "F9FAFB"


@dataclass(frozen=True)
class Heading:
    level: int          # 1..4
    text: str


@dataclass(frozen=True)
class Para:
    text: str
    #: body | note (muted, small — provenance lines) | callout (boxed)
    kind: str = "body"


@dataclass(frozen=True)
class Bullets:
    items: list[str]
    ordered: bool = False


@dataclass(frozen=True)
class Table:
    header: list[str]
    rows: list[list[str]]
    #: Relative column widths; equal when empty.
    widths: list[float] = field(default_factory=list)
    #: Columns that hold code-like values (versions, ids): set in monospace.
    mono: tuple[int, ...] = ()


@dataclass(frozen=True)
class Stat:
    label: str
    value: str
    #: neutral | bad | warn | good — the card's colour cue
    tone: str = "neutral"


@dataclass(frozen=True)
class Stats:
    items: list[Stat]


@dataclass(frozen=True)
class Code:
    lines: list[str]


@dataclass(frozen=True)
class PageBreak:
    pass


@dataclass(frozen=True)
class Title:
    text: str
    #: "Label: value" lines under the title (workspace, date, run id)
    meta: list[tuple[str, str]] = field(default_factory=list)


Block = Heading | Para | Bullets | Table | Stats | Code | PageBreak | Title


@dataclass(frozen=True)
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False


_INLINE = re.compile(
    r"(\*\*[^*\n]+?\*\*"
    r"|`[^`\n]+`"
    r"|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])"
    r"|(?<![\w_])_[^_\s][^_\n]*?_(?![\w_]))"
)


def parse_inline(text: str) -> list[Run]:
    """Split inline markup into runs; unmatched markers stay literal text."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # a link keeps its text
    runs: list[Run] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            runs.append(Run(text[pos:m.start()]))
        tok = m.group(0)
        if tok.startswith("**"):
            runs.append(Run(tok[2:-2], bold=True))
        elif tok.startswith("`"):
            runs.append(Run(tok[1:-1], code=True))
        else:
            runs.append(Run(tok[1:-1], italic=True))
        pos = m.end()
    if pos < len(text):
        runs.append(Run(text[pos:]))
    return runs or [Run("")]


def strip_inline(text: str) -> str:
    """The text without any markup — for places that cannot style a run."""
    return "".join(r.text for r in parse_inline(text))


def severity_of(cell: str) -> str | None:
    """The severity a cell consists of, if it is nothing but a severity word."""
    word = strip_inline(cell).strip().lower()
    return word if word in SEVERITIES else None


# ─── markdown → blocks ───────────────────────────────────────────────

_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", inner)]


def parse_markdown(md: str) -> list[Block]:
    """Headings, paragraphs, bullet/numbered lists, pipe tables, code fences.

    The subset the audit and the generated AI report actually use. Anything
    else is a paragraph — never dropped.
    """
    blocks: list[Block] = []
    lines = md.splitlines()
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append(Para(" ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("```"):
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i].rstrip())
                i += 1
            blocks.append(Code(code))
            i += 1
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
        if heading:
            flush()
            blocks.append(Heading(min(len(heading.group(1)), 4), heading.group(2)))
            i += 1
            continue
        if _ROW.match(line) and i + 1 < len(lines) and _SEP.match(lines[i + 1]):
            flush()
            header = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and _ROW.match(lines[i]):
                row = _cells(lines[i])
                row = (row + [""] * len(header))[:len(header)]
                rows.append(row)
                i += 1
            blocks.append(Table(header, rows))
            continue
        item = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", line)
        if item:
            flush()
            ordered = item.group(1)[0].isdigit()
            items: list[str] = []
            while i < len(lines):
                m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)$", lines[i].rstrip())
                if m and m.group(1)[0].isdigit() == ordered:
                    items.append(m.group(2))
                    i += 1
                elif (lines[i].startswith("  ") and lines[i].strip() and items
                      and not _ROW.match(lines[i])):
                    items[-1] += " " + lines[i].strip()      # a wrapped item
                    i += 1
                else:
                    break
            blocks.append(Bullets(items, ordered))
            continue
        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            flush()
            blocks.append(Para(quote.group(1), "callout"))
            i += 1
            continue
        para.append(line)
        i += 1
    flush()
    return blocks


# ─── blocks → markdown ───────────────────────────────────────────────

def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown_text(blocks: list[Block]) -> str:
    out: list[str] = []
    for b in blocks:
        if isinstance(b, Title):
            out += [f"# {b.text}", ""]
            if b.meta:
                out += ["_" + " · ".join(f"{k}: {v}" for k, v in b.meta) + "_", ""]
        elif isinstance(b, Heading):
            out += ["#" * b.level + f" {b.text}", ""]
        elif isinstance(b, Para):
            if b.kind == "callout":
                out += [f"> {b.text}", ""]
            elif b.kind == "note":
                out += [f"_{b.text}_", ""]
            else:
                out += [b.text, ""]
        elif isinstance(b, Bullets):
            for n, item in enumerate(b.items, 1):
                out.append(f"{n}. {item}" if b.ordered else f"- {item}")
            out.append("")
        elif isinstance(b, Table):
            out.append("| " + " | ".join(_md_cell(h) for h in b.header) + " |")
            out.append("|" + "|".join(" --- " for _ in b.header) + "|")
            for row in b.rows:
                out.append("| " + " | ".join(_md_cell(c) for c in row) + " |")
            out.append("")
        elif isinstance(b, Stats):
            out += [f"- **{s.label}:** {s.value}" for s in b.items]
            out.append("")
        elif isinstance(b, Code):
            out += ["```", *b.lines, "```", ""]
        # PageBreak has no markdown form
    return "\n".join(out).rstrip() + "\n"


__all__ = [
    "BAND", "HEADER_FILL", "SEVERITIES", "SEVERITY_COLOURS", "TONE_COLOURS", "Block", "Bullets", "Code", "Heading", "PageBreak", "Para",
    "Run", "Stat", "Stats", "Table", "Title", "parse_inline", "parse_markdown",
    "severity_of", "strip_inline", "to_markdown_text",
]
