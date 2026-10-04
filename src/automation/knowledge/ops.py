"""Monitoring, notifications, system pages, MCP.

Written from web/app/(app)/{alerts,admin/notifications,admin/jobs,admin/audit,
admin/logs,admin/health,admin/gdpr,settings/mcp,admin/oauth-clients} and the
routers behind them.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="alerts-notifications",
        title="Incoming alerts and notification channels (Slack, Discord, ...)",
        keywords=(
            "alert", "grafana", "alertmanager", "monitoring", "incident",
            "notification", "notify", "slack", "discord", "google chat",
            "telegram", "email", "channel", "binding",
            "алерт", "сповіщен", "моніторинг", "інцидент", "канал", "слак",
            "оповещен", "уведомлен", "мониторинг", "инцидент",
        ),
        strong=("alert", "алерт", "notification", "slack", "сповіщен",
                "уведомлен", "grafana"),
        body="""
Incoming alerts — [Incoming alerts](/alerts):
1. In "Ingest webhook" press "Create ingest URL" (owner or admin); "Rotate"
   replaces it. The URL is `POST /webhook/alerts/<workspace_id>.<secret>`
   (behind the bundled Caddy prefix it with `https://<host>/backend`).
2. Grafana: add a webhook contact point with that URL (its payload is parsed
   as-is). Anything else posts JSON with fields `title`, `body`, `severity`,
   `repo` (title required; severity info, warning, error or critical; a `repo`
   label ties the alert to a repository).
3. Alerts appear in the list with "Fix from here" (opens a Claude Code
   session holding the alert), "Acknowledge" and "Mark fixed".

Notification channels — [Notification channels](/admin/notifications) (owner
or admin): Slack, Discord, Google Chat or a generic webhook (no Telegram or
email channel).
1. "New channel": "Name", "Kind", "Webhook URL" → "Add"; "Test" sends a test
   message.
2. "New binding": "Channel", "Event" (any, review_complete, breaking_change,
   agent_turn_done, alert_received), "Repo slug (blank = workspace-wide)",
   "Min severity". A channel without a binding never delivers anything.
Personal browser push ("Notify me when work finishes") is on
[Account](/settings).
""",
    ),
    Section(
        id="system-pages",
        title="Job queue, audit log, server logs, system status, GDPR",
        keywords=(
            "job", "queue", "stuck", "failed", "dead", "retry", "audit log",
            "log", "logs", "health", "status", "system status", "gdpr",
            "erase", "export", "cpu", "memory",
            "черг", "завдан", "задач", "завис", "журнал", "лог", "стан",
            "здоров", "статус",
            "очеред", "задан", "завис", "журнал", "состоян",
        ),
        strong=("queue", "черг", "очеред", "job", "audit log", "gdpr",
                "system status"),
        body="""
- [Job queue](/admin/jobs) — the durable queue behind indexing, reviews,
  vaults, audits; refreshes every 5 s. Statuses pending, running, completed,
  failed, dead (max attempts exceeded). "Retry", "Stop" and delete need owner
  or admin. Whether your indexing is running is answered here.
- [Audit log](/admin/audit) — every LLM call: operation, mode, model,
  repository, tokens, duration (no prompts or answers stored). Owner or
  admin.
- [Server logs](/admin/logs) — live tail of the API log plus a state
  snapshot; global admin only.
- [System status](/admin/health) — global admin: live state of git and LLM
  providers, MCP sources, channels, the queue and the vector store; "Resource history" (CPU, memory, reviews in parallel; CSV export); the "Edition"
  card with the licence.
- [GDPR](/admin/gdpr) — global admin: pick a user, "Export JSON" or "Erase"
  (anonymise, irreversible).
- [OAuth clients](/admin/oauth-clients) — register MCP/OAuth clients
  ("Name", "Client type", "Redirect URIs (comma-separated)", "Allowed scopes (comma-separated)", "Register"); a confidential client's secret is shown
  once.
""",
    ),
    Section(
        id="mcp",
        title="MCP: connecting Claude Code, Cursor and other editors",
        keywords=(
            "mcp", "cursor", "zed", "editor", "ide", "claude code", "tool",
            "bearer", "token", "integration", "connect editor",
            "редактор", "інтеграц", "интеграц",
        ),
        strong=("mcp", "cursor"),
        body="""
Celmis serves its index (symbols, callers, cross-repository consumers, API
surfaces, owners, reviews, audits) to editors over MCP at `/mcp/`.

[MCP](/settings/mcp) (Settings → "MCP"), any signed-in user:
1. "Generate a token" — read-only scopes (`read:graph`, `read:groups`,
   `read:reviews`), valid 30 days, issued as you — your research rules on
   Code access apply to every call — and shown once. Generating another
   does not revoke the first; a token cannot be revoked, it expires. The
   server answers from the workspace where you hold your highest role
   (usually your personal one), not the one active in the browser.
2. Claude Code: `claude mcp add --transport http celmis
   https://<host>/backend/mcp/ --header 'Authorization: Bearer <token>'`
   (the page shows the exact URL; with a separate API domain there is no
   `/backend`). The trailing slash matters.
3. Cursor and others: the same URL and header in `.cursor/mcp.json` (or
   `.mcp.json` for a Claude Code project).
4. Check that the tools are listed. Not connecting: missing trailing slash,
   expired token, 403 because you are no longer a member of the workspace
   the token was issued for (generate a new one), or 503 MCP is not
   configured (the operator must set `MCP_JWT_SECRET`).
Write tools (`add_repo`, `start_dep_audit`, `generate_docs`,
`set_auto_review`, `migrate_consumers`) need write scopes, issued by the
operator with `analyzer mcp issue-token --scopes ...` or through an OAuth
client on [OAuth clients](/admin/oauth-clients).
""",
    ),
)
