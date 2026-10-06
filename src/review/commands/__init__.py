"""Commands a person gives the reviewer in a pull-request comment.

* `parser`   — what a comment asks for (pure text).
* `events`   — the three providers' comment webhooks as one `CommentEvent`.
* `gate`     — who may command the bot, and the loop and rate guards.
* `ledger`   — one row per accepted comment: idempotency, rate limit, timeline.
* `handlers` — the command registry and the built-in commands.

The receiver is `src.review.webhook._dispatch_command`; the work runs as a
`pr_command` job on the sync queue (`src.sync.handlers.handle_pr_command`).
"""
