"""Notification channels (Stage 15) — Grafana-style contact points.

Public API:

    from src.notifications import notify

    notify(
        workspace_id=ws_id,          # required — the delivery boundary
        event="breaking_change",
        repo_slug="owner/repo",
        title="Signature change on get_user",
        body_md="**3 consumers** across `svc-a`, `svc-b`.",
        severity="error",
        link_url="https://…/pr/42",
    )

Behaviour:
    * Looks up every enabled ChannelBinding matching (repo_slug, event) OR
      (NULL, event) OR (repo_slug, '*') OR (NULL, '*').
    * Filters by min_severity.
    * Sends via the per-kind adapter (slack blocks / discord embed /
      google chat card / raw JSON POST).
    * All errors are logged, never raised — a broken channel must NOT
      break the review pipeline.
"""

from src.notifications.dispatch import notify

__all__ = ["notify"]
