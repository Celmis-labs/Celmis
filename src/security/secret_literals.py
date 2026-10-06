"""Secret literals the repository's generic code redactor lets through.

`src.security.redactor` finds provider tokens, private keys, DSNs with a user
and a password and quoted ``SECRET_KEY = '...'`` assignments. The secrets that
are committed most often look different: ``DB_PASSWORD=value`` in an env-style
file, ``password: value`` in YAML, ``password='value'`` as a keyword argument,
``redis://:value@host`` (no user name), a JSON ``"token": "value"``, a default
in ``os.environ.get("X_PASSWORD", "value")``. This module masks those.

It is the floor under every outgoing text that carries repository source: the
dev profile's redaction hook (`emit._redact`), `howto`, the PR chat, the code
Q&A agent and the cross-repo drift comments (`mcp_redact.redact_for_mcp_floored`).
It always runs on top of the central layer, so none of them depends on that
layer's rules alone to be safe. It errs on the side of
masking a value: a name that says "password" next to a literal is masked
unless the literal is plainly a pointer (a variable, an env lookup, a
placeholder like ``changeme`` or ``<password>``).

A value is replaced by ``[REDACTED:literal]``; the name, the operator and the
quotes stay, so the code still reads (``password='[REDACTED:literal]'``) and
the caller can see WHICH setting carries a secret.
"""

from __future__ import annotations

import re

MASK = "[REDACTED:literal]"

# A name is secret-bearing when it says so ...
_NAME_KW = re.compile(
    r"(?i)pass(?:word|wd|phrase)|(?<![a-z])pass(?![a-z])|pwd|secret|credential"
    r"|(?<![a-z])token(?!s|iz)|api[_\-]?key|private[_\-]?key|access[_\-]?key"
    r"|auth[_\-]?key|signing[_\-]?key|encryption[_\-]?key|master[_\-]?key|client[_\-]?key",
)
# ... unless it names WHERE the secret lives, not the secret itself.
_NAME_POINTER = re.compile(
    r"(?i)(?:[_\-.]|(?<=[a-z]))(?:file|path|env|name|ref|header|field|type|length|ttl|"
    r"expires?|expiry|url|uri|endpoint|dir|provider|timeout|count|command|cmd|prefix|"
    r"mount|volume|store|manager|policy|label|key_?id|id)$",
)

_ASSIGN = re.compile(
    r"""(?P<name>[A-Za-z_][\w.\-]*)(?P<nq>["']?)
        (?:[ \t]*:[ \t]*(?P<type>[A-Za-z_][\w.\[\], |]*?))?
        [ \t]*(?P<op>:=|=>|[:=])(?![=>])[ \t]*
        (?P<val>"[^"\n]*"?|'[^'\n]*'?|`[^`\n]*`?|[^\s,;)\]}]+)""",
    re.VERBOSE,
)
_LINE_PREFIX = re.compile(r"^\s*(?:-\s+|export\s+|ENV\s+|ARG\s+|set\s+|declare\s+-x\s+)?$")
_REST_OF_LINE = re.compile(r"^\s*(?:#.*|//.*)?$")
_PLACEHOLDER_WORD = re.compile(
    r"(?i)^(?:change[_\- ]?me.*|changeit|your[_\- ].*|x{3,}|\*{3,}|\.{3,}|example.*|todo.*|"
    r"replace.*|secret|password|passwd|pass|token|dummy.*|placeholder.*|none|null|nil|"
    r"true|false|yes|no|on|off|undefined|redacted|masked|hidden|test|testing|empty)$",
)
_TYPE_WORDS = re.compile(
    r"(?i)^(?:str|string|int|integer|bool|boolean|bytes|secretstr|secret|optional(?:\[.*)?|"
    r"any|object|number|float|char\*?|\*?string|\[\]byte|ref|required|text)$",
)
_REF_MARKERS = ("${", "{{", "$(", "os.environ", "getenv", "process.env", "env(", "[REDACTED")
_REF_PREFIXES = ("settings.", "config.", "self.", "cfg.", "secrets.", "this.", "args.", "opts.")
_REF_START = ("<", "%(", "#{", "{")
_SHELL_VAR = re.compile(r"^\$(?:[{(]|[A-Za-z_]\w*$)")
_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
_IDENT_CHAIN = re.compile(r"^[A-Za-z_][\w]*(?:(?:\.|->|::)[A-Za-z_]\w*)*(?:\(\)?)?$")
_SPECIAL = re.compile(r"[!@#$%^&*+/=~?]|\d")

# `://user:pw@host` and `://:pw@host`
_URL_PW = re.compile(
    r"(?P<pre>[A-Za-z][\w+.\-]*://[^\s/:@'\"]*:)(?P<pw>[^\s/@'\"]+)(?P<post>@)")
