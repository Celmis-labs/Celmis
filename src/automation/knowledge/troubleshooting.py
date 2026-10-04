"""When something does not work: the symptom, the cause, the fix.

Each entry is a message or a symptom people actually report, with the error
text as the code writes it (src/llm/{gateway,completion,errors,budget,
litellm_proxy}.py, src/review/webhook.py, src/api/middleware.py) and the page
that fixes it.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="troubleshooting",
        title="Troubleshooting: common problems and fixes",
        keywords=(
            "error", "fail", "failed", "broken", "not working", "doesn't",
            "does not", "nothing", "empty", "why", "401", "403", "404",
            "409", "429", "500", "503", "mismatch", "dimension", "timeout",
            "rate limit", "stuck", "no comments", "not posted", "no issues",
            "помилк", "не прац", "не вийшло", "чому", "порожн", "нічого",
            "зависл", "не з'явля", "не публік",
            "ошибк", "не работ", "почему", "пуст", "ничего", "завис",
            "не появля",
        ),
        strong=("error", "помилк", "ошибк", "mismatch", "not working",
                "не прац", "не работ", "401", "403", "troubleshoot"),
        body="""
- No issues / pull requests shown: nothing has been reviewed yet. Issues and
  [Pull requests](/pull-requests) fill only after a review. Press "Install webhook" on the repository's row on [Repositories](/repositories) (owner or
  admin), or switch it on in "Auto-review PRs" on [Review history](/reviews)
  and set up the webhook by hand, or run one with "Run a review".
- "webhook failed" on a repository: the token may not manage webhooks
  (GitHub `admin:repo_hook` or fine-grained Webhooks Read and write; GitLab
  Maintainer; Bitbucket `write:webhook:bitbucket`; the account must administer
  the repository), or the server's `PUBLIC_BASE_URL` is unset or points at
  localhost. Fix it, then "Repair webhook"; or use "manual setup".
- Auto-review does nothing: the repository's switch is off; the webhook is
  missing or has the wrong secret (check the provider's delivery log: 401/403
  = secret, 500 = no secret generated); the workspace id in the URL is
  another workspace's; GitHub polling with a fine-grained token (needs a
  classic token with `notifications`, or use a webhook); Bitbucket is never
  polled; the base branch is not among the policy's "Target branches"; the PR
  is a draft (and `run_on_drafts` is off); review is off in the policy.
- Review ran but posted no comments: "Post comments" was off for a manual
  run; findings were below the policy's "Post comments for" level (still in
  the summary and on Issues); the token cannot write (GitHub Pull requests
  write; GitLab `api`, not `read_api`). "Apply fix" 403 = GitHub Contents is
  read-only.
- A review is SKIPPED or degraded: the diff is over 500 KB, every file is in
  ignore paths, all agents are disabled, or a critical agent failed (the
  summary names it). `local_timeout` = the installation's own deadline;
  the operator raises `REVIEW_LLM_TIMEOUT_SECONDS`.
- no API key configured for provider / the provider rejected the API key:
  save and "Test" the key on [LLM Setup](/settings/llm) in THIS workspace
  (keys are per workspace). Model does not exist at the provider: pick one the
  "Test" lists. Quota exhausted or rate-limited: the provider's limit — wait,
  or set a "Fallback review model".
- the workspace spend budget is exhausted: raise "Monthly cap (USD)" or turn
  off the hard stop on [Usage & cost](/admin/usage) (owner/admin).
- LiteLLM proxy 401/403 (the LiteLLM proxy rejected the virtual key): the
  virtual key is wrong, expired or not allowed for that model — enter a valid
  one in "Virtual key" and "Verify and save". Lists no models: the key has no
  models assigned on the proxy. Must use https / resolves to a non-public
  address: use the public https URL (or the operator allow-lists the host in
  `LITELLM_PROXY_ALLOWED_HOSTS`). Model not offered by the proxy: choose one
  from its list.
- Embeddings profile is X but the vector collection was built with Y (width
  N) / dimension mismatch: the embeddings model or "Dimensions" changed after
  indexing (embeddings are installation-wide). A global admin presses "Reindex everything" on
  [LLM Setup](/settings/llm) (the error calls it Re-index embeddings), or
  switches the profile back. Self-hosted: set `EMBEDDING_DIMENSIONS` to what
  the model returns (768 for nomic-embed-text, 1024 for bge-m3) and reindex.
- Q&A cites nothing / semantic search unavailable: the repository is not
  indexed or has no vault — "index now", then "Generate vault" on
  [Repositories](/repositories).
- Indexing seems stuck: see [Job queue](/admin/jobs) (failed/dead jobs can be
  retried by owner/admin).
- Git token rejected on [Git connections](/connections): GitHub/GitLab 401 =
  wrong or expired token; Bitbucket 401 = wrong Atlassian email or token, 403
  = no access to the workspace, 404 = wrong "Workspace slug". Self-hosted
  GitLab URLs are not supported.
- 403 Requires owner/admin on this workspace: the page needs owner or admin of
  the ACTIVE workspace — check the switcher, or ask that workspace's admin.
  Read-only prompt/policy pages: you need editor, admin or owner.
- 429 rate limit exceeded: too many requests per minute from one address;
  wait the Retry-After seconds.
- Analytics says "Available in Celmis Enterprise": no licence with analytics
  (see SSO and licence); "Analytics is for workspace leads": you need
  editor, admin or owner.
- An invite link fails: expired, revoked, used, or bound to another email —
  ask for a new one; or send an [access request](/access-request).
""",
    ),
)
