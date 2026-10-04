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
global admin); anyone else gets 403 Requires owner/admin on this workspace. There is no field for a self-hosted GitLab or GitHub Enterprise
URL: verification and polling talk to gitlab.com / github.com.

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
Automatic review is switched on per repository on [Review history](/reviews)
(Code review → "Review history"), NOT on the Repositories page: the
"Auto-review PRs" panel has one switch per registered repository ("Automatic review for {repo}"). Under it is the "Connect auto-review" card that shows the
webhook URL and secret. The chat agent can also switch it on for a set of
repositories (and pin their branch) — ask it.

Two ways a new pull request reaches Celmis once the switch is on:
- Polling (GitHub and GitLab, every ~60 s). GitHub polling reads the token's
  notification inbox, so it needs a CLASSIC token with `notifications`;
  fine-grained tokens get 403, and GitHub never notifies you about your own
  pull requests. GitLab polls gitlab.com merge requests.
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
