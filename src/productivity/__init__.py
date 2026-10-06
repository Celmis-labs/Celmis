"""Productivity: pull request history, deployments and where the sync stands.

The tables, the sync engine, the scheduler and the webhook hook are part of the
AGPL core and stay quiet until a repository is switched on
(`ProductivitySettings.enabled`): an install that never opts in spends no API
calls. The metrics maths, the API and the page that read these tables are
Enterprise (`src/ee/analytics`).

Layout:
    settings    repo > workspace > built-in resolution of the settings
    classify    pure: who is a bot, what kind of PR is it, which PR it reverts
    ratelimit   one token bucket per credential, Retry-After honoured
    providers/  Bitbucket, GitHub and GitLab behind one adapter interface
    deploys     pure: deployments from merges, PR-to-deploy link, failures
    sync        the engine: list, detail, deployments, link, classify
    scheduler   hourly tick that queues one job per enabled repository
"""
