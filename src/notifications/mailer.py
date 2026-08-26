"""Optional outbound email (stdlib smtplib — no extra dependency).

Configured entirely from Settings (SMTP_HOST etc. in .env). When SMTP_HOST is
unset every flow silently keeps its mailer-less behaviour, so a deployment
without email still works exactly as before: reset links are handed over by an
admin, invite links are copy-pasted.

Delivery is best-effort by design: callers treat a failed send the same as "no
mailer configured" and fall back to the manual channel. Nothing here raises to
the caller.
"""

from __future__ import annotations

import logging
import smtplib
import threading
from email.message import EmailMessage

from src.config import get_settings

logger = logging.getLogger(__name__)


def mailer_configured() -> bool:
    """True when outbound email can be attempted (smtp_host is set)."""
    s = get_settings()
    return bool(s.smtp_host)


def absolute_url(relative: str) -> str:
    """Absolutize an app-relative link (e.g. "/invite/abc") for use in an
    email body. Falls back to the bare relative path when public_base_url is
    unset — still usable, just needs the recipient to know the host."""
    s = get_settings()
    base = (s.public_base_url or "").rstrip("/")
    return f"{base}{relative}" if base else relative


def send_email(to: str, subject: str, body: str) -> bool:
    """Send a plain-text email. Returns True on success, False otherwise.

    Never raises — email is an optional convenience channel here, and every
    caller has a manual fallback path.
    """
    s = get_settings()
    if not s.smtp_host:
        return False

    msg = EmailMessage()
    msg["From"] = s.smtp_from or (s.smtp_username or "celmis@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if s.smtp_starttls:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if s.smtp_username and s.smtp_password:
                    smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                if s.smtp_username and s.smtp_password:
                    smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
                smtp.send_message(msg)
        logger.info("email_sent to=%s subject=%r", to, subject)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort channel
        logger.warning("email_send_failed to=%s err=%s", to, exc)
        return False


def send_email_background(to: str, subject: str, body: str) -> None:
    """Fire-and-forget variant so an HTTP handler never blocks on SMTP."""
    if not mailer_configured():
        return
    threading.Thread(
        target=send_email, args=(to, subject, body), daemon=True,
    ).start()


__all__ = [
    "mailer_configured",
    "absolute_url",
    "send_email",
    "send_email_background",
]
