"""A stack trace reached Slack and a model with the connection string in it.

An alert body is written by somebody else's monitoring, about a failure. That
is precisely the text most likely to carry a secret: a connection string in a
`could not connect to` line, an Authorization header in a dumped request, a
token in an environment dump.

Celmis has a redactor — `src/security/redactor.py`, built to strip secrets out
of code before it is sent to a model, fail-closed by design. The alert path
did not call it. `grep -c redact` returned 0 in alerts.py, dispatch.py and
runner.py, and the body went three places verbatim: into `incoming_alerts`,
out to Slack, Discord or Google Chat, and — when somebody presses Fix with
Claude — into the prompt.

TWO NEIGHBOURS OF THE SAME HOLE.

`incoming_alerts` had no DELETE and no retention. Whatever arrived stayed for
the life of the installation, which turns a transient leak into a permanent
one and makes an erasure request unanswerable.

And `/webhook/` was exempt from rate limiting, with the comment "HMAC + dedup
already guard these". True of the git webhooks it was written for. Not true of
`/webhook/alerts/{token}`, which has neither: the token is compared, not a
signature over the body, and nothing dedupes. A monitoring system in a loop —
or anyone holding the token — could write unboundedly into a table with no
retention, and fan every one of them out to a chat room.
"""
from __future__ import annotations

# ─── the redaction ───────────────────────────────────────────────────

def test_a_secret_in_an_alert_body_does_not_reach_the_database(monkeypatch):
    from src.api.routers import alerts as mod

    rows = []
    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")

    class _Session:
        def add(self, row): rows.append(row)
        async def commit(self): return None

    class _Req:
        headers = {"host": "celmis.example.com"}
        url = type("U", (), {"scheme": "http"})()
        async def json(self):
            return {
                "title": "checkout cannot reach the database",
                "body": "OperationalError: could not connect to "
                        "postgresql://celmis:hunter2XYZ@db.internal:5432/prod",
                "severity": "critical",
            }

    import asyncio

    from fastapi import BackgroundTasks
    asyncio.run(mod.ingest("ws-1.s3cret", _Req(), BackgroundTasks(), _Session()))

    assert len(rows) == 1
    stored = f"{rows[0].title} {rows[0].body}"
    assert "hunter2XYZ" not in stored, (
        "the password was written to incoming_alerts verbatim"
    )


def test_the_redacted_body_is_what_gets_dispatched(monkeypatch):
    """Redacting the stored row and sending the original would be worse than
    not redacting at all: it would look done."""
    from src.api.routers import alerts as mod

    sent = []
    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")

    class _Session:
        def add(self, row): pass
        async def commit(self): return None

    class _Req:
        headers = {"host": "h"}
        url = type("U", (), {"scheme": "http"})()
        async def json(self):
            return {"title": "t", "body": "token=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "severity": "error"}

    import src.notifications as notifications
    monkeypatch.setattr(notifications, "notify", lambda **kw: sent.append(kw) or 1)

    import asyncio

    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    asyncio.run(mod.ingest("ws-1.s3cret", _Req(), bg, _Session()))
    for task in bg.tasks:
        asyncio.run(task())

    assert sent, "nothing was dispatched"
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in sent[0]["body_md"]


def test_ordinary_text_survives_redaction(monkeypatch):
    """A redactor that eats the message is a redactor nobody keeps on."""
    from src.api.routers.alerts import _redact_alert

    title, body = _redact_alert(
        "checkout: unhandled exception in settle()",
        "CRITICAL KeyError 'currency' at src/checkout/settle.py:47",
    )
    assert "settle()" in title
    assert "src/checkout/settle.py:47" in body


def test_redaction_failure_does_not_lose_the_alert(monkeypatch):
    """Fail-closed on the SECRET, not on the alert.

    The redactor is fail-closed by design — it raises rather than pass text
    through unchecked. On this path an exception must still not drop the
    alert: an alert that never arrives is the failure the whole feature
    exists to prevent. It is stored with the text withheld instead.
    """
    from src.api.routers import alerts as mod

    def _boom(*a, **k):
        raise RuntimeError("detect-secrets exploded")

    monkeypatch.setattr("src.security.redactor.redact", _boom)
    title, body = mod._redact_alert("t", "sensitive")
    assert "sensitive" not in body
    assert body, "the alert lost its body entirely"


# ─── retention ───────────────────────────────────────────────────────

def test_incoming_alerts_have_a_retention_window():
    """Kept for ever turns a transient leak into a permanent one."""
    import inspect

    from src.api.routers import alerts as mod

    src = inspect.getsource(mod)
    assert "purge_expired_alerts" in src, (
        "nothing deletes incoming alerts; they accumulate for the life of the "
        "installation"
    )


def test_the_purge_is_actually_scheduled():
    """A purge nothing calls is a function, not a retention policy."""
    import inspect

    from src.ownership import scheduler

    assert "purge_expired_alerts" in inspect.getsource(scheduler), (
        "the retention sweep is never run"
    )


# ─── rate limiting ───────────────────────────────────────────────────

def test_the_alert_ingest_is_not_exempt_from_rate_limiting():
    """The exemption was written for git webhooks and reasoned from HMAC.

    `/webhook/alerts/{token}` has no signature over the body and no dedup, so
    neither half of that reasoning applies to it.
    """
    from src.api.middleware import _EXEMPT_PREFIXES, _is_exempt

    assert not _is_exempt("/webhook/alerts/ws-1.secret"), (
        "the alert ingest is still exempt from rate limiting"
    )
    assert _is_exempt("/webhook/github/ws-1"), (
        "git webhooks lost their exemption; a burst from GitHub will be dropped"
    )
    assert any("/webhook/" in p for p in _EXEMPT_PREFIXES), (
        "the git webhook exemption was removed wholesale"
    )
