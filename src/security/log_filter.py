"""Secrets must not reach the log, and no call site can be trusted to remember.

WHY THIS EXISTS. `_remote_branch_exists` logs a git failure like this:

    logger.warning("ls_remote_failed url=%s branch=%s err=%s ...",
                   strip_credentials(url), branch, e)

The url is stripped. `e` is not — and `subprocess.TimeoutExpired.__str__` is
``"Command '%s' timed out after %s seconds" % (self.cmd, self.timeout)``, where
`self.cmd` is the argv that CONTAINS the same url, tokenised. One line, the
same secret, redacted in one argument and printed in the next.

That is not a slip somebody made once. Any exception raised by a subprocess or
by GitPython carries the command line it was built from, and every
`logger.exception` renders a traceback in which the whole chain — including a
`GitCommandError` whose `cmdline:` holds `https://x-access-token:ghs_…@…` — is
formatted verbatim. There are dozens of such call sites and every future one
starts out unsafe. Redaction at the call site is a rule people have to keep;
redaction at the handler is a property of the process.

WHAT IT COVERS. The message, the `%`-args (before formatting, so a secret is
never assembled into a string in the first place), the exception traceback,
and the stack info. Traceback redaction works by filling `record.exc_text`
ourselves: `logging.Formatter.format` calls `formatException` only when that
field is still empty, so pre-filling it is what makes a Filter — which nothing
else lets you use to rewrite a traceback — able to reach one.

WHAT IT DOES NOT COVER. A secret that is not shaped like one. The patterns
here match strings that announce themselves (`ghp_`, `sk-ant-`, `AIza`,
`Bearer `, a URL with a password in it); the entropy-based patterns are
deliberately left out because in a log they match every git SHA. This is a
backstop, not a licence to log credentials on purpose.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from src.security.patterns import LOG_REDACTION_PATTERNS

#: Marker left in place of anything that matched.
PLACEHOLDER = "[REDACTED]"

_INSTALLED_ATTR = "_celmis_redaction_installed"


def redact_text(text: str) -> str:
    """Every known secret shape in `text`, replaced."""
    for pattern in LOG_REDACTION_PATTERNS.values():
        text = pattern.sub(PLACEHOLDER, text)
    return text


def _redact_value(value: Any) -> Any:
    """Redact a `%`-arg without changing what it is.

    Strings are redacted directly. Everything else is left alone UNLESS its
    `str()` contains something — an exception object is the case that matters,
    since that is exactly how `TimeoutExpired` smuggles an argv through — in
    which case it is replaced by its redacted text. Ints, floats, paths and the
    like round-trip untouched so log formatting (`%d`, `%.2f`) still works.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bool, int, float, type(None))):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — a repr that raises must not kill the log
        return value
    cleaned = redact_text(text)
    return cleaned if cleaned != text else value


class RedactingFilter(logging.Filter):
    """Attach to a HANDLER, not to a logger.

    A filter on a logger only sees records logged through that logger; a
    filter on a handler sees every record that reaches it, including everything
    that propagated up from the hundreds of module loggers in this codebase.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            elif record.msg is not None:
                record.msg = _redact_value(record.msg)

            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _redact_value(v)
                                   for k, v in record.args.items()}
                else:
                    record.args = tuple(_redact_value(a) for a in record.args)

            if record.exc_info and not record.exc_text:
                # Fill it ourselves so the Formatter does not: it only calls
                # formatException when this is empty, and a traceback is where
                # a chained GitCommandError prints its full command line.
                record.exc_text = redact_text(
                    "".join(traceback.format_exception(*record.exc_info))
                )
            elif record.exc_text:
                record.exc_text = redact_text(record.exc_text)

            if record.stack_info:
                record.stack_info = redact_text(record.stack_info)
        except Exception:  # noqa: BLE001
            # Fail closed. A log line we could not clean is a log line that
            # might carry a token, and this filter exists precisely because
            # nobody can say which one. Keep the level, the logger and the
            # location — drop the content.
            record.msg = (
                f"[log line dropped: redaction failed] logger={record.name}"
            )
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> int:
    """Attach the filter to every handler currently configured.

    Returns how many handlers were newly covered. Idempotent: a handler is
    marked once, so calling this from both the API startup and the CLI (or
    twice from either) does not stack filters.

    Called for its effect on handlers that EXIST at call time. Under uvicorn
    that is the right moment — its handlers are configured before the app is
    imported — and anything that adds a handler later (a test, a script) is
    expected to call this again.
    """
    covered = 0
    handlers: list[logging.Handler] = []
    roots = [logger] if logger is not None else [logging.getLogger()]
    if logger is None:
        for name in list(logging.Logger.manager.loggerDict):
            obj = logging.Logger.manager.loggerDict.get(name)
            if isinstance(obj, logging.Logger):
                roots.append(obj)
    for lg in roots:
        handlers.extend(lg.handlers)
    for handler in handlers:
        if getattr(handler, _INSTALLED_ATTR, False):
            continue
        handler.addFilter(RedactingFilter())
        setattr(handler, _INSTALLED_ATTR, True)
        covered += 1
    return covered


__all__ = ["PLACEHOLDER", "RedactingFilter", "install_log_redaction", "redact_text"]
