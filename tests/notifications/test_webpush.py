"""Push has to fail quietly and clean up after itself.

Two properties matter more than delivery, because both are silent when wrong:
a notification failure must never touch the outcome of the run that triggered
it, and a subscription the push service reports as gone must be deleted rather
than retried forever.
"""

from __future__ import annotations

import json

import pytest

from src.notifications import webpush


@pytest.fixture(autouse=True)
def _no_vapid(monkeypatch):
    for var in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(var, raising=False)


def _configure(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPublicKeyBase64Url")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "cHJpdmF0ZQ")


# ─── the off switch ──────────────────────────────────────────────────


def test_disabled_without_keys():
    assert webpush.is_enabled() is False
    assert webpush.status()["enabled"] is False


def test_send_is_a_noop_when_disabled():
    """No keys must mean no database work and no exception."""
    assert webpush.send_to_user("u1", title="t", body="b", url="/x") == {
        "sent": 0, "expired": 0, "failed": 0,
    }


def test_enabled_needs_both_halves(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    assert webpush.is_enabled() is False      # public key alone is useless
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    assert webpush.is_enabled() is True


def test_status_never_exposes_the_private_key(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "super-secret-value")
    assert "super-secret-value" not in json.dumps(webpush.status())


# ─── the subject ─────────────────────────────────────────────────────


def test_bare_address_becomes_mailto(monkeypatch):
    monkeypatch.setenv("VAPID_SUBJECT", "ops@example.com")
    assert webpush._subject() == "mailto:ops@example.com"


def test_url_subject_is_left_alone(monkeypatch):
    monkeypatch.setenv("VAPID_SUBJECT", "https://celmis.example")
    assert webpush._subject() == "https://celmis.example"


def test_subject_has_a_scheme_even_when_unset():
    # Push services reject a VAPID token whose `sub` has no scheme.
    assert webpush._subject().startswith(("mailto:", "http"))


# ─── payload ─────────────────────────────────────────────────────────


def test_payload_carries_what_the_service_worker_reads():
    data = json.loads(webpush._payload("Finished", "repo · pushed", "/claude/1", "s-1"))
    assert data == {"title": "Finished", "body": "repo · pushed",
                    "url": "/claude/1", "tag": "s-1"}


def test_payload_stays_under_the_push_size_limit():
    raw = webpush._payload("t" * 500, "b" * 9000, "/claude/1", "tag")
    assert len(raw.encode()) <= 3800
    # Truncation must not produce something the worker cannot parse.
    assert json.loads(raw)["url"] == "/claude/1"


def test_payload_keeps_non_ascii_readable():
    data = json.loads(webpush._payload("Готово", "гілку запушено", "/claude/1", "t"))
    assert data["title"] == "Готово"
