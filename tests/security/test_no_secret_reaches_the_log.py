"""A token must not survive a trip through the logging module.

FOUND IN THE SOURCE, and the shape of it is the reason this is a handler-level
guard rather than a fixed call site:

    logger.warning("ls_remote_failed url=%s branch=%s err=%s ...",
                   strip_credentials(url), branch, e)

`url` is stripped. `e` is a `subprocess.TimeoutExpired`, whose `__str__` is
``"Command '<argv>' timed out after Ns"`` — and that argv is the same command,
built two lines earlier from the same tokenised URL. The secret was removed
from the first argument and printed in full in the third, on one line.

Every subprocess exception and every GitPython error carries the command line
it was built from, and `logger.exception` renders the whole chain verbatim.
There are dozens of such call sites. Remembering to redact at each one is a
rule people keep until they don't; redacting at the handler is a property of
the process.

Both layers are tested, and tested SEPARATELY — the call-site tests here run
with no filter installed, so a regression in one is not masked by the other.
"""

from __future__ import annotations

import io
import logging
import subprocess

import pytest

from src.security.log_filter import RedactingFilter, install_log_redaction

TOKEN = "ghp_" + "A" * 36
URL = f"https://x-access-token:{TOKEN}@github.com/acme/worker.git"
ANTHROPIC = "sk-ant-" + "B" * 60
GOOGLE = "AIza" + "C" * 35


