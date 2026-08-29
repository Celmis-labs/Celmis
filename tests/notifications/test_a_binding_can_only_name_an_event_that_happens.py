"""The channel page offers five events. Two of them exist.

`compliance_failed`, `deprecation_used` and `apply_fix_applied` are in the
dropdown on /admin/notifications and are emitted by nothing. Binding a channel
to one of them produces a row in the Bindings table that looks configured,
reads as configured, and is silent for ever. The opposite drift is here too:
`agent_turn_done` is emitted by src/agent/runner.py and cannot be picked.

A silent channel is the worst failure an alerting feature has, because its
whole job is to be the thing that tells you. There is no error to notice —
the absence of a message looks exactly like the absence of a problem.

The cause is two lists of strings, one in TypeScript and one implied by
scattered call sites, with nothing holding them together. So the fix that
matters is not editing the list, it is this test: the dropdown must offer
exactly the events the code emits, and drift in either direction fails here
rather than in a room that stays quiet.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PAGE = ROOT / "web" / "app" / "(app)" / "admin" / "notifications" / "page.tsx"

#: `*` means "every event", not an event.
WILDCARD = "*"


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_a_notify_call(node: ast.Call) -> bool:
    """Both shapes that reach the dispatcher.

    Called directly — `notify(event=...)` — and handed to a runner:
    `asyncio.to_thread(notify, event=...)`, which src/agent/runner.py uses so
    the review pipeline is not blocked on an HTTP post. The second shape is
    still a call site; a detector that only knows the first reports a clean
    result for a file it cannot see, which is worse than no detector.
    """
    if _name_of(node.func) == "notify":
        return True
    return any(_name_of(arg) == "notify" for arg in node.args)


def _emitted_events() -> dict[str, list[str]]:
    """Every literal `event=` passed to `notify(...)`, by call site.

    Parsed, not grepped. A docstring in src/notifications/__init__.py shows a
    sample call with `event="breaking_change"` — text that looks exactly like
    a call site and is not one. `ast` cannot make that mistake; a regex over
    the file cannot avoid it.
    """
    found: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_a_notify_call(node):
                continue
            for kw in node.keywords:
                if kw.arg == "event" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    found.setdefault(kw.value.value, []).append(
                        f"{path.relative_to(ROOT)}:{node.lineno}"
                    )
    return found


def _offered_events() -> list[str]:
    """The `v:` values of EVENT_OPTIONS in the page."""
    text = PAGE.read_text(encoding="utf-8")
    block = re.search(r"const EVENT_OPTIONS\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert block, "EVENT_OPTIONS is gone from the notifications page"
    return re.findall(r'v:\s*"([^"]+)"', block.group(1))


@pytest.fixture(scope="module")
def emitted() -> dict[str, list[str]]:
    return _emitted_events()


@pytest.fixture(scope="module")
def offered() -> list[str]:
    return _offered_events()


def test_every_event_you_can_pick_is_one_the_code_emits(offered, emitted):
    phantom = [e for e in offered if e != WILDCARD and e not in emitted]
    assert not phantom, (
        "the notifications page offers events nothing emits: "
        + ", ".join(phantom)
        + " — a binding on one of these is silent for ever, and silence is "
          "indistinguishable from nothing having gone wrong"
    )


def test_every_event_the_code_emits_can_be_picked(offered, emitted):
    missing = [e for e in emitted if e not in offered]
    assert not missing, (
        "these events are emitted and cannot be bound to: "
        + ", ".join(f"{e} ({emitted[e][0]})" for e in missing)
    )


def test_the_wildcard_is_still_offered(offered):
    """`*` is how you catch an event added after your binding was made."""
    assert WILDCARD in offered


def test_the_list_is_not_empty_in_either_direction(offered, emitted):
    """A guard on the guards.

    Both comparisons above pass trivially if a parser silently returns
    nothing — a renamed constant, a moved page, an import that changes the
    call shape. Then this file would report success while checking air.
    """
    assert len(offered) > 1, "parsed no events from the page"
    assert emitted, "parsed no notify() call sites from src/"


# ─── the alerts that do not arrive ───────────────────────────────────

class _Req:
    """Just enough Request for the endpoint: a body and the proxy headers."""

    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {"host": "celmis.example.com",
                                   "x-forwarded-proto": "https"}
        self.url = type("U", (), {"scheme": "http"})()

    async def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


async def _ingest(monkeypatch, payload, sent):
    from fastapi import BackgroundTasks

    from src.api.routers import alerts as mod

    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")

    import src.notifications as notifications
    monkeypatch.setattr(notifications, "notify",
                        lambda **kw: sent.append(kw) or 1)

    bg = BackgroundTasks()
    out = await mod.ingest("ws-1.s3cret", _Req(payload), bg, _Session())
    for task in bg.tasks:          # what Starlette runs after the response
        await task()
    return out


@pytest.mark.asyncio
async def test_an_ingested_alert_reaches_the_dispatcher(monkeypatch):
    """The alert that arrives must leave again.

    It used to be written to `incoming_alerts` and go no further: the row
    carries `severity` and `repo_hint`, which are precisely the two fields the
    binding matcher gates on, so the data was being stored for a routing step
    nobody took. The result was an inbox you have to remember to open, which
    is the thing an alert exists to save you from.
    """
    sent: list[dict] = []
    out = await _ingest(monkeypatch, {
        "title": "checkout 5xx spike",
        "body": "error rate 12% over 5m",
        "severity": "critical",
        "repo": "celmis-codereviewer/celmis-demo-gateway",
    }, sent)

    assert out == {"ok": True, "created": 1}
    assert len(sent) == 1, "the alert was stored and never dispatched"
    assert sent[0]["event"] == "alert_received"
    assert sent[0]["severity"] == "critical"
    assert sent[0]["title"] == "checkout 5xx spike"


@pytest.mark.asyncio
async def test_the_alert_carries_the_repo_the_binding_gates_on(monkeypatch):
    """`repo_hint` must arrive as `repo_slug`, or a per-repo binding misses it.

    A binding scoped to one repository matches on repo_slug; passing None
    would deliver every alert to workspace-wide channels only, and silently
    never to the per-repo one somebody set up on purpose.
    """
    sent: list[dict] = []
    await _ingest(monkeypatch, {
        "title": "t", "body": "b", "severity": "error",
        "repo": "acme/payments",
    }, sent)
    assert sent[0]["repo_slug"] == "acme/payments"


@pytest.mark.asyncio
async def test_the_card_links_somewhere_the_reader_can_open(monkeypatch):
    """From the operator's configured origin.

    This test used to assert the link was built from the request headers,
    "because this box does not know its own public name". It does not — but
    neither does the sender get to tell it. The alert arrives from somebody
    else's monitoring over a token, and Host is theirs to write, so that
    mechanism put an attacker-chosen address behind the Open button of a card
    delivered into the workspace's chat room. See
    tests/security/test_the_sender_of_an_alert_does_not_choose_the_link.py.

    The property this test is named for survives: where the operator has said
    what the box is called, the card links there and the reader can open it.
    """
    from src import config

    monkeypatch.setattr(
        config, "get_settings",
        lambda: type("S", (), {"public_base_url": "https://celmis.example.com"})(),
    )
    sent: list[dict] = []
    await _ingest(monkeypatch, {"title": "t", "body": "b",
                                "severity": "info"}, sent)
    assert sent[0]["link_url"] == "https://celmis.example.com/alerts"


@pytest.mark.asyncio
async def test_a_broken_channel_never_reaches_the_sender(monkeypatch):
    """The monitoring system retries on anything but a 2xx.

    A chat webhook that is down must not turn one firing alert into a stream
    of duplicates, so dispatch happens after the response and swallows its own
    failures. Here the dispatcher raises and the endpoint has already answered.
    """
    from fastapi import BackgroundTasks

    from src.api.routers import alerts as mod
    monkeypatch.setattr(mod, "_load_secret", lambda ws: "s3cret")

    import src.notifications as notifications

    def _boom(**kw):
        raise RuntimeError("webhook down")

    monkeypatch.setattr(notifications, "notify", _boom)

    bg = BackgroundTasks()
    out = await mod.ingest(
        "ws-1.s3cret",
        _Req({"title": "t", "body": "b", "severity": "info"}),
        bg, _Session(),
    )
    assert out == {"ok": True, "created": 1}
    for task in bg.tasks:
        await task()          # must not raise


@pytest.mark.asyncio
async def test_nothing_is_dispatched_when_nothing_parsed(monkeypatch):
    """An empty payload must not schedule an empty send."""
    sent: list[dict] = []
    out = await _ingest(monkeypatch, {"alerts": []}, sent)
    assert out == {"ok": True, "created": 0}
    assert sent == []
