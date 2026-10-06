"""Markers: the machine-readable lines every bot comment carries, kept invisible.

Celmis finds its own comments, summaries and descriptions again by a marker
line (`<!-- celmis:finding fp=… -->`, the review marker, the lifecycle marks,
the description block). On GitHub and GitLab an HTML comment renders as
nothing. Bitbucket Cloud does not render raw HTML, so there the same line
would show up as text in every comment. The code keeps writing the HTML form
everywhere; the Bitbucket provider converts at the boundary:

    outbound  hide(bitbucket_flavour(text))    HTML comment -> `[//]: # (x)`
    inbound   reveal(text)                     and back, so every reader
                                               (and every `in` test) sees one form

Styles (`REVIEW_MARKER_STYLE`, default `auto`):
    html     leave the HTML comment alone (GitHub, GitLab)
    refdef   a markdown reference definition, `[//]: # (x)`; renders as
             nothing on CommonMark-compatible renderers
    zwsp     zero-width characters on a line of their own: the last resort
             when a renderer shows the refdef anyway. The provider switches to
             it for the rest of the process the first time it sees a marker in
             the `content.html` Bitbucket answers with (`fall_back_to_zwsp`).
    auto     refdef (hide is only ever called for Bitbucket)

Kinds (`has_marker(raw, kind)`):
    review   the idempotency marker of the persistent review comment
    status   a lifecycle mark (in-progress / feedback)
    summary  the description block's own markers
    chat:v1  a reply to a PR command or a question
    finding  an inline finding: `finding_marker(fp, sha)`

A marker counts only when it is a whole line of its own. A quote reply
(`> <!-- … -->`) copies the quoted comment's raw markdown, marker and all, into
a human's rebuttal; matching on the whole line keeps that rebuttal from being
mistaken for ours. Authorship is still the second half of "is it ours" —
`has_marker(...) and authored_by_viewer`. A marker says what SHAPE a comment
has, never who wrote it.
"""

from __future__ import annotations

import logging
import re
from typing import Final

logger = logging.getLogger(__name__)

STYLES: Final[tuple[str, ...]] = ("auto", "html", "refdef", "zwsp")

#: Token prefixes that make an HTML comment OURS (so a human's own `<!-- todo -->`
#: or `[//]: # (note)` is never rewritten). The configured review marker
#: (`REVIEW_COMMENT_MARKER`) is ours as well; see `_ours`.
_OUR_PREFIXES: Final[tuple[str, ...]] = ("celmis:", "code-analyzer:")

#: The built-in review marker's own token, and its successor name.
_REVIEW_TOKENS: Final[frozenset[str]] = frozenset({"code-analyzer:review", "celmis:review"})

# `\r?` before `$`: a description edited in a web form can come back with CRLF line ends.
_HTML_LINE = re.compile(
    r"^[ \t]*<!--[ \t]*(?P<x>(?:(?!-->).)*?)[ \t]*-->[ \t]*\r?$", re.MULTILINE)
_HTML_ANY = re.compile(r"<!--[ \t]*(?P<x>[^\n]*?)[ \t]*-->")
_REFDEF = re.compile(
    r"^[ \t]*\[//\]:[ \t]*#[ \t]*\((?P<x>[^\n\r]*)\)[ \t]*\r?$", re.MULTILINE)

# zwsp: U+2060 word joiner, then one invisible character per bit of the UTF-8
# payload (U+200B = 0, U+200C = 1), then U+2060 again — a line of its own.
_ZW_EDGE: Final = "⁠"
_ZW_BITS: Final = ("​", "‌")
_ZWSP = re.compile(
    "^[ \t]*" + _ZW_EDGE + "(?P<bits>[​‌]+)" + _ZW_EDGE + "[ \t]*\r?$",
    re.MULTILINE,
)

_FINDING = re.compile(r"^celmis:finding fp=(?P<fp>[0-9a-f]{16}) sha=(?P<sha>[0-9a-f]{12})$")

#: Set by `fall_back_to_zwsp` once a renderer has shown a refdef marker as text.
_forced_style: str | None = None


# ─── the configured marker ────────────────────────────────────────────

def _configured_token() -> str:
    """The token inside the install's `REVIEW_COMMENT_MARKER`, if it is an HTML comment."""
    try:
        from src.review.settings import get_review_settings

        found = _HTML_ANY.fullmatch((get_review_settings().comment_marker or "").strip())
    except Exception:  # noqa: BLE001 — a marker lookup never fails a review
        return ""
    return found.group("x") if found else ""


