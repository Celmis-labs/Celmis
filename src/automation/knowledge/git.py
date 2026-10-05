"""Git providers, webhooks, repositories and indexing.

Written from web/app/(app)/connections, src/api/routers/connections.py,
src/review/webhook.py, src/api/routers/webhooks.py (the URL it builds),
deploy/Caddyfile (`/backend/*` is the API), src/review/poller.py and
web/app/(app)/repositories.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="git-connections",
        title="Connecting GitHub, GitLab or Bitbucket (tokens and scopes)",
        keywords=(
            "github", "gitlab", "bitbucket", "token", "scope", "connect",
            "connection", "provider", "pat", "credential", "atlassian",
            "glpat", "permission", "git",
            "токен", "скоуп", "права", "дозвол", "підключ", "з'єднан",
            "провайдер", "гітхаб", "гітлаб", "битбакет", "бітбакет",
            "подключ", "соединен", "разрешен", "гитхаб", "гитлаб",
        ),
        strong=("token", "токен", "scope", "скоуп", "glpat", "atlassian"),
        body="""
Where: [Git connections](/connections) (Settings → "Git connections"). One
token per provider per workspace, encrypted with a workspace-scoped key and
used to clone, list repositories and post review comments. Saving, replacing,
verifying and removing a token needs owner or admin of the workspace (or a
global admin); anyone else gets 403 Requires owner/admin on this workspace. A self-hosted GitLab is supported: fill "GitLab URL" on the
GitLab card (see the self-hosted GitLab section). GitHub Enterprise Server
is not: GitHub verification and polling talk to github.com.

