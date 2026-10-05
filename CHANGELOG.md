# Changelog

This file begins on **19 August 2026**, at the state the repository was in
that day. It does not reconstruct what came before, and that is a fact about
the repository rather than a shortcut: this tree starts at a single root
commit. Development happened privately before it and is not published.
[`PROVENANCE.md`](PROVENANCE.md) states the licence position and the origin of
the code.

A changelog written backwards from a squashed tree would be fiction. So the
first entry below is the first change made *after* the rebuild, and everything
older is described in one line as the starting state.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html). The version
number lives in exactly one file — `src/__init__.py` — and everything else
derives it from there.

---

## [Unreleased]

## [2.3.2] — 2026-10-05

### Added

- **Self-hosted GitLab.** A GitLab connection takes a "GitLab URL" (own domain
  or a sub-path such as `https://host/gitlab`; gitlab.com when empty). Every
  GitLab operation — clone and fetch, merge requests, branches, review
  comments, approvals, descriptions, webhook install, polling, reviewer
  assignment, apply-fix, links — goes to the workspace's instance. The URL is
  checked like the LiteLLM proxy: https, no credentials or query, public
  addresses only, the address pinned on every connection; an internal host is
  allowed only by the operator (`GITLAB_ALLOWED_HOSTS`, plain http only by
  `GITLAB_HTTP_ALLOWED_HOSTS`), a private CA through `GITLAB_CA_BUNDLE`. TLS
  verification cannot be turned off. The token no longer appears in clone URLs,
  process arguments or `.git/config`; a webhook from another instance than the
  workspace's is refused.
- **The review settings panel folds**: Global, the repository list and every
  repository have a chevron; Global lists its sections with counts too; the
  state is remembered per user; tree keyboard navigation and Collapse all /
  Expand all.

### Fixed

- The full repository name no longer covers the "Per repository" heading; it is
  shown beside the panel and only when the name is cut off.

### Changed

- **Agent prompts are customised with guidelines that are ADDED, not swapped
  in.** Each agent in Code review settings → Custom prompts (Global and per
  repository) now has "Guidelines (added to the built-in prompt)": up to
  2,000 characters, appended after the agent's own prompt in a delimited
  `Team guidelines for <agent>` block that says they refine focus and
  wording but never override the prompt's scope, evidence, severity and
  output rules. A repository's guidelines replace the workspace's for that
  agent (as in Kodus, Qodo and Bito) unless "Also keep the workspace
  guidelines" is switched on (as CodeRabbit's `inheritance: true`), in
  which case both apply and the repository's win a disagreement. Replacing
  the whole prompt moved to "Advanced: replace the built-in prompt", with a
  warning; guidelines are still added to a replaced prompt. The preview
  folds the agent's own prompt and highlights the blocks added to it. The
  Claude Code engine gets every running agent's guidelines; the chat
  assistant can set them (`agent_prompt_guidelines`); the API accepts
  `agent_prompt_guidelines` / `agent_guidelines_extend` on repository
  policies and `PUT/DELETE /api/agents/{name}/guidelines` for the workspace.

### Migration

- **`c5d6e7f8a9b0` sorts the existing per-agent prompts.** Until now every
  per-agent prompt REPLACED the built-in one, so a short list pasted into
  "Defect" or "Security" silently dropped the tuned prompt. Each stored
  repository override is now classified: at most 2,000 characters, no
  output contract (no JSON, no `"reasoning"`/`"severity"`/`"file"` field,
  no "reply with"/"output format") and no `You are …` opening → moved to
  guidelines; anything else stays a replacement and behaves as before. Each
  conversion is logged by repository and agent (never the text). The
  workspace prompts (credential store) are sorted by the same rule once, at
  the first API start, and logged the same way. Downgrade restores every
  converted repository entry; `python -m src.review.prompt_guidelines_cli
  revert` does the same for the workspace prompts.

## [2.3.0] — 2026-10-05

Code review reaches parity with Kodus and goes past it: one settings page for
the workspace and every repository, pull-request actions, new review
categories, a rules library, a visible review pipeline, a chat assistant that
remembers and acts, and a refreshed visual system. Three new migrations run on
start (`f1a2b3c4d5e6`, `a7b8c9d0e1f2`); no repository reviews differently right
after the upgrade except where noted under Changed.

### Added

- **One settings page for code review** (`/review-settings`): "Global"
  (workspace defaults) and "Per repository" with search and an
  "Overridden N" count per repository; sections General, Review categories,
  Filters, Prompts, PR summary, Rules, Messages, Advanced. Every field says
  whether it is overridden here, inherited from Global or the built-in, and can
  be reset to what it inherits. The old Review defaults, Review policies and AI
  Agents pages redirect to the matching section. Resolution everywhere is
  repository → workspace → install → built-in.
- **New settings at both levels**: run on draft pull requests, approve when
  clean, request changes on critical findings, status feedback on skips,
  committable suggestions, apply filters to rules, summary placement and
  behaviour, base instruction, custom messages, opt-in agents
  (`enabled_agents`). `GET /api/review-settings/overview`.
- **Target branches take patterns**: globs (`release/*`) and exclusions
  (`!master`); an exclusion wins, a list of exclusions only means "every other
  branch". The skip reason names the pattern that decided.
- **Review categories Performance (on) and Business logic (opt-in)** next to
  Bug, Contract and Security; findings are grouped "By category" in the summary.
  Business logic checks the change against the PR description and its
  acceptance criteria and skips quietly without one. A **base instruction**
  (up to 2,000 characters) tells every agent how to write its comments.
- **The review pipeline is visible**: every run records its stages (received,
  settings, each gate, each agent with model and tokens, summary, publish,
  issues) with status, duration and reason; skips at the webhook and duplicate
  requests are recorded too. The Pull requests page expands a PR into its
  reviews and a review into its stage timeline.
- **Manual review of every open pull request** on GitHub, GitLab and
  Bitbucket: all target branches, every page, search, branch filter, "Review"
  and "Review all open PRs" (up to 25, through the queue).
- **The Celmis agent remembers the conversation** (last 8 exchanges, per chat)
  and can propose review rules, generate them for a repository, or change a
  review setting — each only after you confirm, with the same permissions as
  the settings page. It knows how to give another team access to explore code.
- **Visual system**: a teal primary (green now means only success), a warm
  "changed from default" tone, severity and status colour scales used
  everywhere with an icon beside every colour, WCAG AA contrast in both themes,
  option cards, segmented controls, slider, skeletons, empty states, motion that
  respects reduced-motion; the review pages (pull requests, issues, rules,
  analytics, history) are rebuilt on it. Code review tabs: Review history, Pull
  requests, Issues, Review rules, Settings, Analytics, More.
- **Pull-request actions on GitHub, GitLab and Bitbucket.**
  `approve_when_clean` approves a complete review with nothing to post and
  takes the approval back on a later run that is not clean (GitHub dismissal,
  GitLab unapprove, Bitbucket DELETE approve). `request_changes_on_critical`
  blocks on a critical finding (GitHub REQUEST_CHANGES, Bitbucket
  request-changes; GitLab has no such action, so it withdraws its approval and
  says so in the summary) and lifts the block when the next run has none.
- **The summary in the PR description** (`summary_target: description`),
  between `<!-- celmis:summary:start/end -->`, with
  `summary_existing_description` (append / replace / complement — one short
  LLM call that falls back to append) and `summary_on_new_commits`
  (nothing / append / replace). The summary comment then keeps the verdict and
  findings only.
- **Custom messages**: `message_started` and `message_finished_header`, with
  `{commit}`, `{agents}`, `{files}` and `{pr_number}`; anything else in braces
  stays as typed.