@pytest.fixture
def captured():
    """A logger with the filter installed and nothing else attached."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("celmis.test.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    installed = install_log_redaction(logger)
    assert installed == 1
    yield logger, buf
    logger.handlers = []


# ─── the filter ──────────────────────────────────────────────────────


def test_a_token_in_the_message_is_removed(captured):
    logger, buf = captured
    logger.info("cloning %s", URL)
    assert TOKEN not in buf.getvalue()


def test_a_token_in_the_format_string_itself_is_removed(captured):
    """Someone f-strings the URL into the message instead of passing an arg."""
    logger, buf = captured
    logger.info(f"cloning {URL}")  # noqa: G004
    assert TOKEN not in buf.getvalue()


def test_the_exact_production_line_is_clean(captured):
    """The real call, with the real exception type."""
    logger, buf = captured
    exc = subprocess.TimeoutExpired(
        ["git", "ls-remote", "--heads", URL, "main"], 15)
    logger.warning("ls_remote_failed url=%s branch=%s err=%s",
                   "https://[REDACTED]@github.com/acme/worker.git", "main", exc)
    out = buf.getvalue()
    assert TOKEN not in out
    # And it is still a usable log line, not a blank one.
    assert "ls_remote_failed" in out and "branch=main" in out


def test_a_token_inside_a_traceback_is_removed(captured):
    """`logger.exception` formats the whole chain. A GitCommandError two links
    down prints `cmdline: git clone … https://x-access-token:…@…`."""
    logger, buf = captured
    try:
        raise RuntimeError(f"fatal: could not read from {URL}")
    except RuntimeError:
        logger.exception("clone_failed")
    out = buf.getvalue()
    assert TOKEN not in out
    assert "RuntimeError" in out, "the traceback itself must survive"


def test_a_chained_cause_is_redacted_too(captured):
    logger, buf = captured
    try:
        try:
            raise ValueError(f"git clone {URL}")
        except ValueError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError:
        logger.exception("clone_failed")
    out = buf.getvalue()
    assert TOKEN not in out
    assert "ValueError" in out


@pytest.mark.parametrize("secret", [TOKEN, ANTHROPIC, GOOGLE,
                                    "Bearer " + "D" * 40,
                                    "postgresql://u:hunter2@db:5432/celmis"])
def test_every_provider_shape_is_caught(captured, secret):
    logger, buf = captured
    logger.error("boom %s", secret)
    out = buf.getvalue()
    needle = secret.split(":")[-1] if secret.startswith("postgres") else secret
    assert needle not in out


def test_dict_style_args_are_redacted(captured):
    logger, buf = captured
    logger.info("cloning %(url)s", {"url": URL})
    assert TOKEN not in buf.getvalue()


# ─── what it must NOT eat ────────────────────────────────────────────


def test_a_git_sha_is_not_a_secret(captured):
    """`aws-secret` and `high-entropy-base64` match any 40-character run of
    base64 alphabet, which is every git SHA and every content hash we print on
    purpose. They are excluded from the log set for this reason; a filter that
    redacts the SHAs makes the log unreadable and protects nothing."""
    logger, buf = captured
    sha = "9c25b4d9" + "a1b2c3d4" * 4
    logger.info("indexed repo=%s sha=%s symbols=%d", "acme/worker", sha[:40], 317)
    out = buf.getvalue()
    assert sha[:40] in out
    assert "symbols=317" in out


def test_ordinary_numbers_survive_formatting(captured):
    """Args are redacted BEFORE `%` formatting, so a redactor that returned
    strings for everything would break `%d` and `%.2f`."""
    logger, buf = captured
    logger.info("took %.2fs over %d files", 1.5, 42)
    assert "took 1.50s over 42 files" in buf.getvalue()


def test_a_plain_repo_url_is_left_alone(captured):
    logger, buf = captured
    logger.info("cloning %s", "https://github.com/acme/worker.git")
    assert "https://github.com/acme/worker.git" in buf.getvalue()


# ─── failure behaviour ───────────────────────────────────────────────


def test_redaction_failure_drops_the_line_rather_than_leaking_it():
    """Fail closed, like the code redactor. A line that could not be cleaned
    might be carrying the thing this filter exists to stop."""
    import src.security.log_filter as mod

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("celmis.test.redaction.broken")
    logger.handlers = [handler]
    logger.propagate = False

    original = mod.redact_text
    mod.redact_text = lambda _t: (_ for _ in ()).throw(RuntimeError("nope"))
    try:
        logger.error("token is %s", TOKEN)
    finally:
        mod.redact_text = original
        logger.handlers = []

    out = buf.getvalue()
    assert TOKEN not in out
    assert "redaction failed" in out


def test_installing_twice_does_not_stack_filters():
    logger = logging.getLogger("celmis.test.redaction.idempotent")
    handler = logging.StreamHandler(io.StringIO())
    logger.handlers = [handler]
    try:
        assert install_log_redaction(logger) == 1
        assert install_log_redaction(logger) == 0
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        logger.handlers = []


# ─── the call site, with no filter in the way ────────────────────────


def test_the_clone_call_site_strips_its_own_exception(monkeypatch, caplog):
    """No filter installed here on purpose. The backstop and the call site are
    two defences, and a test that exercises both at once can only tell you that
    at least one of them works."""
    from src.sync import clone as clone_mod

    def _boom(*_a, **_kw):
        raise subprocess.TimeoutExpired(
            ["git", "ls-remote", "--heads", URL, "main"], 15)

    monkeypatch.setattr(clone_mod.subprocess, "run", _boom)
    with caplog.at_level(logging.WARNING, logger="src.sync.clone"):
        assert clone_mod._remote_branch_exists(URL, "main") is True

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN not in joined, joined
    assert "ls_remote_failed" in joined


# ─── the wiring ──────────────────────────────────────────────────────


def test_the_api_covers_handlers_that_appear_after_import():
    """Two installs, and the second one is the one that matters in production.

    Under uvicorn the app module is imported first and logging is reconfigured
    around startup, so a filter attached only at import time can end up on a
    handler that is then replaced. The startup hook runs it again. This drives
    the real startup event and checks a handler added in between is covered.
    """
    import io
    import logging

    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.security.log_filter import RedactingFilter

    late = logging.StreamHandler(io.StringIO())
    root = logging.getLogger()
    root.addHandler(late)
    try:
        with TestClient(app):  # runs the startup event
            pass
        assert any(isinstance(f, RedactingFilter) for f in late.filters), (
            "a handler configured after import was left unredacted"
        )
    finally:
        root.removeHandler(late)