Steps (every card works the same way):
1. Open [Git connections](/connections). Each card (GitHub, GitLab,
   Bitbucket) has "Show setup instructions" and "Open token page" (the
   provider's token screen).
2. Create the token at the provider with the scopes below. Prefer a machine
   account to your own: a personal token reaches everything you can see.
3. Paste it into "Token" (Bitbucket also needs "Atlassian email" and
   "Workspace slug") and press "Save & verify". It is saved only if the
   provider accepts it; the card then says "Connected" and shows the account it is saved as.
4. Later: "Verify" re-checks it, "Replace token" swaps it, "Remove"
   disconnects.

GitHub — fine-grained token (Settings → Developer settings → Fine-grained
tokens → Generate new token): Repository access all or the selected repos;
Permissions: Contents Read and write, Pull requests Read and write, Metadata
Read. Contents must be write, otherwise reviews work but Apply fix fails with
403. A classic token needs the whole `repo` scope (`public_repo` covers only
public repositories); automatic review by POLLING additionally needs the
classic `notifications` scope — fine-grained tokens cannot poll at all, use a
webhook instead.

GitLab — personal access token (avatar → Edit profile → Access tokens → Add
new token) with the `api` scope; `read_api` is not enough (comments cannot be
posted). gitlab.com requires an expiry date, at most 400 days. The value
starts with `glpat-` and is shown once.

Bitbucket — Atlassian API token (Atlassian account → Security → Create API
token), workspace-scoped recommended, permissions Repositories Admin/Write and
Pull requests Write. "Workspace slug" is the `<workspace>` in
bitbucket.org/<workspace>/; "Atlassian email" is the address you log in to
Bitbucket with. Errors: 401 = wrong email or token; 403 = the token cannot
read that workspace (`workspace:read` missing); 404 = wrong slug. Bitbucket
is never polled: review it by webhook or by hand.

For the one-press "Install webhook" on Repositories the token must also be
allowed to manage webhooks: GitHub classic `admin:repo_hook` (or `repo` with
admin rights), fine-grained Webhooks Read and write; GitLab Maintainer on the
project; Bitbucket `read:webhook:bitbucket` + `write:webhook:bitbucket`
(see the webhook-install section).
""",
    ),
    Section(
        id="self-hosted-gitlab",
        title="Self-hosted GitLab (own instance URL, sub-path, network, CA)",
        keywords=(
            "self-hosted", "self hosted", "selfhosted", "on-prem", "on prem",
            "own gitlab", "gitlab url", "instance", "enterprise", "internal",
            "private network", "vpn", "certificate", "ca bundle", "tls",
            "власн", "свій gitlab", "інстанс", "сертифікат", "внутрішн",
            "собственн", "свой gitlab", "инстанс", "внутренн",
        ),
        strong=("self-hosted", "self hosted", "gitlab url", "on-prem",
                "власний gitlab", "свой gitlab"),
        body="""
Where: [Git connections](/connections), GitLab card, field "GitLab URL".
Empty means gitlab.com. For your own instance enter its root address —
`https://gitlab.example.com`, or `https://example.com/gitlab` when GitLab
runs under a sub-path. A trailing `/api/v4` is accepted and removed; a
project link is refused. The URL is saved together with the token (you
always re-enter the token when you change it), and is used for EVERYTHING
GitLab in this workspace: verification (`GET /api/v4/user`), the repository
browser, cloning, merge-request lists, branches, review comments,
approvals, the MR description, reviewer assignment, apply-fix, polling and
the one-press webhook install. Other workspaces keep their own GitLab.

Token: a personal (or group/project) access token created ON YOUR INSTANCE
(avatar → Edit profile → Access tokens) with the `api` scope; `read_api` can
list but not comment or approve. Installing the webhook needs the
Maintainer role on the project.

Rules the URL must pass (checked on save and again before every call):
https only; no user:password@, query string or fragment; the host must
resolve to a public address. The server operator can relax two of these:
`GITLAB_ALLOWED_HOSTS` (JSON list) lets a listed host resolve to a private
address (never the 169.254.x cloud-metadata range), `GITLAB_HTTP_ALLOWED_HOSTS`
allows plain http:// for an exact host. A private certificate authority:
`GITLAB_CA_BUNDLE` = path to a PEM bundle on the server (added to the public
roots; TLS verification cannot be switched off).

Network: the Celmis SERVER connects to GitLab, not your browser. A GitLab
that is only reachable inside an office network or VPN (for example
gitlab.internal.example.com) cannot be reached from a cloud server unless a
route is arranged (VPN, peering, a reverse proxy). The save then fails with
an error saying the host does not resolve from the Celmis server, resolves
to a non-public address, could not be connected to, or presented a
certificate that is not trusted. For webhooks the
reverse also applies: your GitLab must reach this Celmis address.

Adding repositories: paste the project URL
(`https://gitlab.example.com/group/sub/project`, a merge-request URL also
works) on [Repositories](/repositories), or pick it in the browser. It is
stored as `gitlab:group/sub/project` and always resolved against the
workspace's own instance; a manual review takes `gitlab:group/project#7` or
the merge-request URL.
""",
    ),
    Section(
        id="webhooks",
        title="Automatic review: polling or webhook (URL, secret, events)",
        keywords=(
            "webhook", "hook", "auto-review", "auto review", "automatic",
            "polling", "poll", "payload", "secret", "event", "push",
            "merge request", "pull request event",
            "вебхук", "веб-хук", "хук", "автомат", "автоматичн", "опитуван",
            "секрет", "подій", "події", "подія",
            "автоматическ", "опрос", "событи",
        ),
        strong=("webhook", "вебхук", "веб-хук", "polling", "auto-review",
                "автоматичн", "автоматическ"),
        body="""
Quickest route: "Install webhook" on the repository's row on
[Repositories](/repositories) creates the webhook on the provider AND
switches auto-review on (owner or admin; see the webhook-install section).
What follows is the manual route and the switches themselves.

The per-repository switches are on [Review history](/reviews) (Code review →
"Review history"): the "Auto-review PRs" panel has one switch per registered
repository ("Automatic review for {repo}"). Under it is the "Connect auto-review" card with the webhook URL and secret. The chat agent can also
switch review on for a set of repositories (and pin their branch) — ask it.

Two ways a new pull request reaches Celmis once the switch is on:
- Polling (GitHub and GitLab, every ~60 s). GitHub polling reads the token's
  notification inbox, so it needs a CLASSIC token with `notifications`;
  fine-grained tokens get 403, and GitHub never notifies you about your own
  pull requests. GitLab polls the merge requests of the workspace's GitLab
  (gitlab.com or its self-hosted URL).
- Webhook (all three providers, recommended; the only automatic way for
  Bitbucket). Works whenever the repository's switch is on.

Setting up the webhook:
1. On [Review history](/reviews), in "Connect auto-review", press "Generate secret" for the provider (it says "configured" once one exists; "New secret" rotates it and the old one stops working at once). The secret is
   shown once — copy it immediately with "Copy the secret".
2. Copy the URL with "Copy the URL". It has the form
   `https://<your-host>/backend/webhook/<provider>/<workspace_id>` — e.g.
   `/backend/webhook/github/<workspace_id>`, `/backend/webhook/gitlab/...`,
   `/backend/webhook/bitbucket/...`. Behind the bundled Caddy the API lives
   under `/backend` (it is `/webhook/...`, not `/backend/api/...`); on a
   deployment with a separate API domain the same path is served without the
   `/backend` prefix.
3. In the repository at the provider: GitHub Settings → Webhooks → Add
   webhook (Payload URL, Content type `application/json`, Secret);
   GitLab Settings → Webhooks → Add new webhook (URL, Secret token);
   Bitbucket Repository settings → Webhooks → Add webhook (URL, Secret).
4. Events: GitHub `pull_request` (a `push` event also keeps the index fresh);
   GitLab Merge request events; Bitbucket `pullrequest:created` and
   `pullrequest:updated` (add `pullrequest:fulfilled` / `pullrequest:rejected`
   to record merges and declines).
5. Make sure the repository's switch in "Auto-review PRs" is on.

How a delivery is checked: GitHub `X-Hub-Signature-256` (HMAC-SHA256),
Bitbucket `X-Hub-Signature` (HMAC-SHA256), GitLab `X-Gitlab-Token` (the secret
itself). A delivery is silently skipped when the repository is not registered
in that workspace, auto-review is off for it, or the workspace id in the URL
is another workspace's. Without a secret the endpoint answers 500 Webhook
secret not configured. Drafts are skipped until ready for review; a new push
to an open pull request is reviewed again.
""",
    ),
    Section(
        id="webhook-install",
        title="Install webhook: automatic webhook setup per repository",
        keywords=(
            "webhook", "install", "repair", "hook", "public_base_url",
            "public url", "webhook failed", "no webhook", "auto-review",
            "вебхук", "веб-хук", "встанов", "інсталю", "полагод", "відремонт",
            "установ", "почин",
        ),
        strong=("install webhook", "repair webhook", "webhook", "вебхук",
                "веб-хук", "встанов", "установ"),
        body="""
Celmis can create the review webhook on the provider itself — no copying of
URLs and secrets:

1. Open [Repositories](/repositories). Each row shows a webhook badge
   ("webhook installed", "webhook failed" or "no webhook") and a button
   "Install webhook" ("Repair webhook" once installed).
2. Press "Install webhook". With the workspace's git token Celmis creates the
   hook (the workspace's webhook secret and the right events: GitHub
   `pull_request` and `push`; GitLab merge request events; Bitbucket
   `pullrequest:created`, `pullrequest:updated`, `pullrequest:fulfilled`,
   `pullrequest:rejected`) and switches auto-review on for that repository.
3. Success says "Webhook installed — new pull requests will be reviewed automatically". "Repair webhook" re-applies URL, events and secret and
   never creates a duplicate — use it after replacing a token or rotating
   the secret.

Requirements:
- Owner or admin of the workspace (anyone else gets the manual dialog).
- The server's `PUBLIC_BASE_URL` set to the address this Celmis is reachable
  at from the internet (not localhost); otherwise the button explains that it
  is not set — the operator sets it and restarts the API.
- A git token allowed to manage webhooks — the usual cause of "webhook failed":
  - GitHub: classic token with `admin:repo_hook` (or `repo` with admin rights
    on the repository); fine-grained token with Repository permissions →
    Webhooks: Read and write. The account must be an admin of the repository.
  - GitLab: the `api` scope, and Maintainer (or Owner) on the project.
  - Bitbucket: Atlassian API token scopes `read:webhook:bitbucket` and
    `write:webhook:bitbucket` (or Webhooks: Read and write on a repository or
    workspace access token); the account must be an admin of the repository.
  Fix the token on [Git connections](/connections) ("Replace token"), then
  press "Repair webhook".

Manual fallback: "manual setup" next to the button opens "Webhook for {repo}" with where to add it at the provider, the "Payload URL:", the events
and the secret — this workspace's webhook secret, generated or rotated on
[Review history](/reviews) in "Connect auto-review" (see the polling/webhook
section). Until a webhook delivers, nothing is reviewed automatically; the
empty [Issues](/issues) and [Pull requests](/pull-requests) pages offer
"Install the webhook" and "Run a review".
""",
    ),
    Section(
        id="repositories",
        title="Adding repositories, indexing, branch, vault, removing",
        keywords=(
            "repositor", "repo", "add", "register", "index", "reindex",
            "re-index", "clone", "branch", "vault", "graph", "remove", "purge",
            "delete", "freshness",
            "репозитор", "репо", "додати", "додав", "індекс", "переіндекс",
            "гілк", "бранч", "клон", "сховищ", "вилуч", "видал",
            "добав", "индекс", "переиндекс", "ветк", "удал",
        ),
        strong=("index", "індекс", "индекс", "vault", "branch", "гілк", "ветк"),
        body="""
Where: [Repositories](/repositories). Adding needs a connected provider
([Git connections](/connections)) for private repositories. A repository
belongs to exactly one workspace (adding it to a second answers 409 This
repository is already registered in another workspace).

Add:
1. In the "Add repository" card choose "By URL" (paste `https://github.com/
   owner/repo`, or `owner/name`, or `github:owner/name`; optional "Branch",
   empty = the repository's default) or "Browse" (pick from the connected
   provider, filter by owner or name) and press "Add".
2. Indexing starts by itself: the row shows "indexing…" and then "indexed".
   Indexing = clone + code/symbol graph (what review, impact analysis and
   search use). It calls no model and writes no documentation. If nothing
   was started, press "index now".
3. For Q&A in [Projects](/projects) and semantic [Search](/search), also press
   "Generate vault" (needs an LLM key): one LLM call per module, writes the
   documentation notes and their embeddings in the background. Choose the
   "Documentation language" and "Documentation engine" in its dialog.

Keep it fresh: an indexed row shows when it was indexed and a "Check" button
that asks the remote whether the branch moved and re-indexes if it did. A
daily sweep and the GitHub `push` webhook do the same. The header's index-all
button queues every repository that is not indexed.

Branch: click the branch chip on the row ("default branch" when none is
pinned) and pick another; then press "index now" again — the clone still holds
the old branch until re-indexed.

Webhook: the row's badge and "Install webhook" button set up automatic
review in one press (owner or admin; see the webhook-install section).

Other row buttons: the ⚡ icon lists the open pull requests with a "Review"
button each; the people icon rebuilds ownership (git blame + CODEOWNERS);
"open" goes to the provider.

Remove: trash icon → "Remove repository". Without "Purge all data" it only
leaves your list; with it, the clone, graph, vault notes, vectors, project
links and review history are deleted too. Removing needs admin permission on
that repository (open to everyone until team grants are configured on
[Code access](/admin/access) / [Teams](/admin/teams)).

Embeddings-model change: vectors made with another embeddings model or
dimension are not comparable — use "Reindex everything" on
[LLM Setup](/settings/llm) (see LLM setup).
""",
    ),
    Section(
        id="docs-deps-intel",
        title="Documentation, dependency audits (SBOM, evidence pack), repo intel",
        keywords=(
            "documentation", "docs", "doc", "prd", "dependenc", "audit",
            "sbom", "cyclonedx", "evidence", "vulnerab", "cve", "osv",
            "outdated", "intel", "ownership", "architecture", "export",
            "docx", "pdf",
            "документац", "залежн", "вразлив", "аудит", "застаріл",
            "архітектур", "власник",
            "зависимост", "уязвим", "устаревш", "архитектур",
        ),
        strong=("sbom", "evidence", "dependenc", "залежн", "зависимост"),
        body="""
Documentation — [Documentation](/docs): what the generator wrote about each
module. Generate it per repository with "Generate vault" on
[Repositories](/repositories), or here with "Generate the missing ones";
"Rewrite this one" redoes one note. Export as "Markdown", "Word (.docx)",
"PDF (print)" or "Download everything" (zip). Q&A does not need it when
source display is on.

Dependencies — [Dependencies](/dependencies): versions vs latest releases vs
known vulnerabilities (native auditors, OSV everywhere else; no model, no LLM
key needed).
1. Optionally set "Audit scope" ("All workspace repositories", "Specific repos", "By owner…", "By developer…", or a project) and a branch.
2. Press "Run audit"; it runs as a background job ("Restart audit" /
   "Cancel").
3. Read the findings (filters "Everything", "Outdated only", "Vulnerable only") and "Coverage & sources" — an ecosystem nobody scanned reports zero
   vulnerabilities exactly like a clean one, so check coverage.
4. "Download SBOM" (CycloneDX) and "Evidence pack" (SBOMs, findings, timeline
   and a sha256 manifest; verify offline with `pip install celmis` then
   `celmis verify evidence-pack.zip`).
5. "Generate report" writes an AI summary (engine "API model (review profile)" or "Claude Code"). "Fix from here" (pick a repository in the
   filter first) opens a Claude Code session that edits the manifests and
   opens a pull request — see Claude agent sessions.

Repo intelligence — [Repo intel](/admin/intel): architecture overview,
ownership (git blame + CODEOWNERS) and a reverse index (which notes document
which file); "Rebuild" refreshes. The repository must be indexed first.
""",
    ),
)