- Bitbucket Cloud renders a suggested change as a ```diff block: its "Suggest
  code" feature is editor-only and has no documented markdown for API comments.
- **Review rules** (Code review → Review rules, `/admin/review-rules`,
  `/api/review-rules`). Rules every review agent enforces, for the whole
  workspace or for one repository — a repository rule with the same title
  replaces the workspace one. Rules come from four places: written by hand,
  copied from a built-in library of about forty (PHP, JS/TS, Vue, Python,
  SQL, security, performance, general), generated for a repository by the
  workspace's review model (booked as `rules_generate`), or imported from
  the repository's own CONTRIBUTING / CLAUDE.md / AGENTS.md / Cursor and
  Copilot instructions / .editorconfig / ESLint / PHPCS / Ruff files.
  Everything a machine wrote arrives pending and reaches no review until it
  is approved. Agents cite the rule a finding violates; the finding takes
  the rule's severity and shows the rule's name on the pull request.
  Migration `a7b8c9d0e1f2`.

### Fixed

- **Bitbucket pull-request lists work with access tokens** (Bearer); they
  failed with 401 and showed only the first 50 pull requests on every provider.
- **A queued review whose provider credential was missing no longer stays
  "running" forever**; it ends as failed with the reason.
- **Access rules now cover every way into the code**: code search, generated
  documentation and the MCP graph tools apply the team's research rules (also
  on single-tenant installs); free Cypher is refused where a rule restricts the
  repository; every MCP tool re-checks a refused caller.
- **An MCP token answers for the workspace it was generated in**, not the
  user's highest-ranked one, and stops working when they leave it.
- **A team member can be added by email** or picked from the workspace's
  members; the field used to accept only an internal id it never showed.
- **The theme toggle drives dark mode**; `dark:` styles followed the operating
  system before.

### Changed

- **A GitHub review no longer approves or requests changes on its own.** The
  review event used to follow the verdict, so every clean review was an
  APPROVE and every blocking one a REQUEST_CHANGES. It is now COMMENT unless
  the repository turns on `approve_when_clean` or `request_changes_on_critical`
  (both off by default). Approvals left by earlier versions are not touched.
- **A fix hint is no longer rendered as a committable block.** `suggestion`
  (often prose, e.g. "=== / !==") is shown as a plain block; only the new
  exact replacement, `suggested_code`, can become a GitHub ```suggestion /
  GitLab ```suggestion:-0+N block, and only with `committable_suggestions` on.
- **A skipped review says so.** With `status_feedback` (on by default) a draft,
  oversized, empty or off-target pull request with no placeholder to rewrite
  gets one brief marked note, rewritten in place on later skips.

### Compatibility

- A repository policy's existing per-folder rules (`folder_rules`) are not
  converted and keep working exactly as before, edited where they always
  were; the rules page lists them read-only beside the new rules.

## [2.2.4] — 2026-10-04

### Fixed

- **A branch picker opens in about a second on a repository with thousands of
  branches.** 2.2.3 listed every branch, but did it by walking every provider
  page before answering: ~19 s on a real Bitbucket repository with 2,092
  branches. Where the provider can search branch names (GitLab, Bitbucket) the
  full walk is now off the request path: a search goes to the provider
  (~0.7 s, every branch still findable), an empty query reads only the newest
  page with the default branch on top (~1.2 s) and says there is more, and a
  full listing already in the cache answers anything at once. GitHub, which
  has no branch-search API, keeps the capped full walk. The cache now keeps a
  listing for 5 minutes instead of 90 s.

## [2.2.3] — 2026-10-04

### Added

- **Workspace review defaults** (Code review → **Review defaults**, `/admin/review-defaults`). A workspace now
  sets, once for all its repositories: which agents take part (verifier included) and each agent's model and
  output limit, the comment threshold, the inline-comment cap, the PR summary and its instructions, the
  "review started" comment, the review language, ignore paths and target branches. Every repository inherits
  them and can override any field on its own policy page, which shows "overridden here" / "inherited from
  workspace" / "install default" with a reset to the inherited value. Resolution everywhere — orchestrator and
  API — is repository → workspace → install default → built-in. `GET|PUT /api/review-defaults` (owner/admin
  or global admin write, members read; audited). Per-agent models and the language stay where they already
  lived, in the workspace LLM settings, so there is one copy of each. Migration `e4c8a1f7b2d9`: repository
  fields that only held the old defaults (`[]`, `true`) become "inherit", explicit values stay — no
  repository reviews differently right after the upgrade. The "Agents & prompts" tab of a repository now shows
  each agent's participation switch together with its model and output limit.

### Fixed

- **Branch pickers show every branch and can search them.** The branch lists
  on Repositories (the branch chip and the PR filter), Dependencies (the
  run-wide override and the per-repository table) and the review policy's
  target branches read one provider page — 100 names — and presented it as the
  whole list. The server now follows every page (GitHub `Link`, GitLab
  `X-Next-Page`, Bitbucket `next`) up to 5,000 names, caches the listing for
  90 s per repository and credential, and searches it (`?q=`, case-insensitive
  substring; GitLab and Bitbucket are also searched server-side when the cap
  cut the listing). The default branch comes first, then the most recently
  updated where the provider says so (GitLab, Bitbucket), else A→Z. Every
  picker is one searchable, keyboard-accessible combobox that says how many
  matches it is not showing. The review-policy list now comes from the
  provider too; it read the local clone, which is single-branch.

### Changed

- `PUT /api/review-policies/{slug}`: `target_branches` and `disabled_agents` follow the other optional fields —
  absent keeps the stored value, `null` inherits, a list (even `[]`) is the repository's own. Policy reads may
  return `null` for `target_branches`, `disabled_agents`, `ignore_globs`, `summary_enabled` and
  `started_comment_enabled`, with the resolved value in the matching `*_effective` field and its origin in
  `sources`.
- `GET /api/repos/{slug}/branches` returns an object —
  `{repo_slug, branches, default_branch, total, truncated, source, error}` —
  instead of a bare list, and takes `q` and `limit` (default 100, max 1000).
  `GET /api/review-policies/{slug}/branches` takes the same parameters and
  gains `total`, `truncated` and `source`.

## [2.2.1] — 2026-10-04

The version jumps from 0.2.1 to 2.2.1 by the maintainer's decision; nothing in
the compatibility promise changed with it — upgrade the same way as before
(new images, migrations run on start).

### Added

- **The review announces itself and ends in one Kodus-style summary.** On
  GitHub, GitLab and Bitbucket a "🔄 Celmis is reviewing this PR…" comment is
  posted as soon as a review starts (commit, agents, file count), and the same
  comment is rewritten in place into the summary: verdict, **Summary** (a short
  overview of what the PR changes), **Changes walkthrough** (one line per file,
  up to 30), **Findings** (by severity and by agent, top findings with links),
  and scope/performance in a collapsed block. The overview and walkthrough come
  from one extra cheap LLM call on the review's own client (booked as
  `review_summary`, 45 s timeout, no retries); if it fails the summary is
  rendered without those sections — the review never fails because of it.
  Skipped and failed runs finalize a placeholder ("⏭️ Skipped: …" /
  "❌ Review failed: …") but never overwrite a finished summary. A summary that
  could not be written now marks the run PARTIAL with `post_error` on every
  provider (it already did on Bitbucket).
- **Per-repository review customization, Kodus-style.** The review policy page
  is organised into tabs — General, Agents & prompts, Rules, Comments & summary,
  Ignore paths, Models & limits, MCP sources (deep-linkable with `?tab=`).
  - Every overridable agent, the verifier included, can have its own system
    prompt per repository, with preview, an inherited/overridden badge and
    reset. The verifier's prompt override was stored but never read; it is now
    honoured (repo → workspace → built-in).
  - Structured custom rules: title, file glob, severity hint and the agents a
    rule applies to; old `{pattern, prompt}` rules work unchanged.
  - Suppressed rule ids, per-repo review language, inline-comment cap (1–100),
    summary on/off, summary instructions and the "review started" switch.
    New nullable columns on `repo_review_policies` (migration `d7a3e9c51b64`).
  - The AI Agents page says that a repository override wins and shows, per
    agent, in how many repositories it is overridden, with links
    (`GET /api/review-policies/overrides-summary`, workspace-scoped).
- **Webhooks install themselves.** "Install webhook" / "Repair webhook" on every
  repository row creates (or updates in place, never duplicates) the review
  webhook on GitHub, GitLab or Bitbucket with the workspace's secret, and turns
  auto-review on. Registration through the API with `auto_review` tries it too;
  a refusal never fails registration and comes back with the reason and the
  token permission to add. Manual setup stays one click away. Requires a public
  `PUBLIC_BASE_URL`. `POST|GET|DELETE /api/repos/{slug}/webhook`. Repository
  lookups for webhooks are case-insensitive (fail-closed on ambiguity).
  The empty Issues and Pull requests pages now say that nothing has been
  reviewed yet and link to installing the webhook or running a review.
- **The Celmis agent knows the product.** A packaged knowledge base (29
  sections, EN/UK/RU keywords) is searched per question and sent with the
  prompt; answers are concrete numbered steps with real page names, in the
  user's language, and say whether the asker's role allows the action.

### Changed

- **Embeddings are an installation-wide setting** edited by a global admin
  from any workspace (`PUT /api/llm/embeddings`), and LiteLLM is offered for
  them: the installation embeddings proxy is the default workspace's proxy and
  can be connected inline from the Embeddings card. Saving embeddings from a
  non-default workspace used to be silently discarded; it is now stored where
  it is read, or refused with a reason. A workspace owner who is not a global
  admin can no longer change embeddings.
- **A workspace owner grants admin and editor** (and member/viewer) in their
  own workspace; the owner role itself stays with the superadmin, and an admin
  still manages only members and viewers.

### Fixed

- Review policies help no longer says the verifier always runs (it is opt-in).

## [0.2.1] — 2026-10-03

### Added

- **Access requests.** A signed-in person whose only workspace is their
  personal one — password, Google or SSO — can ask for access in general
  (dashboard banner, `/access-request`, optional comment up to 1000
  characters; one pending at a time, cancellable, re-askable after a
  rejection; a cancel racing an approval is 409, never a "cancelled" request
  over granted memberships; access given another way — the Users page, an
  accepted invite — closes it as approved). The request names no workspace and the requester sees none
  until it is approved. The superadmin alone decides on **Administration →
  Access requests**: approve with one or more (workspace, role) pairs, applied
  atomically through the membership writer (`change_memberships`, one audit
  row each, `via="access_request"`), or reject with a required reason.
  Deciding twice is 409; create, cancel, approve and reject are audited. The
  decision shows on `/access-request` and as a toast on the next visit, the
  switcher re-reads its list, and with SMTP configured the requester is
  emailed. New table `access_requests` (migration `f6b1d3a8c240`). API:
  `POST|GET|DELETE /api/access-requests[/me]`, `GET
  /api/admin/access-requests?status=`, `POST
  /api/admin/access-requests/{id}/approve|reject`.
- **Administration → Users: "No team access" filter**
  (`GET /api/admin/users?no_team_access=true`) — accounts with no workspace
  but their personal one, any sign-in method, newest first, with their
  sign-in methods and sign-up date.
- **Invites reach people without an account, end to end.** The landing page
  says who sent the invite, and signed out offers sign-in, sign-up (when
  password login is on), Google and SSO, each returning to the link. A dead
  link says whether it expired, was revoked or was used; opening a used link
  again as the person it was for opens the workspace. The inviter is told
  when the link was also emailed.
- **Invites are redeemed at a verified Google / SSO sign-in.** Pending
  email-bound invites for the address the identity provider marks verified
  are redeemed automatically, through the same path as accepting the link —
  the inviter's right re-checked, audited, single use. Password accounts are
  never redeemed by their address (it is not verified), including one that
  also has Google/SSO linked; they accept through the link. The automatic
  path never changes an existing member's role (the invite is used up,
  `invite.auto_redeem_skipped`), and removing a member revokes the live
  invites addressed to them for that workspace (`invite.revoked_on_removal`).

- **Administration → Users (`/admin/users`), for the superadmin.** Search
  accounts, see each person's workspaces and role in each, change a role, add
  a membership, remove one — so one person can be admin of several
  workspaces and editor of several others. API: `GET /api/admin/users?q=`,
  `GET /api/admin/workspaces`, `GET|PUT|DELETE
  /api/admin/users/{user_id}/memberships[/{ws_id}]`. Every membership change
  from any page is now audited as `workspace.member_role_changed` (actor,
  person, workspace, old → new role, which path).

- **Manual prices for LiteLLM proxy aliases.** An alias on a workspace's own
  LiteLLM proxy names whatever the proxy maps it to, so Celmis could price it
  only when the model behind it was in LiteLLM's table; a fine-tune or a
  custom upstream was recorded with an unknown cost. Settings → LLM now shows
  a **Model prices** table under the connected proxy: every alias, its mode,
  the model behind it, the effective price in USD per 1M input/output tokens
  and where that price came from (Manual / Proxy / LiteLLM table / Unknown).
  A workspace admin can set a price per alias or reset it to automatic;
  members see the table read-only. One resolver
  (`src/llm/proxy_pricing.py`) now prices every workspace-proxy call — review
  and agent calls, chat streaming, embeddings and the spend ledger — in this
  order: the manual price, an amount the response says was charged, the price
  the proxy declares in `/model/info` (`input_cost_per_token` /
  `output_cost_per_token`, which was ignored until now), LiteLLM's table price
  of the underlying model, unknown. New `cost_source` values `manual_price`
  and `proxy_price`. Embeddings use the default workspace's prices, because
  they run on its proxy. Prices are stored per workspace in the LLM config
  (`model_prices`), set through `GET`/`PUT /api/llm/litellm/prices`
  (admin-only write, validated before anything is written, audited as
  `llm_model_price.updated`), and apply to calls made after they are saved —
  past ledger rows are not rewritten. The installation gateway is unchanged.
  An alias is no longer priced by its own name: one called `gpt-4o` that runs
  something else, or whose `/model/info` the proxy refuses, is Unknown both in
  the table and in the ledger (the two share one resolver). The proxy's
  declared prices are cached for up to an hour; **Refresh from proxy**
  (`GET /api/llm/litellm/prices?refresh=true`) re-reads them. Writers of the
  workspace LLM config are now serialised per workspace, so saving the main
  LLM form at the same moment as a price no longer drops the price.
- **The enterprise licence can be entered from the UI.** A global admin
  pastes the key into the Edition card on **Admin → Health**
  (`GET`/`PUT`/`DELETE /api/license`, `src/ee/license_router.py`). It is
  verified first — an invalid, expired, not-yet-valid, wrong-issuer,
  featureless or oversized key is refused with the reason and nothing is
  stored — then kept encrypted in the credential store under an
  installation-wide slot, and applied without a restart: the granted features
  are mounted, `/api/capabilities` reports `enterprise` at once, and the
  Analytics tab and the SSO button appear without a reload. Removing it, or
  replacing it with one that drops a feature, unmounts those routes at once,
  and the per-request licence check now reads the licence in force rather
  than the one captured at start-up. Save, replace and remove are audited
  (customer, features, expiry — never the key). See
  [`ee/README.md`](ee/README.md#installing-a-licence).

### Changed

- **Licence precedence is `CELMIS_LICENSE_KEY` > `CELMIS_LICENSE_FILE` >
  the key entered in the UI.** While either variable is set the UI shows the
  licence as managed by the server environment and the API answers 409 to a
  save or a removal instead of storing a key the variable would shadow.
- **Who may grant a workspace role.** The superadmin is the env master
  account (`CELMIS_MASTER_EMAIL` + `CELMIS_MASTER_KEY`) and nobody else; other
  global admins keep their platform pages but are not superadmins. Granting,
  changing to or from, and removing **owner, admin or editor** is the
  superadmin's alone; a workspace's owner/admin manages **members and
  viewers**. A workspace admin can no longer demote the owner or another
  admin, nor mint a password-reset link for one. One rule (`can_change`,
  `src/users/roles.py`) behind every path: member PUT/DELETE, invite create
  and accept, the Users page. Invites carry only a role their creator may
  grant (403 otherwise), re-checked against the creator when accepted.
- **Creating a shared workspace is the superadmin's** (`POST
  /api/workspaces`); personal workspaces are still provisioned at sign-up.
  Deleting a workspace takes its owner or the superadmin — no longer any
  admin.
- **`editor` is the prompt editor.** Agent system prompts (`PUT/DELETE
  /api/agents/{name}/prompt`) and review policies (`PUT/DELETE
  /api/review-policies/{slug}`) need editor, admin or owner of the workspace;
  the policy write also needs the repository to be registered in that
  workspace, plus the team grant as before. A member with a `review` grant
  can no longer rewrite the prompts a repository is reviewed with. Resetting
  a policy needs `review` on the repo, like saving it (was `admin`).
- **Team roles** come from the shared role table (`TEAM_ROLES`): the
  workspace roles plus the team-only `reviewer`; `editor` is now accepted.
- The workspaces page draws only the controls the signed-in person can use:
  role pickers offer grantable roles, members above the actor show no
  change/remove/reset buttons, and a card's invites now act on that card's
  workspace rather than the active one.

### Security

- **Repository routes no longer reach another workspace's repository.** Every
  by-slug route under `/api/repos` (index, freshness, vault, pulls,
  branches, auto-review and branch settings) fell back to "a row this user
  registered", which could sit in a different workspace: a person removed
  from a workspace kept indexing its repositories, listing their pull
  requests and toggling their auto-review — with that workspace's stored
  token. Now only the active workspace's registration counts.
- `GET /api/review-policies/{slug}/branches` read any clone on disk by slug;
  it now requires the repository to be the workspace's.
- Team/repo grants are looked up among the active workspace's own teams. A
  grant written in one workspace decided access to another workspace's copy
  of the same repository.
- A team can no longer take a person who is not a member of its workspace.
- Signing up with the master address is refused: the master login adopts a
  password account holding it, which made a pre-emptive signup a route to
  superadmin.
- New test module `tests/security/test_tenant_isolation_matrix.py`: the
  admin, editor and member of one workspace against ~55 routes of another,
  via `X-Workspace`, the cookie and direct ids/slugs, with the other
  workspace's state read back afterwards.
- **No account takeover through membership.** Inviting an existing account
  by email enrolled it on the spot, without consent, and the workspace
  reset-link route then treated that membership as authority to mint a
  password-reset link. Since every account owns a personal workspace, any
  signup could take over any non-global-admin account, including the owner
  or admin of another workspace. Now a direct add happens only for somebody the
  inviter already shares a workspace with (everyone else gets an invitation
  to accept), and a workspace reset link requires the right to change the
  target in *every* workspace they belong to. In multi_tenant mode that in
  practice leaves the superadmin, or the account itself.
- Neither reset-link route (`/api/users/{id}/reset-link`,
  `/api/workspaces/{id}/members/{user}/reset-link`) mints a link for the
  master identity any more, including a password account the master login
  adopted by address. Previously a global admin could reset it and log in
  as the superadmin.
- `is_superadmin` no longer matches a Google/OIDC-bound account by the
  master address. It now follows the same rule as the master login's
  adoption, so tokens such an account already holds do not become superadmin
  tokens.
- An invite's granting authority is resolved by the issuer's user id (new
  column `workspace_invites.created_by_id`, migration `e5a7c2f19d63`). Older
  rows fall back to the email. Changing `CELMIS_MASTER_EMAIL` no longer voids
  the superadmin's pending invites.
- Creating a workspace writes its owner row through the membership writer,
  so the grant leaves a `workspace.member_role_changed` audit row like every
  other grant.

### Security

- **MCP graph tools are confined to the caller's workspace under
  `multi_tenant`.** `find_symbol`, `get_symbol`, `find_callers`,
  `find_callees`, `query_graph`, `cross_repo_edges`, `list_repos` and
  `list_groups` on the `analyzer mcp serve` server checked the `read:graph`
  scope and nothing else, and graph files live flat at
  `<data_dir>/<repo_slug>/graph.fdblite`. Any token with `read:graph` could
  read another tenant's symbol graph and run read-only Cypher against it by
  naming its slug. Each tool now requires the repository (or group) to be
  registered to the caller's workspace and researchable under the access
  rules; deny-globbed files are filtered from results, and raw Cypher is
  refused on a repository with path restrictions. Unknown, foreign and refused
  targets answer exactly like a missing graph. `single_tenant` behaviour is
  unchanged apart from the slug check below.
- **Repo slugs are validated before they become paths.** `repo_path`,
  `repo_data_path`, `repo_graph_path` and `repo_vault_path` refuse anything
  outside `[A-Za-z0-9._-]`, `..`, `.` and empty strings, and check that the
  result is a direct child of its base directory. `../` in a slug reached any
  graph file on the box. The API answers 404 for a refused slug.
- **The `/mcp` mount's research-access check now includes the tenant binding
  under `multi_tenant`**, for global admins too, so `get_api_surface`,
  `get_owner`, `get_architecture`, `route_incident`, `get_review_policy`,
  `get_my_access` and the project tools refuse a repository registered to
  another workspace. `list_deprecations` and `get_review` read only the
  caller's workspace.
- **REST:** under `multi_tenant`, routes guarded by `require_repo_permission`
  (`/api/intel/ownership|architecture|reverse-index/{repo_slug}` and their
  rebuilds, policy writes, repo delete) return 404 for a repository outside
  the active workspace, global admins included.
  `GET /api/review-policies/{repo_slug}/branches` no longer runs `git` in an
  unregistered or traversal path, in any mode. The deprecation consumer scan
  only walks the deprecation's own workspace's repositories under
  `multi_tenant`, and the branches route no longer accepts a user's
  registration row from a workspace they have left.
- **MCP project tools and `bootstrap_client`.** Under `multi_tenant`,
  `search_symbols`, `find_consumers`, `migrate_consumers` and
  `bootstrap_client` resolve a project's repositories only for a project in
  the caller's workspace; another tenant's project reads like a missing one
  instead of naming its repositories in `blocked_repos`. `bootstrap_client`
  returns `top_owners` only for a target the caller may research (it read
  any slug's ownership snapshot, so it handed out another tenant's top
  committers).
- **Raw group reads need unrestricted members.** `query_graph(group_name=…)`
  and `cross_repo_edges` return ids and files from member repositories, which
  cannot be filtered by path afterwards, so under `multi_tenant` they now
  require every member to be readable without path restrictions, as
  `query_graph(repo_slug=…)` already did.
- **Registering a repository whose slug is not a safe path segment is refused
  (422)** on `POST /api/repos` and the automation surface, before anything is
  stored. A row stored earlier with such a slug no longer breaks the
  repository listing, "index all" or the docs export.

## [0.2.0] — 2026-10-03

### Added

- **Celmis Enterprise Edition, and the licence that switches it on.** Two new
  features ship under [`LICENSE_EE`](LICENSE_EE) rather than the AGPL, in
  `src/ee/`, `web/ee/` and `tests/ee/` (see [`ee/README.md`](ee/README.md)).
  They are enabled by an offline signed licence — an Ed25519 (`EdDSA`) JWT
  verified at API start-up against a public key in `src/ee/license.py`, set
  through `CELMIS_LICENSE_KEY` or `CELMIS_LICENSE_FILE`. It never calls home.
  No licence, or an invalid or expired one, is the **community edition**: the
  enterprise routers are not mounted, the reason is logged as a warning
  (never the token), and everything else works as before. Each feature is
  mounted only if the licence lists it; a licence that lapses while the
  process runs stops serving them with a 403. `scripts/ee_mint_license.py`
  mints a licence from a private key kept outside the repository.
- **[EE] Single sign-on with Keycloak or any OIDC provider.** `POST
  /api/auth/oidc` verifies the id_token against the issuer's JWKS (issuer,
  audience/azp, Keycloak `typ`, expiry; RSA/PSS/EC algorithms only), links an
  existing account only for a verified email, never the master account, and
  can grant — or with `OIDC_ADMIN_ROLE_SYNC=true` revoke — global admin from
  an IdP role. The "Sign in with …" button appears only when the web side is
  configured (`AUTH_OIDC_*`) **and** the API reports the `sso` capability.
  A local Keycloak to try it with is in `deploy/keycloak/`.
- **[EE] Review analytics** at `/analytics`: reviews run, review time
  (average, p50, p90), cost per review from `review_runs.cost_usd`, findings by
  severity and category, and what came of them — fixed in the next commits,
  left open on merged PRs, fix rate — over 7, 30 or 90 days. For a global
  admin or the owner, admin or editor of the workspace. Without a licence the
  tab is hidden and the page says it is part of Celmis Enterprise.
- **`/api/capabilities` reports the edition.** `edition` is now `community`
  or `enterprise` (it used to be `full`/`partial`, which moved to
  `complete`), and `license` carries the granted features — plus, for a
  signed-in caller only, the customer and expiry. `/admin/health` shows them
  as an Edition card. `schema_version` is 2.
- **LiteLLM proxy as an LLM provider**: point Celmis at an existing LiteLLM
  gateway and use the models it routes, with the gateway's own keys, budgets
  and logging. Configured only in the UI by a workspace admin: the URL must be
  public `https` (no userinfo, query or fragment; private, loopback,
  link-local, metadata and CGNAT addresses refused after DNS resolution; the
  connection is pinned to the checked address; no redirects; 2 MB cap), and
  it is saved only after `/v1/models` answers with that key. URL and key are
  stored encrypted; the API shows a masked key and a fingerprint, the host to
  admins only. Model pickers list what the proxy serves. A LAN proxy needs
  `LITELLM_PROXY_ALLOWED_HOSTS`. Proxy aliases get no `cache_control`
  breakpoints unless the proxy reports Claude behind them (forwarded to a
  Gemini free tier they failed every call with a 429).
- **Issues**: findings followed across a PR's pushes by a line-free
  fingerprint, marked fixed when a later commit removes them, with status
  tabs, filters and per-row status changes (`/issues`, `GET/PATCH
  /api/issues`). "Fixed" needs the flagged line itself gone, not just its
  file changed; a finding the model re-words (new title, new rule id) on the
  same line re-finds its issue instead of opening a second one.
- **Pull requests**: every reviewed PR with its state from close/merge
  webhooks, review count and open suggestions (`/pull-requests`).
- **Ignore globs** per repository review policy: matching paths are skipped
  and cut from the diff the agents read. Matched without backtracking; at most
  eight `*` per pattern.
- **Comment threshold** per policy: findings below the chosen severity are not
  posted inline, and the summary says how many were held back.
- **Editor workspace role** between member and admin: may read analytics, has
  no workspace-admin powers. One role table (`src/users/roles.py`) now serves
  workspaces, invites and the MCP identity resolver.
- **`AUTH_PASSWORD_LOGIN=false`** turns off email+password sign-in and signup
  for SSO-only installs; the master-key login keeps working as break-glass.
- **The Celmis agent as a floating panel on every page**, sharing one
  conversation with `/automation`, and able to answer "how do I…" and "where
  is…" from a curated product guide with links to the right pages.
- **Sidebar**: no width flash on first paint (state in a cookie), Cmd/Ctrl+B
  to toggle, tooltips on the collapsed rail, theme and account menu in its
  footer.

### Fixed

- A review's cost is no longer "unknown" because agents that call no model
  (cve, structural) ran in it.
- Tests that pinned LiteLLM's model table (red on `main` since 2026-09-16)
  read it from the installed version instead.
- **Google sign-in** no longer links or creates an account for an email
  Google has not verified.
- **An SSO-only session is not renewed** by `/api/auth/refresh`, so disabling
  a user in the IdP takes effect when their session expires.
- **Dark mode followed the operating system instead of the theme toggle** for
  every `dark:` utility; it now follows the toggle.
- `isAdmin` in the web session is re-read every five minutes instead of once
  at sign-in.

## [0.1.31] — 2026-09-14

### Fixed

- **`celmis-web`'s release build failed on `v0.1.30`**, silently: the
  `publish` job needs all three images, so no GitHub Release was cut for
  that tag even though `celmis-api` and `celmis-sandbox` published fine
  (`fail-fast: false` keeps a matrix leg's failure from hiding the others).

  `pnpm install --frozen-lockfile` errored with `ERR_PNPM_IGNORED_BUILDS`.
  Two things, and only the second one bit: `web/pnpm-workspace.yaml`
  already named `sharp` and `unrs-resolver` under `allowBuilds`, correctly —
  but `web/Dockerfile`'s `COPY` line never carried the file into the build
  context, so pnpm never saw it. Fixed by copying it in. The second: the
  Dockerfile's own fallback — `--config.dangerouslyAllowAllBuilds=true` —
  stopped working in pnpm 12, which reads neither that flag nor
  `package.json`'s old `pnpm.onlyBuiltDependencies` field, both silently,
  with no error at parse time to say so. That second fact made the first
  invisible: the flag used to cover for the missing `COPY` line, until
  `corepack prepare pnpm@latest` picked up v12 on some build between
  `v0.1.29` (31 August) and this one and the cover stopped working. Also
  dropped `onlyBuiltDependencies` from `pnpm-workspace.yaml` itself — the
  pre-v11 spelling of `allowBuilds`, dead since, kept alongside it in a way
  that implied two mechanisms where there is now one.

## [0.1.30] — 2026-09-14

### Added

- **The runtime image now carries `LABEL io.modelcontextprotocol.server.name`.**
  The official MCP registry checks this against the image itself before it
  will accept a `server.json` package entry — a JSON claim alone isn't
  enough, the image has to say the same thing GHCR is actually serving.
  `server.json` is added at the repository root, published under
  `io.github.constantinemakoid/celmis`. The `io.github.celmis-labs/*`
  namespace is what the entry should carry, and could not be used this time:
  the registry's GitHub-organization auth has an open upstream bug (public
  membership confirmed, fresh tokens, no effect — several other reporters hit
  the identical 403), not anything wrong on this repository's side. The
  record moves to the org namespace once that is fixed.

## [0.1.29] — 2026-08-31

### Fixed

- **A paused session reconnected every two seconds, for as long as the tab was
  open.** Measured on production: **36 event-stream connections in 75
  seconds**, under an amber "Reconnecting…" badge, on a conversation that was
  perfectly healthy and simply waiting for somebody to type.

  Two causes that compound. The retry loop stopped only on `final` — "cannot
  continue at all" — while a paused session reports `resumable`, which
  continues when a person sends a turn and never on its own. And the backoff
  reset on any frame, `stream_end` included, so every reconnect that found
  nothing reset the counter it was supposed to grow.

  What still reconnects is the state between the two: a RUNNING session whose
  API restarted under it, which reports neither flag. That is the deploy case
  the server comment describes, and stopping on any `stream_end` would strand
  it. Sending a turn now wakes the stream, because stopping the loop is only
  safe if typing restarts it.

## [0.1.28] — 2026-08-31

### Fixed

- **A notification with a relative link arrived as an empty card.** Google
  Chat validates `openLink.url`; `/claude/<session-id>` fails that validation
  and the whole card is dropped, so what lands in the room is the bot's name
  with nothing under it — while the log records `notif_delivered … delivered=1`.
  Seen on a phone, as a hole in the feed between a firing alert and its
  recovery, at the exact minute an agent session finished.

  The alerts path already knew this rule and wrote it down; the agent path did
  not. It now lives in one place — `dispatch.public_link`, absolute or nothing
  — and a guard reads every `notify(link_url=…)` call site with `ast`.

- **`**0** critical · **4** error` reached a chat card with the asterisks in
  it.** `body_md` is markdown and every other adapter is right to treat it as
  such — Slack takes mrkdwn, Discord takes markdown — but a Google Chat
  `textParagraph` renders a small HTML subset and prints the rest verbatim.
  Bold, italic, inline code and links are converted now, and the text is
  escaped first: a notification body carries an alert title from somebody
  else's monitoring, and that must not be able to put markup into a card sent
  under this product's branding.

### Changed

- The agent's own notification was titled "Claude Code finished a step". The
  page it links to is called **Agent** in all sixteen locales.

## [0.1.27] — 2026-08-31

### Fixed

- **A recovery paged exactly as hard as the outage did.** Grafana sends the
  same labels when an alert resolves as when it fires — the labels identify
  the rule, not the event — and only `status` says which happened. The parser
  read the labels and ignored `status`.

  Reproduced end to end on production, not deduced: a test gateway came back
  up at 14:45 and the workspace was paged `critical` with the title
  `5xx rate 100% over 1m`, byte for byte the card it had sent at 14:30 when
  the service actually broke. A false page is noise; a false page
  indistinguishable from the real one teaches people that a critical card
  might mean nothing. A resolved alert now says so in its title and arrives as
  `info`. It still arrives — coming back is worth knowing — it just stops
  impersonating an incident.

## [0.1.26] — 2026-08-31

### Changed

- **The agent page was headed "Claude Code" while the sidebar entry that
  opens it said "Agent".** Both were on screen at once — sidebar, breadcrumb
  and H1 in one frame — and that frame is the screenshot published in the
  guide. Two things were wrong and only one is about a trademark: a page whose
  title contradicts the entry you clicked to reach it is a bug at any name.
  `claude.title` now equals `nav.agent` in all sixteen locales.

  Every descriptive mention stays, because the terms allow saying a product
  runs Claude Code: the connection card, the token walkthrough, the engine
  pickers, the MCP client list, `AI report: Claude Code` beside `off` and
  `API`. The guard that was supposed to protect the permitted half had picked
  `claude.title` as its specimen — a heading, not a sentence — so it asserted
  the one string that had to change. It now watches `claude.helpTitle`, on the
  same page, and two new guards hold the heading: it must agree with its nav
  entry, and neither may carry the engine's name.

## [0.1.25] — 2026-08-31

### Security

- **The sandbox firewall covered one of the two ways in.** The INPUT rule was
  in place and the deploy logged "sandbox→host blocked"; probed from inside
  the running container, `host:22` timed out and `host:80` answered. A
  container port published on the host is destination-NAT'd and then
  forwarded, so INPUT never sees it.

  Nothing was exposed by it — the only `0.0.0.0` port is Caddy, which the
  internet reaches anyway, and postgres, the api and the web app are bound to
  `127.0.0.1`; cross-network isolation was probed too and holds. The rule is
  here because the next published port is the one nobody re-checks. A second
  rule in `DOCKER-USER` matches `--ctstate DNAT`: exactly the packets that
  arrived through a published port, leaving the sandbox's internet egress and
  the api on the sandbox network alone. Verified on production after applying
  it — 22 blocked, 80 blocked, internet open, api open.

### Fixed

- **CI could not see the history it was asked about.** `actions/checkout`
  fetches one commit and no tags. The guard that recomputes PROVENANCE.md's
  numbers failed with "the record pins v0.1.23, which is not a tag in this
  repository" — a true sentence about the clone and a false accusation against
  the file — and the guard that no commit ever weakened that record and its
  own test together walked a log of one commit and passed on nothing. Proven
  on a `--depth 1` clone. `fetch-depth: 0`, and both now fail loudly on a
  shallow clone rather than answering a question they cannot see.

## [0.1.24] — 2026-08-31

### Security

- **The sandbox firewall step said it was not optional and continued without
  it.** Both failure branches logged a `WARNING:` and carried on, five lines
  after a real `fail` on a missing `.env` — so a host without iptables shipped
  a container that runs a tenant's own build commands with a route to the host.
  It now tries iptables, then nft, then stops, with
  `CELMIS_ALLOW_UNFIREWALLED_SANDBOX=1` as a typed decision for a host isolated
  another way.

  And it did not survive a reboot. Measured on the production box: the rule was
  in place, `iptables-persistent` was absent and crontab was **empty** — so the
  gap after a restart was not until the next scheduled deploy, it was until
  somebody deployed by hand. A systemd unit ordered `Before=docker.service`
  reinstates it at boot.

### Added

- **Publishing is a workflow now, not a person with a token.**
  `publish-verifier.yml` uses PyPI Trusted Publishing, so upload rights belong
  to this workflow in this repository for the length of a run. The tag prefix
  is `pypi-`, and the first letter is the reason: `release.yml` triggers on the
  glob `v*`, which `verifier-0.2.1` matches, because "verifier" starts with a
  v — that tag would have built three container images.

- **Images carry a signed build attestation.** `gh attestation verify` now
  answers "built from which commit, by which workflow" for somebody holding
  only the image; before this the OCI `revision` label was the only claim, and
  a label is text anybody can write. Buildkit's own `provenance: false` stays —
  different mechanism, and the comment there is right.

- **`packaging/pypi/DIGESTS.md`** records the sha256 of every published file in
  the git repository, because PyPI is the same channel that serves them. It
  says plainly what it is: the 0.1.0 and 0.2.0 digests were read from PyPI and
  written down, which is a trust-on-first-use anchor and not independent
  attestation. From the first workflow-published release the digests are
  printed before upload and the artefacts carry a PyPI attestation.

### Fixed

- **The lint gate everybody believed in enforced nothing.** `ci.yml` said the
  rules-of-hooks rule was blocking as a test in the python job; those tests
  carry `skipif(not _eslint_available())` and that job never installs
  `web/node_modules`, so they reported SKIPPED in green — while this step ended
  in `|| echo`. Measured for the first time: 42 findings, 31 errors, 24 of them
  in the react-hooks family whose breach took every authenticated page down.
  `scripts/eslint_gate.py` makes rules-of-hooks fatal at any count and ratchets
  the rest from 42.

- **The verifier read whatever an archive told it to.** A zip declares each
  member's size before you read it, and neither copy checked. Measured against
  the published `celmis 0.2.0`: an archive of 200 KB on disk declaring 200 MB
  verified as `OK` with a 215 MB peak, and the number was the sender's to
  choose. The person running that got the file from the party they are
  checking, on their own laptop, precisely because they do not trust them.

  Sizes are checked before anything is read — per member, and in total, which
  is what a spread of medium files defeats — and hashing streams a megabyte at
  a time, counting what actually arrives so a header that lies in the other
  direction is refused too. Limits are three orders of magnitude above a real
  pack, and identical in both copies, held so by a test.

- **A manifest that was valid JSON and not an object crashed the verifier.**
  `manifest.get(...)` on a list raised AttributeError: plain output reported it
  as a problem found (exit 1) and `--json` printed a traceback, also exit 1.
  Neither was true — nothing had been checked, which is exit 2. Both modes now
  agree, and the refusal says what the manifest actually was.

## [0.1.23] — 2026-08-31

### Security

- **`/healthz` handed out the installation's configuration, to anybody, now.**
  `main.py` copies the webhook sub-app's routes into the main app; the sub-app
  declares its own detailed `/healthz`, and a copied route lands ahead of the
  plain one declared below — first match wins. `middleware.py` exempts the path
  from authentication and Caddy proxies `/backend/*`, so the public answer
  carried every model name, deadline, budget, the cache size and which backends
  are configured. Observed answering exactly that on production.

  The route is no longer copied. The reason the block existed is real and is
  kept: `env_file` points at a `.env` that does not exist in the container, so
  a setting the compose file does not forward silently takes the code default
  and nothing outside could tell which had happened. It is served from
  `/api/ops/review-settings`, behind an admin.

- **`/readyz` and `/metrics` were public by the same route.** `/readyz`
  returned per-dependency detail including a user count and `str(exc)[:200]`
  from a failed connection — for a bad DSN, a fragment of the DSN. It now
  answers `{"ok": …}` and the status code, which is a probe's whole contract;
  the detail is at `/api/ops/readyz` behind an admin.

  `/metrics` returned the entire Prometheus slice — queue depths, spend, review
  counts, error rates. It now needs `CELMIS_METRICS_TOKEN` when the request
  passed a proxy, and is served unconditionally when it did not: the bundled
  Prometheus scrapes `api:8000/metrics` directly over the compose network and
  keeps working. A caller can add `X-Forwarded-For` and lose access; they
  cannot remove Caddy's, so the public path fails closed.

## [0.1.22] — 2026-08-31

### Added

- **A finding now says whether your own code names the package.** A findings
  list is hundreds of rows and every one reads the same, so a direct dependency
  the service imports on its hot path and a transitive package pulled in four
  levels down by a build tool looked identical to whoever had to triage them.
  `named_in_code` carries an import-position answer with up to five
  `file:line` sites.

  **Three states, and the third is the point.** `imported`, `not_found`, and
  `unknown` — because a package name does not determine its module name.
  `beautifulsoup4` imports as `bs4`, `pillow` as `PIL`; reporting those as "not
  imported" would be the silent zero this subsystem is built to refuse. The
  rule is exact for npm, Go and crates.io, so absence means something there;
  for PyPI it is not, and the answer says so.

  **This is not reachability, and nothing in it is named as though it were.**
  Reachability would need the dependency's own source in the index — excluded
  on purpose — advisories that name the vulnerable symbol, which OSV carries
  for a small minority, and a notion of where execution starts, which does not
  exist here. A test fails the build if any identifier in the feature is
  called `reachable`, because a column name reaches an API response and
  eventually a filing, where nobody reads the docstring that qualified it.

### Fixed

- **Four caps bounded a dependency audit in silence.** Twenty lock files, four
  thousand entries per lock file, forty manifests, six hundred transitive
  candidates — every one a plain slice or `break`, with nothing logged and
  nothing recorded. A monorepo with twenty-five lock files produced an SBOM
  missing five, and the evidence pack built from it read as a complete
  inventory. The caps themselves are right; an audit has to end. Not saying
  they bit is what turned a bounded read into a false statement about what is
  installed — and `document.py` already writes the rule down: "count of what
  was dropped is printed rather than silently truncated".

  Each now logs what it dropped and records it, and the run summary carries
  `truncated` per repository so the SBOM and the pack can say the list was
  short. A test asserts the caps still bound the work, because lifting them
  would turn a warning into an audit that never finishes.

- **Two texts still promised verification without trust.** The README
  scenarios table and `home.html` were missed when the evidence-pack claim was
  corrected, and the site build reads README — so the old promise was live on
  the published docs page.

## [0.1.21] — 2026-08-31

### Security

- **The evidence pack's hashes prove consistency, not authenticity — and the
  product said otherwise.** `MANIFEST.json` records a sha256 for every other
  file and none for itself, because a file cannot contain its own hash. So
  anyone who opens the zip, edits a file and rewrites that file's entry in the
  manifest passes verification. Demonstrated against a real production pack:
  the edited archive verified as `OK` and exited 0.

  README, the dependencies page and the onboarding tour all said a third party
  could "check nothing was edited afterwards without trusting us". That is
  false of an unsigned manifest that does not hash itself, and it is the
  sentence a CRA filing would lean on.

  The fix is the manifest's own hash, obtained from somewhere the sender does
  not control. `GET /api/deps/{run_id}/evidence` now returns it in
  `X-Celmis-Manifest-SHA256` and logs it; `celmis verify --manifest-sha256
  <hex>` checks against it; and every text now says which of the two things it
  establishes. A malformed hash is a usage error (exit 2), not a mismatch
  (exit 1) — losing characters to a line wrap must not read as tampering.

## [0.1.20] — 2026-08-31

### Added

- **`celmis` is on PyPI, and the pack now says so.** Published 0.1.0 and
  verified from the live index: installed on Python 3.9.25 in a clean
  container, all five commands run against a real production pack, one flipped
  bit reported as `findings.json: sha256 mismatch` with exit 1, a missing file
  with exit 2. `summary.md` inside every pack said "recompute them and
  compare", which was true and had no executable form; it now names
  `pip install celmis` and `celmis verify`, and says in the same breath that
  you are not required to use it — the manifest is plain JSON and sha256 is
  sha256. A tool that made checking us require trusting us would defeat the
  point of the pack.

- **`packaging/pypi/` — the evidence-pack verifier, as a standalone package.**
  `summary.md` inside every pack tells a third party they can check it without
  trusting the tool that made it, and until now the only way to reach
  `verify_pack()` was to clone an AGPL repository and install forty
  dependencies under Python 3.13. The claim was true and unexecutable. The
  verifier is a few hundred lines of `hashlib`, `io`, `json` and `zipfile`, so
  it lifts out with no dependencies at all and runs from Python 3.9 — which is
  where a locked-down audit box usually is. Two commands, `verify` and `show`,
  with exit codes an auditor's CI can read: 0 intact, 1 problems found, 2 could
  not check.

  A fixture pack is committed in both trees and a drift test builds a fresh
  pack from `build_evidence_pack()` and runs the *packaged* verifier over it.
  Without it, the day somebody adds a file to the producer every installed
  `celmis verify` starts reporting `present but not in the manifest` — a false
  accusation of tampering, produced by our own tooling, against the operator.

### Changed

- **The platform's distribution is `celmis-platform`.** `celmis` on PyPI is the
  verifier above, and while both were called the same thing two failures were
  one command away: `python -m build` from the repository root would publish
  the entire server under the verifier's name, and in a shared environment
  `importlib.metadata.version("celmis")` would answer with the verifier's
  version — which `src/vault/provenance.py` stamps into every generated
  document and `/api/capabilities` reports as the running platform. Both
  `DISTRIBUTIONS` tuples now ask for `celmis-platform` first, and the root
  carries a `Private :: Do Not Upload` classifier, which is not a real
  classifier and which PyPI therefore rejects — a hard stop rather than a note.

- **Two sentences kept the mark after the labels above them lost it.** The
  navigation entry and the tour heading were renamed to "Agent"; the subtitle
  and the tour's own description still said "Dispatch a bug straight to the
  Claude agent" and "Claude agent: code-editing sessions on your repos". A scan
  for "Claude agent" found seven locales of sixteen — the other nine inflect it
  ("agentovi Claude", "à l'agent Claude", "al agente de Claude"), which is why
  the first pass missed them. All thirty-two strings are fixed, and the guard
  now covers the keys that name a section of ours, not only the button.

  Naming Anthropic's product is untouched: "Claude Code agent — researches the
  code" is an engine the operator picks between, and erasing the mark there
  would leave them guessing what they chose.

## [0.1.19] — 2026-08-30

### Fixed

- **A refusal of ours was blamed on Anthropic.** Every `TokenRejected` reached
  the caller as "Anthropic rejected that token: …", including the new rule that
  a shared slot may not hold a subscription. That reads as a claim about the
  operator's Anthropic account and sends them to look for a problem that is not
  there. The refusal now carries who made it; a real provider verdict still
  says so, because that one *is* the cue to go and check.

### Added

- **The evidence pack declares its format version.** The manifest carried
  `product`, `generated_at`, `run_id`, `algorithm` and `files` — and nothing
  about the shape of the thing. A verifier meeting a newer pack had no way to
  tell "I do not understand this" from "this has been altered", and would have
  reported the first as the second: an accusation of tampering aimed at
  whoever produced a perfectly good pack. `verify_pack` now answers with
  "upgrade the verifier" instead, reads a missing field as version 1 so packs
  made before today keep verifying, and still names the changed file when one
  really has changed.

## [0.1.18] — 2026-08-30

### Security

- **A workspace could share one person's Claude subscription.** The shared slot
  accepted the `sk-ant-oat…` token from `claude setup-token`, and
  `resolve_connection` fell back to it for any member without one — so one
  person's plan ran everybody else's sessions. Anthropic's terms name it
  directly: "Customers may not pay for, resell, or intermediate Claude usage on
  their end users' behalf. Each end user must authenticate with their own
  Anthropic API key, Claude subscription plan credentials, or 3P inference
  provider credential." The UI carried a warning about this and saved it
  anyway; a warning is not a control.

  The slot survives and now takes an **API key**, which the same terms permit —
  "configuring an API key in a development environment, secrets manager, or
  machine image for use by the customer's own authorized users" — because the
  bill lands on the key's owner. Refused at write time and again at read time,
  so an installation whose slot was filled before this rule stops using it
  rather than keeping it for ever.

### Added

- **An Anthropic API key works everywhere a subscription did.** Measured
  end to end against the CLI: a session driven by `ANTHROPIC_API_KEY` alone
  starts, edits the checkout and reports success. `ClaudeConnection` now
  carries which credential it holds and hands out the variable that belongs to
  it — the two are not interchangeable, since with both set the API key wins
  outright. A key is verified against `GET /v1/models`, which costs nothing and
  also tells the two credential types apart.

### Changed

- **The agent section is no longer named after somebody else's product.**
  "Claude agent" in the navigation and the tour is now just "Agent", in all
  sixteen languages. Saying the product runs Claude Code stays — that is
  accurate and permitted; naming your own section with the mark is not.

## [0.1.17] — 2026-08-30

### Fixed

- **Freshness asked the remote about a branch the clone was not on.** With no
  branch configured, `remote_head` fell through to the provider default while
  `RepoSync.clone_or_update` takes `branch="dev"` and `_advance_to_remote`
  resets onto whatever branch the checkout is standing on. A repository with a
  `dev` branch, added without naming one, was indexed from `dev` and compared
  against `main`: two shas that never converge, so it read as behind for ever
  and re-indexed daily to no effect. The check now asks the clone — the thing
  that was actually indexed — so the check and the advance name the same ref
  by construction.
- **`CELMIS_DEPLOYMENT_MODE` could not reach the process.** README documents it
  and `docker-compose.yml` never passed it; there is no `env_file`, so the
  container gets the variables its block names and no others. Confirmed on a
  running box: `env | grep CELMIS_DEPLOYMENT_MODE` returned nothing. Passed
  through now, empty by default, which `parse_mode` reads as `single_tenant` —
  so no existing installation changes.
- **The Hetzner proxy served the app and not `/backend`.** The published web
  image is built with an empty `NEXT_PUBLIC_API_BASE`, so its bundle calls the
  relative `/backend`; that overlay routed only the app on `APP_DOMAIN` and put
  the API on a second domain, so every API call from a browser 404'd on an
  otherwise healthy stack. The test that exists for this read every Caddyfile
  into one string and asked whether the prefix appeared anywhere, so one
  correct file covered for the one that was wrong. It checks each file now.
- **`docs/ORACLE_CICD.md` documented a pipeline that had been removed** — rsync
  and `compose up --build` on the box, and an Actions workflow that does not
  exist — and told the reader to set `API_HOST_PORT=127.0.0.1:8000`. Compose
  already writes the address in front of that variable, so the documented value
  produces `invalid IP address: 127.0.0.1:127.0.0.1` and the stack does not
  start.
- **The middleware docstring called webhooks exempt from rate limiting** after
  the exemption was narrowed to the three git provider routes, and called a
  fixed-window limiter sliding. Both matter to an operator: the first is how
  the alert ingest came to be exempt in the first place, and the second
  promises that a burst cannot cross a window boundary at twice the limit.
- **`.env.example` told a new installer to fill in four secrets by hand.**
  `init-env.sh` generates fourteen and asks for none of them.
- **`src/ops/build.py` described deploys as rsync + build on the server**, and
  said there was no image digest to read back — while the deploy script reads
  exactly that OCI label to stamp the version.
- **A compose comment promised a test that did not exist.**
  `test_a_setting_the_deployment_drops_is_not_settable` was named in
  `docker-compose.yml` as the thing that would catch a compose default
  overriding a settings field. It has been written: it compares every
  pass-through default against its field, and checks the other half too — that
  a setting the README documents actually reaches a container.

### Changed

- **The README no longer promises a web push on every alert.** Web push is
  real and two things send one — an agent turn finishing, and the test-send
  endpoint. The alert path sends none; what it does is fan out to the
  workspace's bound channels.

## [0.1.16] — 2026-08-30

### Fixed

- **The incremental re-index could not move a single production clone.**
  `RepoSync.clone_or_update` finishes with `_chmod_readonly` so analysers
  physically cannot edit the code: directories under the repository root become
  0550 and files 0440. Unlinking a file needs write on its *directory*, so
  `git reset --hard` died on the first path inside any subdirectory —
  `error: unable to unlink old 'src/contract.ts': Permission denied` — measured
  on a copy of a production clone. `_advance_to_remote` returned False, the
  pass recorded "unchanged", and the row carried `last_indexed_at = now` beside
  a stale `last_indexed_sha`: one column saying indexed-just-now and the next
  saying behind, re-queued daily, for ever. `RepoSync._pull` already brackets
  its own pull with `_chmod_writable`; this path did not, so it worked in a
  test whose clone was writable and never once where it shipped.

  The tests that were written to prove this path really pulls all pass with the
  bug reintroduced — they used a writable clone with only top-level files. The
  fixture now applies the project's own `_chmod_readonly` and keeps a file in a
  subdirectory, which is where the mode bites.
- **A fetch that failed was reported as a successful advance.** The fetch ran
  with `check=False`, so an unreachable remote or an expired token left
  `origin/<branch>` at whatever it last said, and the reset then "succeeded"
  onto that stale ref and returned True — the confusion between "could not
  update" and "nothing to update" that this function's own docstring says it
  exists to end.

## [0.1.15] — 2026-08-30

### Security

- **The OAuth consent screen echoed the query string into the page.**
  `_render_consent` builds HTML with an f-string. `client_id`, `redirect_uri`
  and `scope` are checked against the registered client first, but `state` and
  `code_challenge` are not checked against anything — they cannot be, they are
  the caller's own opaque data — and both land inside `value="..."`. Measured
  against a running box, `state='"><b>PWNED</b>'` produced a live element on
  the API's origin, which is the web app's origin too, so a script there runs
  as the signed-in operator.

  The precondition is self-serve: signup is open, a new account owns its
  personal workspace, `require_workspace_admin` accepts an owner, and
  `POST /oauth/register` then hands out a `client_id` with attacker-chosen
  `redirect_uris` and `name`. `client_name` is rendered too, so the same
  payload also works with no query string at all. Every value is escaped now.

### Fixed

- **You could register an OAuth client and then never see or revoke it.**
  Registration takes a workspace admin — deliberately, "registering one grants
  no authority the registrant does not already have" — while listing and
  deleting took a platform admin. The argument that lets you make a credential
  is the same one that lets you unmake it. Both now take a workspace admin and
  narrow by ownership: platform admins still see and delete everything, and
  everyone else sees and deletes their own, which is strictly more than the
  403 they used to get.
- **A missing setting in `.env` ended a deploy after the containers had
  swapped.** `deploy-on-server.sh` read the sandbox subnet with a `grep`
  pipeline, and grep exits 1 when the line is simply absent. Under
  `set -euo pipefail` that ends the script — three lines below the comment
  promising a deploy must not stop over a hardening rule, and one line above
  the default written for exactly this case, which was never reached. It
  aborted after `up -d`: new containers running, the version never stamped,
  the health check never run.

## [0.1.14] — 2026-08-29

### Security

- **The sender of an alert chose where its Open button pointed.** The link on
  an alert card was built from the incoming request's `Host`, and that request
  comes from somebody else's monitoring — the sender writes that header, and
  the reverse proxy passes it through (Caddy overwrites `X-Forwarded-Host`, not
  `Host`). Measured against a running box: an alert POSTed with
  `Host: evil2.example.test` was delivered into the workspace's chat room as a
  card carrying this product's branding, a title and body the sender wrote, and
  an Open button on `http://evil2.example.test/alerts`. The only requirement is
  the ingest token, which is handed to third-party monitoring on purpose.

  The address now comes from `PUBLIC_BASE_URL` — the only party in that
  exchange who is not the sender — and unset means no button rather than a
  guessed one. Deriving a URL from the request stays correct where it goes back
  to whoever made it, which is what the webhook-setup page does and why it is
  untouched. What decides it is not the header, it is who receives the URL.

## [0.1.13] — 2026-08-29

### Fixed

- **An alert's link was correct and would not open.** Google Chat refuses a
  plain-http link to a bare IP address: the card's Open button renders inert,
  and following it reports the site as unavailable — while
  `http://<ip>/alerts` was serving a redirect to the login page the whole
  time. Reached by IP is how an installation works before somebody puts a
  hostname in front of it. The address now goes into the card as text as well,
  which no link policy can switch off, so it can be copied when the button
  will not move.

## [0.1.12] — 2026-08-29

### Fixed

- **A published image named a commit that was not inside it.** The release
  checks out `inputs.tag`, but labelled the images with `github.sha` — the head
  of the branch the run was started from. `deploy-on-server.sh` reads that label
  back as the version the API reports, so v0.1.11 announced itself in production
  as `0.1.11+f2a2b39` while running the tree at `c2a5e48`. One CI-only commit
  apart this time; the mechanism can be off by anything. The AGPL §13 footer
  offers source *at that revision*, which is the one thing it exists to get
  right. The label now comes from the checkout.
- **The release job went red after the release succeeded.** Its last line asked
  `gh release view --json tagName,isLatest`; `isLatest` is a field of `release
  list`, not of `release view`, so gh printed the valid names and exited 1 —
  after all three images had published and `latest` had moved. The line was
  decorative. It now checks the thing the step is for: that `/releases/latest`
  resolves to this tag when we asked for it.

## [0.1.11] — 2026-08-29

### Security

- **The observability overlay published to every interface.** Prometheus,
  Loki and Grafana had bare `PORT:PORT` mappings, and a mapping with no
  address binds `0.0.0.0` — so switching on monitoring opened three ports,
  including Loki, which has no authentication of its own and accepted
  `POST /loki/api/v1/push` from anyone who could reach it. All three are on
  `127.0.0.1` now, reached over `ssh -L`, which is what docker-compose.yml has
  always done for postgres and says why in its own comment.
- **Grafana no longer defaults to `admin`/`admin`.** The overlay refuses to
  start without `GRAFANA_ADMIN_PASSWORD`; `init-env.sh` generates it.
- **Alert bodies are redacted before they are stored, dispatched or sent to a
  model.** An alert is somebody else's text about a failure, which is where a
  connection string or an Authorization header turns up. The redactor existed
  and this path did not call it. Fail-closed on the secret, not on the alert:
  a redactor that breaks costs the text, never the alarm.
- **Incoming alerts have a retention window** — 90 days by default, settable
  with `CELMIS_ALERT_RETENTION_DAYS`, purged on the nightly loop. There was no
  DELETE and no sweep, so a transient leak was a permanent one and an erasure
  request had no answer.
- **The alert ingest is rate-limited.** `/webhook/` was exempt because "HMAC +
  dedup already guard these", which is true of the git webhooks and false of
  `/webhook/alerts/{token}` — it has a compared token, not a signature over
  the body, and no dedup. The exemption names the three provider routes it was
  reasoned about.

## [0.1.10] — 2026-08-27

### Added

- **The index notices when the branch moves.** Until now "Ask the code"
  answered from whatever was indexed the last time somebody pressed a button,
  and said nothing about it — a repository indexed on Tuesday answered
  Friday's questions from Tuesday's code. Three ways in, one decision: a daily
  sweep (`git ls-remote` per repository, no clone, no model call), a `push`
  webhook, and a **Check** button on each repository. All three call the same
  `check_repo`, so a schedule, a provider and a person cannot disagree about
  what "current" means.
- **The repositories list says when the remote was last asked**, and what it
  answered: "No new changes · checked 2 h ago", "New commits on the branch —
  re-indexing", or "Could not reach the remote". Four states, not two — a
  check that could not reach the remote must never render as "no new changes",
  so `up_to_date` is `true`/`false`/**null** and null renders as "never
  checked", never as green.
- `CELMIS_REFRESH_INTERVAL_HOURS` (default 24; `0` disables the sweep),
  `CELMIS_REFRESH_STAGGER_SECONDS`, `CELMIS_REFRESH_FIRST_DELAY_SECONDS`.
- `POST /api/repos/{slug}/check-freshness`.

### Fixed

- **The incremental indexer never actually pulled.** `run_index` ran
  `git fetch --all` under a comment saying it pulled; fetch advances
  `origin/<branch>` and leaves HEAD, the index and the working tree where they
  were. So it compared the old local commit against itself, recorded
  "unchanged", and returned a no-op for a repository whose remote had moved —
  with the pushed files not even on disk. It survived because nothing called
  it: every enqueuer used the full path. This is what made the whole feature
  above possible.
- **A `git ls-remote <url> main` glob could answer about the wrong branch.** A
  repository that also has `feature/main` gets both, sorted, with
  `feature/main` first. Refs are fully qualified and matched by name now.
- **Bitbucket freshness checks could never have authenticated.** The username
  slot depends on the token type, not the host, and the stored username for a
  legacy app password was being dropped; both now come from `git_auth_kwargs`.
- **A typo in a scheduler setting killed the sweep permanently.** Two of the
  three settings were parsed with a bare `float()` inside the task, before its
  loop.

## [0.1.9] — 2026-08-27

### Security

- **The agent's git push carried its token in the command line.** The module's
  own "Credential hygiene" paragraph said the token arrived "via a one-shot env
  askpass"; no askpass in this repository supplies a token — `GIT_ASKPASS:
  "echo"` suppresses a prompt, it does not answer one. The credential was
  inside the push URL, passed as an argument, and therefore in `ps auxww` for
  the duration of the push. It now reaches git through a credential helper
  reading the environment, and the URL git is handed carries none.
- **The inherited credential-helper list is cleared first.** `-c
  credential.helper=<x>` appends rather than replaces, so a helper configured
  in the environment answers before ours and can hand git a different
  account's credential — a failure that reads as `Permission … denied` on an
  account with admin rights.

### Fixed

- **`:latest` moved to whatever tag was built last.** Rebuilding an old
  release silently repointed the tag that a first-time `docker compose up`
  pulls, with every log line green. It now moves only for the newest release,
  decided by a script the tests can run rather than a YAML expression they
  cannot. A tree that predates that script — any older tag being rebuilt —
  answers "no" instead of failing on a missing file.

## [0.1.8] — 2026-08-27

This entry exists because the sentence that used to stand here — "Nothing has
been tagged or released. The version has read `0.1.0` since the root commit" —
was false in every clause by the time anyone read it. Eight tags were public
and the file said none were. A changelog that reports no releases is worse
than an empty one: it answers the question, and answers it wrongly.

### Fixed

- **The version literal caught up with the tags, and cannot fall behind
  again.** `src/__init__.py` holds the release number as a literal on purpose
  — it is readable without installing the package, which is why
  `pyproject.toml` points at it — but a literal is exactly what drifts. Tags
  `v0.1.1` through `v0.1.7` were cut without touching it, so an install of
  `v0.1.7` answered `/api/capabilities` with `0.1.0`. Nothing broke visibly:
  the AGPL footer builds its source link from the sha after the `+`, and that
  sha was always right, so the offer of source kept resolving while the number
  in front of it was false. A wrong number that hurts nothing is the kind that
  survives.

### Added

- **The release workflow refuses to build a tag that disagrees with the
  literal**, checked before buildx starts, so a mismatch costs seconds rather
  than three multi-arch images. It is the only place that can make this
  comparison — a test cannot see a tag that does not exist yet.
- **A test pins the byte format that guard depends on.** The guard reads the
  literal with `sed`; Python reads it by import. Five ordinary edits — single
  quotes, a missing space, a doubled space, a trailing comment, CRLF endings —
  keep every other test green while making the two disagree, and since
  `ci.yml` does not run on tags the first signal would be a failed release.
  The test extracts the `sed` program out of the workflow and compares its
  output against the imported value, so editing either side alone goes red.

## [0.1.7] — 2026-08-27

### Fixed

- **The execution sandbox could call the API and reach the host it runs on.**
  Measured from inside it, as the code under review would: `api:8000` answered
  `/healthz` with the review configuration, and `172.17.0.1:22` — the docker
  host — was open. `SandboxNetworkGuard` now refuses anything whose peer
  address is on the sandbox subnet, before routing, because the route it
  closes is the one with no dependencies to hang a check on. The deploy script
  drops sandbox→host traffic at the firewall, which is the only layer that can
  tell the host apart from the internet it routes for. Outbound internet stays
  open on purpose: `pip install` and `npm ci` need it.
- **`max_attempts=5` ran a job six times.** `claim()` returned the attempt
  count from before it incremented, so the fifth run reported four and retried.
  The same stale number logged the first attempt as `attempt=0` and made the
  first backoff half the base delay. For a review job the extra run was an
  extra billed model call on work already failing.
- **The sandbox was unreachable on every install.** `.env.example` set
  `SANDBOX_URL=http://sandbox:8080`; the sandbox listens on `8900`. An existing
  test asserted the port against `docker-compose.yml`, which is not the file
  anybody edits.

## [0.1.6] — 2026-08-27

### Fixed

- **MCP over HTTP answered `421 Invalid Host header` on every install**, naming
  neither the host it rejected nor the setting that would admit it. The guard
  is correct — refusing an undeclared host is what DNS-rebinding protection is
  for — so the refusal now prints the arriving host and the exact line to add.
  It hides behind the `401`: without a valid token the same request reports an
  authentication failure, so the real cause only surfaces once the token works.

## [0.1.5] — 2026-08-27

### Fixed

- **`/alerts` still said alerts are not forwarded**, one commit after they
  were. Copy describing an absent behaviour outlives the absence. Corrected in
  all 16 locales.

## [0.1.4] — 2026-08-26

### Fixed

- **Ingested alerts were stored and dispatched nowhere.**
  `POST /webhook/alerts/{token}` wrote a row and returned; `severity` and
  `repo_hint` were being stored for a routing step nobody took. Dispatch now
  happens after the response, because the sender retries on anything but a
  `2xx`.
- **The event dropdown offered three events nothing emits** —
  `compliance_failed`, `deprecation_used`, `apply_fix_applied` — and hid one
  that does, `agent_turn_done`. A binding on a phantom event looks configured
  and is silent for ever.
- **A fresh install sent every logged-out visitor to `http://localhost:3000`.**
  `.env.example` shipped `NEXTAUTH_URL=http://localhost:3000` and `init-env.sh`
  copies it verbatim, overriding the `trustHost` setting that exists precisely
  so a self-hosted app derives its address from the request.

## [0.1.3] — 2026-08-26

### Fixed

- **Testing a notification channel returned the webhook it was testing.** A
  Google Chat webhook URL carries `key` and `token` in its query string — the
  URL *is* the credential — and `httpx` puts the request URL in its error
  string, which the endpoint returned verbatim.
- **A channel's kind is checked against its URL's host.** A Google Chat URL
  saved as `slack` failed only at the first send, and until somebody pressed
  Test the sole symptom was alerts quietly not arriving.

## [0.1.2] — 2026-08-26

### Fixed

- **A bare `owner/name` was qualified for parsing and stored raw**, so the slug
  said GitHub while the clone said Bitbucket. Half a fix is the same defect
  wearing the fix's name.

## [0.1.1] — 2026-08-26

### Fixed

- **The repository form recommended a spelling that meant a different
  provider.** `owner/name` parsed as Bitbucket while the placeholder above it
  showed a GitHub URL; a bare name is now resolved against the connected
  provider when exactly one is connected.

## [0.1.0] — 2026-08-26

First tagged release, and the first published images
(`ghcr.io/celmis-labs/celmis-{api,web,sandbox}`, `linux/amd64` and
`linux/arm64`). The entries below this line describe changes made after the
19 August rebuild and before that tag.

### Fixed

- **The container image builds on arm64.** The runtime stage fetched
  `osv-scanner_linux_amd64` by name, so on the arm64 hosts both deployment
  guides recommend — `docs/HETZNER.md` picks a Hetzner CAX21, `docs/ORACLE_CICD.md`
  builds natively on an Oracle Ampere A1 — the download succeeded, the checksum
  matched, and the `--version` gate immediately after it failed with `exec
  format error`. The documented setup could not build at all. The artifact name
  and its expected digest are now selected from BuildKit's `TARGETARCH`.

### Changed

- **Pinned binaries are verified per architecture, and an unknown architecture
  aborts the build.** `amd64` and `arm64` each carry their own SHA-256; any
  other `TARGETARCH` — including the empty value the legacy non-BuildKit builder
  supplies — stops the build with a message naming the platform, instead of
  falling through to amd64. Guessing is what produced an image that could not
  run its own scanner.

- **`uv` is pinned to 0.12.5 and installed from a checksummed release
  artifact.** The builder previously ran
  `curl -LsSf https://astral.sh/uv/install.sh | sh`, which was unpinned (each
  rebuild silently adopted whatever uv was current that day) and unverified
  (the build executed whatever that URL returned). The second half is the
  same finding this project's own dependency scanner raises against npm
  packages that run install scripts; it is not a rule we can enforce outward
  and not inward. Digests are Astral's published `.sha256` files, checked
  against a local download of both tarballs.

- **One version, one place.** `0.1.0` was written independently in
  `pyproject.toml`, `src/__init__.py`, and two FastAPI constructors.
  `pyproject.toml` now declares `dynamic = ["version"]` and reads
  `src.__version__`, which setuptools resolves by parsing the file rather than
  importing it. `src/__init__.py` was chosen as the source because it is the
  copy readable without an install — `importlib.metadata.version()` answers
  only after one, and returns `"unknown"` otherwise, which is how every
  generated document once ended up stamped `version: unknown`
  (`src/vault/provenance.py`).

  The resulting value is unchanged: `0.1.0` before and after. Distribution
  metadata still carries it, so the vault's provenance stamp keeps working.

### Added

- This file.

### Known

- Two FastAPI applications still hardcode the version string in their OpenAPI
  documents: `src/api/main.py` (`Celmis API`) and `src/review/webhook.py`
  (`code-analyzer review webhook`). Both need `from src import __version__`;
  neither was changed here, because `src/` was outside this change's scope.
- `web/package.json` carries its own `"version": "0.1.0"`. It is an npm
  package version for a private Next.js app and is deliberately not coupled to
  the Python distribution — coupling them needs a build step, and nothing reads
  it today.
- `docker-compose.yml` still passes `CLAUDE_CODE_VERSION` as a build arg to the
  `api` service. The `ARG` it fed was removed when the Claude Code CLI install
  was dropped from the image, so BuildKit now warns that the argument is
  unconsumed. Harmless, but it is a stale line.
