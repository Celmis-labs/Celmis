"""Channel dispatch — routing + adapter selection."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"info": 1, "warn": 2, "warning": 2, "error": 3, "critical": 4}


def notify(
    *,
    workspace_id: str,
    event: str,
    repo_slug: str | None,
    title: str,
    body_md: str,
    severity: str = "info",
    link_url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    """Fan-out a notification to every matching channel IN THIS WORKSPACE.
    Returns the number of successful deliveries.

    `workspace_id` is required and has no default. The binding query used to
    filter on enabled/event/repo_slug and nothing else, so a workspace-wide
    binding (repo_slug NULL) matched every tenant's events: one workspace's PR
    title, finding counts and review summary were delivered into another
    tenant's Slack room. A default value here would be a way to reintroduce
    that by forgetting an argument.

    Non-raising by design: any transport / adapter failure is logged
    but never bubbles up (the caller is a review agent — it must never
    fail because of a broken Slack webhook).
    """
    try:
        bindings = _matching_bindings(
            workspace_id=workspace_id, repo_slug=repo_slug,
            event=event, severity=severity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("notif_bindings_lookup_failed err=%s", exc)
        return 0

    delivered = 0
    for chan in bindings:
        try:
            _send(chan, title=title, body_md=body_md, severity=severity, link_url=link_url, extra=extra)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("notif_send_failed channel=%s kind=%s err=%s",
                           chan.get("name"), chan.get("kind"), exc)
    if delivered:
        logger.info("notif_delivered event=%s repo=%s severity=%s delivered=%d",
                    event, repo_slug, severity, delivered)
    return delivered


def public_link(path: str) -> str | None:
    """An absolute URL for a page of this installation, or nothing at all.

    A RELATIVE URL IS NOT A SMALLER VERSION OF AN ABSOLUTE ONE — it is a card
    that never arrives. Google Chat validates `openLink.url`, and a value like
    `/claude/78dcfe6c` fails that validation, so the whole card is dropped:
    what lands in the room is the bot's name with an empty body. Seen on a
    real phone, between a firing alert and its recovery — a hole in the feed
    where a notification should have been, delivered=1 in our own log.

    So the rule is absolute-or-nothing, the same one the alerts path already
    follows. A notification without a button still carries its message; a
    notification Google Chat refuses to render carries nothing, and reports
    success while doing it.

    Nothing is guessed from a request: the address comes from configuration,
    which is the only party in the exchange that is not the sender.
    """
    from src.config import get_settings

    base = (get_settings().public_base_url or "").strip().rstrip("/")
    if not base:
        logger.warning(
            "notify_link_unset — set PUBLIC_BASE_URL to put an Open button on "
            "notifications; sending them without one")
        return None
    if not base.startswith(("http://", "https://")):
        logger.warning(
            "notify_link_no_scheme PUBLIC_BASE_URL=%r — needs http:// or "
            "https://; sending notifications without a link", base)
        return None
    return f"{base}/{path.lstrip('/')}"


def _matching_bindings(
    *, workspace_id: str, repo_slug: str | None, event: str, severity: str,
) -> list[dict[str, Any]]:
    """Return channel dicts (already joined with channel row) that match.

    A binding carries no workspace of its own — tenancy lives on the channel it
    points at, so the join is the boundary, exactly as the /bindings listing
    does it.
    """
    from sqlalchemy import create_engine, or_, select
    from sqlalchemy.orm import Session

    from src.db.models import ChannelBinding, NotificationChannel
    from src.db.session import get_database_url

    url = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    sev_rank = _SEVERITY_RANK.get(severity.lower(), 1)
    try:
        with Session(engine) as s:
            stmt = (
                select(ChannelBinding, NotificationChannel)
                .join(NotificationChannel,
                      NotificationChannel.id == ChannelBinding.channel_id)
                .where(NotificationChannel.workspace_id == workspace_id)
                .where(ChannelBinding.enabled.is_(True))
                .where(NotificationChannel.enabled.is_(True))
                .where(or_(ChannelBinding.event == event,
                           ChannelBinding.event == "*"))
                .where(or_(ChannelBinding.repo_slug.is_(None),
                           ChannelBinding.repo_slug == repo_slug))
            )
            out: list[dict[str, Any]] = []
            for binding, chan in s.execute(stmt).all():
                need = _SEVERITY_RANK.get(binding.min_severity.lower(), 1)
                if sev_rank < need:
                    continue
                out.append({
                    "name": chan.name,
                    "kind": chan.kind,
                    "webhook_url": chan.webhook_url,
                    "config": dict(chan.config or {}),
                })
            return out
    finally:
        engine.dispose()


def _send(
    chan: dict[str, Any],
    *,
    title: str,
    body_md: str,
    severity: str,
    link_url: str | None,
    extra: dict[str, Any] | None,
) -> None:
    kind = chan["kind"]
    if kind == "slack":
        _post_slack(chan, title=title, body_md=body_md, severity=severity, link_url=link_url)
    elif kind == "discord":
        _post_discord(chan, title=title, body_md=body_md, severity=severity, link_url=link_url)
    elif kind == "google_chat":
        _post_google_chat(chan, title=title, body_md=body_md, severity=severity, link_url=link_url)
    elif kind == "webhook":
        _post_generic(chan, title=title, body_md=body_md, severity=severity,
                      link_url=link_url, extra=extra)
    else:
        raise ValueError(f"unknown channel kind: {kind!r}")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    with httpx.Client(timeout=10.0) as http:
        r = http.post(url, json=payload, headers=headers or {})
        r.raise_for_status()


# ─── Adapters ────────────────────────────────────────────────────────


def _sev_emoji(sev: str) -> str:
    return {"error": "🔴", "critical": "🔴", "warn": "🟡", "warning": "🟡"}.get(sev.lower(), "🔵")


def _post_slack(chan, *, title, body_md, severity, link_url):
    payload: dict[str, Any] = {
        "text": f"{_sev_emoji(severity)} {title}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
                                        "text": f"{_sev_emoji(severity)} {title}"[:150]}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body_md[:3000]}},
        ],
    }
    if link_url:
        payload["blocks"].append({
            "type": "actions",
            "elements": [{"type": "button",
                          "text": {"type": "plain_text", "text": "Open"},
                          "url": link_url}],
        })
    _post_json(chan["webhook_url"], payload)


def _post_discord(chan, *, title, body_md, severity, link_url):
    color = {"error": 0xE53935, "critical": 0xB71C1C,
             "warn": 0xFB8C00, "warning": 0xFB8C00}.get(severity.lower(), 0x1E88E5)
    embed = {
        "title": title[:256],
        "description": body_md[:4000],
        "color": color,
    }
    if link_url:
        embed["url"] = link_url
    _post_json(chan["webhook_url"], {"embeds": [embed]})


def _md_to_chat(text: str) -> str:
    """Markdown into what Google Chat actually renders.

    `body_md` IS MARKDOWN, and every other adapter here treats it as such:
    Slack takes mrkdwn, Discord takes markdown. Google Chat takes neither — a
    `textParagraph` renders a small HTML subset (`<b>`, `<i>`, `<code>`, `<a>`)
    and prints anything else verbatim. So the review card that says

        **0** critical · **4** error · **0** warn · **2** info

    arrived in the room with the asterisks in it, on a phone, in front of the
    person the notification exists to inform. Seen on a real screenshot; the
    numbers were right and the punctuation was shouting.

    Deliberately small. Bold, italic, inline code and links are what these
    bodies use; a full markdown renderer here would be a second parser to keep
    in step with the one nobody wrote. Anything not converted is escaped, so
    text a sender controls cannot inject markup into the card — the same rule
    the alert bodies follow.
    """
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                 r'<a href="\2">\1</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>",
                 out, flags=re.S)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def _post_google_chat(chan, *, title, body_md, severity, link_url):
    # Google Chat webhook accepts either simple `text` OR cardsV2. Use
    # a card so the header + button render properly.
    card: dict[str, Any] = {
        "cardsV2": [{
            "cardId": "celmis",
            "card": {
                "header": {"title": f"{_sev_emoji(severity)} {title}"[:200]},
                "sections": [{"widgets": [
                    {"textParagraph": {"text": _md_to_chat(body_md)[:4000]}},
                ]}],
            },
        }],
    }
    if link_url:
        widgets = card["cardsV2"][0]["card"]["sections"][0]["widgets"]
        widgets.append({
            "buttonList": {"buttons": [{
                "text": "Open",
                "onClick": {"openLink": {"url": link_url}},
            }]},
        })
        # AND as text, because the button is not always allowed to work. Google
        # Chat will not open a plain-http link to a bare IP address: the button
        # renders inert, and following it reports the site as unavailable — the
        # address was right the whole time. That is the address of every
        # installation reached by IP rather than by hostname, which this product
        # supports on purpose and which the alert must not become useless on.
        # No link policy can disable a string, so the address is copyable even
        # when the button refuses to move.
        widgets.append({"textParagraph": {"text": link_url}})
    _post_json(chan["webhook_url"], card)


def _post_generic(chan, *, title, body_md, severity, link_url, extra):
    headers = dict((chan.get("config") or {}).get("headers") or {})
    payload = {
        "title": title,
        "body_md": body_md,
        "severity": severity,
        "link_url": link_url,
        **(extra or {}),
    }
    _post_json(chan["webhook_url"], payload, headers=headers)