# os.environ.get("X_PASSWORD", "fallback"), getenv(...), env("X", "fallback")
_ENV_DEFAULT = re.compile(
    r"""(?P<pre>(?:getenv|environ\.get|env|config|get|getenv_or|or_else)\(\s*
        (?P<q1>["'])(?P<key>[^"'\n]*)(?P=q1)\s*,\s*(?P<q2>["']))(?P<val>[^"'\n]+)(?P<post>(?P=q2))""",
    re.VERBOSE,
)
# `|| "fallback"` after process.env.X_PASSWORD, `:-fallback` in ${X_PASSWORD:-fallback}
_JS_DEFAULT = re.compile(
    r"""(?P<pre>process\.env\.(?P<key>\w+)\s*(?:\|\||\?\?)\s*(?P<q>["']))(?P<val>[^"'\n]+)(?P<post>(?P=q))""")
_SHELL_DEFAULT = re.compile(r"(?P<pre>\$\{(?P<key>\w+):?[-=])(?P<val>[^}\n]+)(?P<post>\})")


def name_is_secret(name: str) -> bool:
    last = re.split(r"[.]", name)[-1] if name else ""
    if not _NAME_KW.search(name):
        return False
    return not _NAME_POINTER.search(last)


def _is_pointer(val: str, *, quoted: bool) -> bool:
    """True when `val` points at a secret (a variable, a lookup, a placeholder)."""
    v = val.strip()
    if not v or _PLACEHOLDER_WORD.match(v) or any(m in v for m in _REF_MARKERS):
        return True
    if _SHELL_VAR.match(v) or v.startswith(_REF_START):
        return True
    return not quoted and v.startswith(_REF_PREFIXES)


def _bare_is_secret(name: str, op: str, val: str, line_anchored: bool) -> bool:
    if _is_pointer(val, quoted=False) or _TYPE_WORDS.match(val):
        return False
    if _UPPER_SNAKE.match(val) and len(val) > 3 and not _SPECIAL.search(val.replace("_", "")):
        return False  # an env-var name, a constant
    if "(" in val or "[" in val or "->" in val or "::" in val:
        return False  # a call or lookup
    if line_anchored and (op == ":" or name.isupper() or (name.upper() == name)):
        return True  # `key: value` / `KEY=value`: a literal, whatever it looks like
    if _IDENT_CHAIN.match(val) and "." in val:
        return False  # an attribute chain
    # a bare lowercase word inside code is a variable; a digit or symbol is a literal
    return bool(_SPECIAL.search(val))


def _quoted_inner(val: str) -> tuple[str, str, str]:
    q = val[0]
    inner = val[1:]
    closed = inner.endswith(q) and len(inner) >= 1
    if closed:
        inner = inner[:-1]
    return q, inner, q if closed else ""


def mask_literals(text: str) -> str:
    """`text` with the committed-secret shapes masked; everything else untouched."""
    if not text:
        return text

    def assign(m: re.Match[str]) -> str | None:
        """Replacement for the match, or None when it is not a secret."""
        name = m.group("name")
        if not name_is_secret(name):
            return None
        val = m.group("val")
        op = m.group("op")
        lead = m.group(0)[: m.start("val") - m.start()]
        if val[0] in "\"'`":
            q, inner, _close = _quoted_inner(val)
            if _is_pointer(inner, quoted=True) or MASK in inner:
                return None
            # an unterminated literal (a line cut short) is closed so nothing is half-shown
            return f"{lead}{q}{MASK}{q}"
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        rest = text[m.end(): line_end if line_end >= 0 else len(text)]
        anchored = bool(_LINE_PREFIX.match(text[line_start: m.start()])) \
            and bool(_REST_OF_LINE.match(rest))
        if not _bare_is_secret(name, op, val, anchored):
            return None
        return f"{lead}{MASK}"

    pieces: list[str] = []
    pos = done = 0
    while (m := _ASSIGN.search(text, pos)) is not None:
        repl = assign(m)
        if repl is None:
            pos = m.start("val") + 1  # look inside the value too: `data = '{"password": "x"}'`
            continue
        pieces.append(text[done:m.start()])
        pieces.append(repl)
        pos = done = m.end()
    pieces.append(text[done:])
    out = "".join(pieces)

    def url(m: re.Match[str]) -> str:
        pw = m.group("pw")
        if _is_pointer(pw, quoted=False) or pw.startswith(("{", "$")):
            return m.group(0)
        return f"{m.group('pre')}{MASK}{m.group('post')}"

    out = _URL_PW.sub(url, out)

    def default(m: re.Match[str]) -> str:
        key = m.groupdict().get("key") or ""
        val = m.group("val")
        if not name_is_secret(key) or _is_pointer(val, quoted=True):
            return m.group(0)
        return f"{m.group('pre')}{MASK}{m.group('post')}"

    for rx in (_ENV_DEFAULT, _JS_DEFAULT, _SHELL_DEFAULT):
        out = rx.sub(default, out)
    return out
