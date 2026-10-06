"""What a pull-request comment asks the bot to do.

Pure text in, a `ParsedCommand` (or None) out: no I/O, no settings, so the
webhook receiver can run it as a cheap prefilter before it touches the
database, and the tests can run a whole matrix of comments through it.

A comment addresses the bot by starting a token with the handle (`@celmis`,
or the `/celmis` alias; both follow `REVIEW_BOT_HANDLE`). What follows is one
of:

    @celmis                           -> help
    @celmis help                      -> help
    @celmis start-review              -> review what is new since the last review (resumes a pause)
    @celmis review [--force|-f]       -> the same; `--force` reads the whole pull request again
    @celmis remember [--repo|--org|--dir[=path]]: <rule>
    @celmis -v business-logic PROJ-123   (or a link to the task)
    @celmis <anything else>           -> a question about the change (chat)

A command word counts as a command only when nothing but recognised flags
follows it on the line. "@celmis review this function for races" is a
question, not a review request.

What the parser never reads: fenced code, inline code and `>`-quoted lines.
Quoting a comment that mentions the bot (the way a reply on every platform
starts) must not run its command a second time, and neither must an example
in a code block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.review import messages

#: The command names the parser can produce. `chat` is the fallback.
START_REVIEW = "start-review"
REVIEW = "review"
HELP = "help"
REMEMBER = "remember"
BUSINESS_LOGIC = "business-logic"
CHAT = "chat"

COMMAND_NAMES: tuple[str, ...] = (START_REVIEW, REVIEW, HELP, REMEMBER, BUSINESS_LOGIC)

#: The longest text kept for a question or a rule; the rest is cut.
MAX_ARGS_CHARS = 4000

_FENCE = re.compile(r"^[ \t]*(?P<f>`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)[^`\n]*?\1")
_FORCE_FLAGS = frozenset({"--force", "-f"})
_TRAILING_PUNCT = ".!?,;"
_REMEMBER = re.compile(
    r"^remember(?P<flags>(?:[ \t]+--(?:repo|org|dir(?:=[^\s:]+)?))*)"
    r"[ \t]*:?[ \t]*(?P<text>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_TASK_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
# The task is a key (`PROJ-123`) or a link (`https://…/browse/PROJ-123`, a
# Confluence page): the link keeps its case, the key is upper-cased below.
_BUSINESS_LOGIC = re.compile(
    r"^-v[ \t]+business-logic(?:[ \t]+<?(?P<task>[A-Za-z][A-Za-z0-9_]*-\d+|https://[^\s<>]+)>?)?"
    r"[ \t]*[.!]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedCommand:
    """One request found in a comment."""

    name: str
    #: `--force` / `-f` was given.
    force: bool = False
    #: The rule (remember), the ticket key (business-logic) or the question (chat).
    args: str = ""
    #: remember: where the rule applies — `repo`, `org` or `dir`.
    scope: str = ""
    #: remember --dir=path: the directory the rule is limited to.
    path: str = ""


def handle_name(handle: str) -> str:
    """`@celmis` or `/celmis` -> `celmis`."""
    return (handle or "").strip().lstrip("@/").lower()


def might_address_bot(text: str, handle: str) -> bool:
    """A cheap yes/no that is safe to run on every comment of every pull request.

    False negatives are impossible (the parser needs the handle's name to be in
    the text); false positives are fine, `parse_comment` decides.
    """
    name = handle_name(handle)
    return bool(name) and name in (text or "").lower()


def _prose(text: str) -> str:
    """`text` without fenced code, inline code and quoted lines."""
    out: list[str] = []
    fence: str | None = None
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        found = _FENCE.match(line)
        if fence is not None:
            if found and found.group("f")[0] == fence[0] and len(found.group("f")) >= len(fence):
                fence = None
            continue
        if found:
            fence = found.group("f")
            continue
        if line.lstrip().startswith(">"):
            continue
        out.append(_INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def _mention(name: str) -> re.Pattern[str]:
    # A mention starts a token: not glued to a word, an address (`a@celmis.io`),
    # a path (`src/celmis`) or a URL — and does not run on into a longer name
    # (`@celmis-bot`, `@celmisfan`) or a domain (`@celmis.io`).
    return re.compile(
        rf"(?<![\w@/.\-])[@/]{re.escape(name)}(?![\w\-]|\.\w)", re.IGNORECASE,
    )


def parse_comment(text: str, handle: str = "@celmis") -> ParsedCommand | None:
    """The request a comment makes of the bot, or None when it makes none."""
    name = handle_name(handle)
    if not name:
        return None
    prose = _prose(text)
    found = _mention(name).search(prose)
    if found is None:
        return None
    rest = prose[found.end():].strip()
    if not rest:
        return ParsedCommand(HELP)
    first, _, more = rest.partition("\n")
    first = first.strip().lstrip(":,").strip()
    if not first:
        # "@celmis" alone on a line, the question underneath.
        return _chat(rest)

    tokens = first.split()
    word = tokens[0].lower().rstrip(_TRAILING_PUNCT)

    if word == HELP and _only(tokens[1:], ()):
        return ParsedCommand(HELP)

    if word in (START_REVIEW, "start", REVIEW):
        flags = [tok.lower().rstrip(_TRAILING_PUNCT) for tok in tokens[1:]]
        if _only(flags, _FORCE_FLAGS):
            command = REVIEW if word == REVIEW else START_REVIEW
            return ParsedCommand(command, force=any(f in _FORCE_FLAGS for f in flags))
        return _chat(rest)

    if word == REMEMBER or word.startswith(REMEMBER + ":"):
        parsed = _REMEMBER.match(rest)
        if parsed is not None:
            return _remember(parsed)
        return _chat(rest)

    if word == "-v" or word == BUSINESS_LOGIC:
        line = first if word == "-v" else f"-v {first}"
        parsed = _BUSINESS_LOGIC.match(line)
        if parsed is not None:
            task = (parsed.group("task") or "")
            if _TASK_KEY.match(task):
                task = task.upper()
            return ParsedCommand(BUSINESS_LOGIC, args=task[:MAX_ARGS_CHARS])
        return _chat(rest)

    return _chat(rest)


def _only(tokens: Iterable[str], allowed: Iterable[str]) -> bool:
    """Every token is one of `allowed` (punctuation-only tokens are noise)."""
    allowed = frozenset(allowed)
    return all(tok in allowed or not tok.strip(_TRAILING_PUNCT) for tok in tokens)


def _remember(parsed: re.Match[str]) -> ParsedCommand:
    flags = parsed.group("flags").split()
    scope, path = "repo", ""
    for flag in flags:
        flag = flag.lower()
        if flag == "--org":
            scope = "org"
        elif flag == "--repo":
            scope = "repo"
        elif flag.startswith("--dir"):
            scope = "dir"
            path = flag.partition("=")[2].strip("/")
    return ParsedCommand(
        REMEMBER, args=parsed.group("text").strip()[:MAX_ARGS_CHARS],
        scope=scope, path=path,
    )


def _chat(rest: str) -> ParsedCommand:
    return ParsedCommand(CHAT, args=rest.strip()[:MAX_ARGS_CHARS])


# ─── The help text ───────────────────────────────────────────────

#: The guide line of each command, in the order they are listed.
_GUIDE_KEYS: tuple[tuple[str, str], ...] = (
    (START_REVIEW, "guide.start_review"),
    (REVIEW, "guide.review_force"),
    (HELP, "guide.help"),
    (REMEMBER, "guide.remember"),
    (BUSINESS_LOGIC, "guide.business_logic"),
)


def command_lines(
    handle: str, language: str | None = None, *, available: Iterable[str] | None = None,
) -> list[str]:
    """One bullet per command that is installed, then the "ask a question" line.

    `available` names the commands that are registered; None lists them all.
    The guide never advertises a command the installation cannot run.
    """
    names = None if available is None else set(available)
    lines = [
        f"- {messages.t(key, language, handle=handle)}"
        for name, key in _GUIDE_KEYS
        if names is None or name in names
    ]
    if names is None or CHAT in names:
        lines.extend(["", messages.t("guide.ask", language, handle=handle)])
    return lines


def guide_lines(
    handle: str, language: str | None = None, *, available: Iterable[str] | None = None,
) -> list[str]:
    """The block the review summary carries under `commands_guide_enabled`."""
    return [
        messages.t("guide.title", language), "",
        *command_lines(handle, language, available=available), "",
    ]


def help_markdown(
    handle: str, language: str | None = None, *, available: Iterable[str] | None = None,
) -> str:
    """The reply to `@celmis help`."""
    return "\n".join([
        messages.t("command.help_title", language), "",
        *command_lines(handle, language, available=available), "",
        messages.t("command.help_footer", language, handle=handle),
    ])
