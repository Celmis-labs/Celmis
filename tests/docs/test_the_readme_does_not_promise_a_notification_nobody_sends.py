"""The README said an alert puts a web push on your phone. Nothing does.

Web push in this product is real — VAPID keys, a service worker, a
subscription table — and exactly two things send one: `src/agent/runner.py`
when an agent turn finishes, and the test-send endpoint in
`src/api/routers/push.py`. The alert path sends none. `grep -c webpush` in
`src/api/routers/alerts.py` and `src/notifications/dispatch.py` returns zero;
what an alert actually does is fan out to the workspace's bound channels —
Slack, Discord, Google Chat, a plain webhook.

So the sentence describing the feature people would switch the product on for
described something that had never been built. Keyed on the property rather
than on the sentence: if the README says an alert pushes, the alert path has
to push.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

#: Everything an incoming alert passes through on its way out.
ALERT_PATH = (
    ROOT / "src" / "api" / "routers" / "alerts.py",
    ROOT / "src" / "notifications" / "dispatch.py",
)

#: "push" alone is `git push`; these are the phrases that promise the browser
#: notification.
PUSH_CLAIM = re.compile(r"web push|webpush|vapid|service worker", re.I)


def _alert_path_sends_a_push() -> bool:
    return any("webpush" in f.read_text() for f in ALERT_PATH)


def _alert_section() -> str:
    """The README from the alerts heading to the next one."""
    text = README.read_text()
    match = re.search(r"^## Alerts,.*?(?=^## )", text, re.M | re.S)
    assert match, "the alerts section is gone; this test is looking at nothing"
    return match.group(0)


def test_the_alert_section_does_not_promise_a_push() -> None:
    section = _alert_section()
    claims = [line for line in section.splitlines() if PUSH_CLAIM.search(line)]
    if claims and not _alert_path_sends_a_push():
        raise AssertionError(
            "the alerts section promises a web push:\n  "
            + "\n  ".join(c.strip() for c in claims)
            + "\nbut neither alerts.py nor dispatch.py mentions webpush. Either "
              "build it or describe what an alert really does — fan out to the "
              "workspace's bound channels."
        )


def test_the_summary_table_agrees_with_the_section() -> None:
    """The row a reader meets first, before any section."""
    text = README.read_text()
    rows = [line for line in text.splitlines()
            if line.startswith("|") and "alert fires" in line.lower()]
    assert rows, "the alert row left the table; this test is looking at nothing"
    for row in rows:
        if PUSH_CLAIM.search(row) and not _alert_path_sends_a_push():
            raise AssertionError(f"the table promises a push no code sends:\n  {row.strip()}")


def test_web_push_itself_still_exists() -> None:
    """Not a claim that the feature is fictional — only that alerts do not use it.

    If this fails, the two tests above stopped being about a mismatch and
    started being about a deletion.
    """
    runner = (ROOT / "src" / "agent" / "runner.py").read_text()
    assert "webpush" in runner, (
        "nothing sends a web push any more; the README wording above should "
        "say so too"
    )
