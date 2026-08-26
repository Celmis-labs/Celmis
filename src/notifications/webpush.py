"""Web Push — telling someone their agent finished while the app is closed.

This is the only notification channel that works for the way the product is
actually used: start a session from a phone, lock the phone, walk away. A tab
cannot survive that — iOS suspends backgrounded pages and eventually discards
them, so no amount of stream-reconnect logic gets a result in front of anyone
who has put the phone in their pocket. A push subscription is held by the
browser's push service, not by the page.

OFF unless VAPID keys are configured; every call becomes a no-op, so a
deployment that never sets them behaves exactly as before.

Two hard requirements the caller cannot work around:
  * a secure context — service workers and the Push API are HTTPS-only
    (localhost excepted), so on a plain-HTTP deployment subscriptions will
    never be created in the first place;
  * on iOS, the site must be installed to the Home Screen. Safari refuses
    Notification.requestPermission() to a browser tab.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)

# The push services reject anything larger; the payload is a short notification,
# not a transport.
_MAX_PAYLOAD_BYTES = 3800
_TTL_SECONDS = 12 * 60 * 60


def vapid_public_key() -> str:
    return (os.environ.get("VAPID_PUBLIC_KEY") or "").strip()


def _private_key() -> str:
    return (os.environ.get("VAPID_PRIVATE_KEY") or "").strip()


def _subject() -> str:
    """`mailto:` or `https:` identifying the sender — required by RFC 8292.

    Push services reject a token without it, and some of them (Apple in
    particular) reject one whose scheme they do not recognise.
    """
    subject = (os.environ.get("VAPID_SUBJECT") or "").strip()
    if subject and not subject.startswith(("mailto:", "http://", "https://")):
        subject = f"mailto:{subject}"
    return subject or "mailto:ops@celmis.local"


def is_enabled() -> bool:
    return bool(vapid_public_key() and _private_key())


def status() -> dict[str, Any]:
    """Secret-free summary for the settings UI and /api/ops/diag."""
    return {
        "enabled": is_enabled(),
        "public_key": vapid_public_key(),
        "subject_set": bool((os.environ.get("VAPID_SUBJECT") or "").strip()),
    }


def _payload(title: str, body: str, url: str, tag: str) -> str:
    text = json.dumps(
        {"title": title[:120], "body": body[:400], "url": url, "tag": tag},
        ensure_ascii=False,
    )
    if len(text.encode()) > _MAX_PAYLOAD_BYTES:  # pragma: no cover — defensive
        text = json.dumps({"title": title[:120], "body": "", "url": url, "tag": tag})
    return text


def send_to_user(user_id: str, *, title: str, body: str, url: str,
                 tag: str = "celmis") -> dict[str, int]:
    """Deliver to every device this user registered. Never raises.

    Returns ``{sent, expired, failed}``. Blocking (pywebpush is sync) — call it
    from a thread, not from the event loop.

    A push service answering 404/410 means the subscription is dead for good:
    the row is deleted rather than retried, because a stale endpoint otherwise
    fails on every future notification forever.
    """
    counts = {"sent": 0, "expired": 0, "failed": 0}
    if not is_enabled():
        return counts

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:  # pragma: no cover — depends on the install
        logger.warning("webpush_library_missing")
        return counts

    from sqlalchemy import create_engine, delete, select, update
    from sqlalchemy.orm import Session

    from src.db.models import PushSubscription
    from src.db.session import get_database_url

    engine = create_engine(
        get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://"),
        pool_pre_ping=True,
    )
    data = _payload(title, body, url, tag)
    dead: list[str] = []
    try:
        with Session(engine) as session:
            subs = list(session.scalars(
                select(PushSubscription).where(PushSubscription.user_id == user_id)
            ).all())
            for sub in subs:
                try:
                    webpush(
                        subscription_info={
                            "endpoint": sub.endpoint,
                            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                        },
                        data=data,
                        vapid_private_key=_private_key(),
                        vapid_claims={"sub": _subject()},
                        ttl=_TTL_SECONDS,
                    )
                    counts["sent"] += 1
                except WebPushException as exc:
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    if code in (404, 410):
                        dead.append(sub.id)
                        counts["expired"] += 1
                    else:
                        counts["failed"] += 1
                        # The endpoint contains a device-identifying token —
                        # log the status, never the URL.
                        logger.warning("webpush_failed user=%s status=%s", user_id, code)
                except Exception as exc:  # noqa: BLE001
                    counts["failed"] += 1
                    logger.warning("webpush_error user=%s err=%s", user_id, str(exc)[:200])

            if dead:
                session.execute(
                    delete(PushSubscription).where(PushSubscription.id.in_(dead))
                )
            if counts["sent"]:
                from datetime import datetime
                session.execute(
                    update(PushSubscription)
                    .where(PushSubscription.user_id == user_id,
                           PushSubscription.id.notin_(dead or [""]))
                    .values(last_sent_at=datetime.now(UTC))
                )
            session.commit()
    except Exception as exc:  # noqa: BLE001 — a notification must never break a run
        logger.warning("webpush_dispatch_failed user=%s err=%s", user_id, str(exc)[:200])
    finally:
        engine.dispose()

    logger.info("webpush_sent user=%s sent=%d expired=%d failed=%d",
                user_id, counts["sent"], counts["expired"], counts["failed"])
    return counts


__all__ = ["is_enabled", "status", "vapid_public_key", "send_to_user"]