def _ours(token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    return token.startswith(_OUR_PREFIXES) or token == _configured_token()


# ─── whole lines outside code ─────────────────────────────────────────

_FENCE_LINE = re.compile(r"^[ \t]*(?P<f>`{3,}|~{3,})")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges of the CLOSED fenced code blocks in `text`.

    An unclosed fence is not a block: a stray ``` in a model's text must not
    make the markers we append after it invisible to us.
    """
    if "```" not in text and "~~~" not in text:
        return []
    spans: list[tuple[int, int]] = []
    opened: tuple[str, int] | None = None
    pos = 0
    for line in text.split("\n"):
        found = _FENCE_LINE.match(line)
        if opened is None:
            if found:
                opened = (found.group("f"), pos)
        elif (found and found.group("f")[0] == opened[0][0]
              and len(found.group("f")) >= len(opened[0])
              and set(line.strip()) == {opened[0][0]}):
            spans.append((opened[1], pos + len(line)))
            opened = None
        pos += len(line) + 1
    return spans


def _whole_lines(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """The matches of a whole-line `pattern` that are not inside a fenced code block.

    A marker quoted in a code block or an inline code span is a mention of a
    marker, never one.
    """
    spans = _fenced_spans(text)
    return [m for m in pattern.finditer(text)
            if not any(a <= m.start() < b for a, b in spans)]


# ─── style ────────────────────────────────────────────────────────────

def marker_style() -> str:
    """The style the next `hide` uses: forced fallback, else the install's setting."""
    if _forced_style:
        return _forced_style
    try:
        from src.review.settings import get_review_settings

        style = str(get_review_settings().marker_style or "auto").strip().lower()
    except Exception:  # noqa: BLE001
        style = "auto"
    return style if style in STYLES else "auto"


def fall_back_to_zwsp() -> None:
    """Hide markers with zero-width characters from now on, process-wide."""
    global _forced_style
    if _forced_style != "zwsp":
        logger.warning("bitbucket_marker_style_fallback style=zwsp")
    _forced_style = "zwsp"


def reset_style_fallback() -> None:
    """Forget a fallback (tests, and a reload of the settings)."""
    global _forced_style
    _forced_style = None


# ─── hide / reveal ────────────────────────────────────────────────────

def _zw_encode(token: str) -> str:
    bits = "".join(f"{byte:08b}" for byte in token.encode("utf-8"))
    return _ZW_EDGE + "".join(_ZW_BITS[int(b)] for b in bits) + _ZW_EDGE


def _zw_decode(bits: str) -> str | None:
    if len(bits) % 8:
        return None
    try:
        raw = bytes(
            int("".join("1" if c == _ZW_BITS[1] else "0" for c in bits[i:i + 8]), 2)
            for i in range(0, len(bits), 8)
        )
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _hidden_line(token: str, style: str) -> str:
    return _zw_encode(token) if style == "zwsp" else f"[//]: # ({token})"


def hide(text: str, style: str | None = None) -> str:
    """`text` with every marker of ours turned into an invisible line.

    A marker becomes a line of its own with a blank line before and after:
    CommonMark does not let a reference definition interrupt a paragraph, and
    a renderer that drops it still needs the paragraph break. Idempotent, and a
    no-op for the `html` style.
    """
    text = text or ""
    style = (style or marker_style()).lower()
    if style == "auto":
        style = "refdef"
    if style == "html" or "<!--" not in text:
        return text
    blocks: list[str] = []
    pos = 0
    changed = False
    # Only a marker that is a whole line of its own: one in the middle of a
    # sentence, in an inline code span or in a code block is text about a marker.
    for found in _whole_lines(_HTML_LINE, text):
        token = found.group("x")
        if not _ours(token):
            continue
        changed = True
        blocks.append(text[pos:found.start()])
        blocks.append("\0" + _hidden_line(token, style))
        pos = found.end()
    if not changed:
        return text
    blocks.append(text[pos:])
    # Text blocks next to a marker give up their edge newlines (the marker
    # brings its own blank lines); a block that is empty after that is dropped.
    out: list[str] = []
    for i, block in enumerate(blocks):
        if block.startswith("\0"):
            out.append(block[1:])
            continue
        if i > 0:
            block = re.sub(r"\A[ \t]+(?=\S)", "", block).lstrip("\n")
        if i < len(blocks) - 1:
            block = block.rstrip("\n \t")
        if block.strip():
            out.append(block)
    return "\n\n".join(out)


def reveal(text: str) -> str:
    """The inverse of `hide`: every hidden marker of ours back as an HTML comment.

    Idempotent, and a legacy HTML marker passes through untouched — so a
    comment or description written before markers were hidden is still
    recognised, and the next write converts it.
    """
    text = text or ""
    if "[//]" not in text and _ZW_EDGE not in text:
        return text

    def _ref(found: re.Match[str]) -> str | None:
        token = found.group("x").strip()
        return f"<!-- {token} -->" if _ours(token) else None

    def _zw(found: re.Match[str]) -> str | None:
        token = _zw_decode(found.group("bits"))
        return f"<!-- {token} -->" if token is not None and _ours(token) else None

    def _sub(pattern: re.Pattern[str], fn, body: str) -> str:
        out: list[str] = []
        pos = 0
        for found in _whole_lines(pattern, body):
            new = fn(found)
            if new is None:
                continue
            out.append(body[pos:found.start()])
            out.append(new)
            pos = found.end()
        out.append(body[pos:])
        return "".join(out)

    return _sub(_ZWSP, _zw, _sub(_REFDEF, _ref, text))


def fit(text: str, limit: int, style: str | None = None) -> str:
    """`hide(text)` cut to `limit` characters without ever cutting a marker.

    Markers are what every later run finds the comment by; a truncated one is a
    comment nobody can ever replace. They also bound the text between them (the
    description block's start and end), so they stay where they are: the cut
    comes off the end of the longest stretch of text between two markers.
    """
    hidden = hide(text, style)
    if limit <= 0 or len(hidden) <= limit:
        return hidden
    revealed = reveal(hidden)
    found = [m for m in _whole_lines(_HTML_LINE, revealed) if _ours(m.group("x"))]
    note = "\n\n…"
    pieces: list[str] = []  # text, marker, text, marker … text
    pos = 0
    for m in found:
        pieces += [revealed[pos:m.start()], m.group(0)]
        pos = m.end()
    pieces.append(revealed[pos:])
    texts = range(0, len(pieces), 2)
    victim = max(texts, key=lambda i: len(pieces[i]))
    original = pieces[victim]
    tail = original[len(original.rstrip()):]
    body = original.rstrip()
    over = len(hidden) - limit + len(note)
    for _ in range(4):
        cut = body[:max(len(body) - over, 0)].rstrip()
        pieces[victim] = (cut + note if cut else "") + tail
        hidden = hide("".join(pieces), style)
        if len(hidden) <= limit or not cut:
            break
        over += len(hidden) - limit
    return hidden


# ─── find ─────────────────────────────────────────────────────────────

def _tokens(raw: str) -> list[str]:
    """Every whole-line HTML-comment token in `raw` (after `reveal`)."""
    return [m.group("x") for m in _whole_lines(_HTML_LINE, reveal(raw or ""))]


def _kind_matches(kind: str, token: str) -> bool:
    if kind == "review":
        return token in _REVIEW_TOKENS or (bool(_configured_token()) and token == _configured_token())
    if kind == "status":
        return token.startswith("celmis:review-status:")
    if kind == "summary":
        return token.startswith("celmis:summary:")
    if kind == "finding":
        return token == "celmis:finding" or token.startswith("celmis:finding ")
    if kind == "chat:v1":
        return token == "celmis:chat:v1" or token.startswith("celmis:chat:v1 ")
    return False


_KINDS: Final[tuple[str, ...]] = ("review", "status", "summary", "chat:v1", "finding")


def _literal(kind: str) -> str:
    """`kind` as a bare token when it is a whole marker (`<!-- x -->`), else `kind` itself."""
    found = _HTML_ANY.fullmatch(kind.strip())
    return found.group("x") if found else kind.strip()


def has_marker(raw: str, kind: str) -> bool:
    """Does `raw` carry a marker of `kind` on a line of its own?

    `kind` is one of `review`, `status`, `summary`, `chat:v1`, `finding` — or a
    literal marker (`<!-- celmis:review-status:feedback -->`, or the install's
    configured review marker), matched exactly. Hidden markers are revealed
    first; a line quoted with `>` never matches.
    """
    if kind in _KINDS:
        return any(_kind_matches(kind, t) for t in _tokens(raw))
    literal = kind.strip()
    if not literal:
        return False
    if _HTML_ANY.fullmatch(literal):
        return _literal(literal) in _tokens(raw)
    # A configured marker that is not an HTML comment: the whole line, as written.
    return any(line.strip() == literal for line in reveal(raw or "").splitlines())


#: What every conversational reply carries (a command's answer, a chat answer).
#: NOT the review marker: cleanup of an earlier review deletes comments of ours
#: that carry that one, and a person's thread must survive a push.
CHAT_MARKER: Final = "<!-- celmis:chat:v1 -->"


def with_chat_marker(text: str) -> str:
    """`text` with the chat marker on a line of its own (once)."""
    if has_marker(text, "chat:v1"):
        return text
    return f"{(text or '').rstrip()}\n\n{CHAT_MARKER}\n"


def is_bot_text(raw: str) -> bool:
    """Is this text something Celmis wrote — any kind of marker at all?"""
    return any(_kind_matches(k, t) for t in _tokens(raw) for k in _KINDS)


def finding_marker(fp: str, sha: str) -> str:
    """The marker an inline finding carries: its fingerprint and the commit it was posted on."""
    fp, sha = str(fp or "").lower()[:16], str(sha or "").lower()[:12]
    if not re.fullmatch(r"[0-9a-f]{16}", fp) or not re.fullmatch(r"[0-9a-f]{12}", sha):
        raise ValueError("a finding marker needs a 16-hex fingerprint and a 12-hex commit")
    return f"<!-- celmis:finding fp={fp} sha={sha} -->"


def parse_finding_marker(raw: str) -> tuple[str, str] | None:
    """(fingerprint, commit) of the finding marker in `raw`, or None."""
    for token in _tokens(raw):
        found = _FINDING.match(token)
        if found:
            return found.group("fp"), found.group("sha")
    return None


# ─── Bitbucket flavour ────────────────────────────────────────────────

_CODE_SPAN = re.compile(r"(`[^`\n]*`)")
_SUB = re.compile(r"<sub>(.*?)</sub>", re.DOTALL | re.IGNORECASE)
_SUMMARY_TAG = re.compile(
    r"<summary>\s*(.*?)\s*</summary>[ \t]*\n*", re.DOTALL | re.IGNORECASE)
_DETAILS_OPEN = re.compile(r"<details[^>]*>[ \t]*\n?", re.IGNORECASE)
_DETAILS_CLOSE = re.compile(r"\n?[ \t]*</details>", re.IGNORECASE)
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


def _flavour_prose(chunk: str) -> str:
    """One run of prose, its inline code already lifted out into `\0n\0` slots."""
    chunk = _SUB.sub(
        lambda m: f"_{m.group(1).strip()}_" if m.group(1).strip() else "", chunk)
    chunk = _SUMMARY_TAG.sub(lambda m: "**" + m.group(1) + "**\n\n", chunk)
    chunk = _DETAILS_OPEN.sub("", chunk)
    chunk = _DETAILS_CLOSE.sub("", chunk)
    return chunk.replace("&amp;", "&")


def bitbucket_flavour(text: str) -> str:
    """Bitbucket-safe markdown: the few HTML tags the formatters use, as markdown.

    Bitbucket Cloud shows raw HTML as text. The comment formatters keep
    writing `<sub>`, `<details>` and `<summary>` (right on GitHub and GitLab);
    this turns exactly that set into `_italic_` and `**bold**` and drops the
    wrapper. Fenced code blocks and inline code are never touched — a diff
    suggestion for an HTML file must stay the HTML it is. Markers are left to
    `hide`. Idempotent.
    """
    text = text or ""
    if "<" not in text and "&amp;" not in text:
        return text
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if not prose:
            return
        spans: list[str] = []

        def lift(found: re.Match[str]) -> str:
            spans.append(found.group(0))
            return f"\0{len(spans) - 1}\0"

        lifted = _flavour_prose(_CODE_SPAN.sub(lift, "\n".join(prose)))
        out.append(re.sub(r"\0(\d+)\0", lambda m: spans[int(m.group(1))], lifted))
        prose.clear()

    for line in text.split("\n"):
        opened = _FENCE.match(line)
        if fence is None and opened:
            flush()
            fence = opened.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            if opened and opened.group(1) == fence:
                fence = None
        else:
            prose.append(line)
    flush()
    return "\n".join(out)


_TAG = re.compile(r"<[^>]*>")


def leaks_marker(html: str, sent: str) -> bool:
    """Does a rendered `content.html` show, as text, a marker that `sent` carried?

    Bitbucket answers a comment write with the HTML it rendered. If a token of
    ours is in the text of that HTML, the style used to hide it did not work.
    """
    visible = _TAG.sub("", html or "")
    if not visible.strip():
        return False
    if "[//]: #" in visible:
        return True
    return any(t in visible for t in _tokens(sent) if _ours(t))
