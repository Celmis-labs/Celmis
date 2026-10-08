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

## [2.3.9] — 2026-10-08

### Review

- **A file that parsed but declares nothing is no longer a graph gap.** Entry
  scripts, templates and modules without declarations of their own produced
  `⚙ ADJUSTED — graph context partial … the index is stale there` on every run,
  and re-indexing could not clear it. The check now counts the per-file marker
  the indexer already writes, so only files the index really lacks are named.
  Indexes built before the marker existed keep the old behaviour.
- **A near-empty agent reply is said on the run.** When a model-backed agent
  answers a diff of 50+ changed lines with 3 output tokens or fewer on 3000+
  input tokens and no findings, the summary carries
  `⚙ ADJUSTED — <agents> gave a near-empty reply`, pointing at a model with
  reasoning or the per-agent `reasoning` setting. The verdict is unchanged.
  [`docs/REVIEW_SETTINGS.md`](docs/REVIEW_SETTINGS.md) explains the knob.

### Web

- The Repositories page and onboarding say Bitbucket reviews and re-indexes
  automatically once the webhook is installed, and manually otherwise.

## [2.3.8] — 2026-10-08

### Code from an archive, project file scope, project MCP tokens

- **A repository can be added from a `.zip`, `.tar.gz` or `.tgz` archive
  instead of a Git provider.** Only the superadmin can do it (Repositories →
  Archive, or `POST /api/repos/upload/sessions`). The browser sends the archive
  in numbered parts (`CELMIS_UPLOAD_CHUNK_BYTES`, default 32 MiB, always under
  100 MB) so uploads pass proxies and CDNs with a request-body limit; a session
  lives `CELMIS_UPLOAD_SESSION_TTL` (24 h) and can be resumed or aborted. The
  archive is capped by `CELMIS_MAX_UPLOAD_BYTES` (250 MB) and unpacks under
  limits on total size, file count and file size. Links, devices, absolute
  paths and `..` are refused. Uploading again under the same slug replaces the
  code and re-indexes it. Such repositories have no webhook, freshness or
  incremental sync. Single-request `POST /api/repos/upload` stays for small
  archives and requires `Content-Length`.
- **Files without a parser are searchable.** Q&A and project MCP search fall
  back to a plain-text, Unicode-aware search over the allowed files (BSL, XML
  configuration, plain text), skipping binaries, with result and time caps.
- **Each repository in a project has a file scope.** `include_globs` and
  `exclude_globs` (exclude wins) narrow what Q&A and project MCP tokens see.
  Only the superadmin edits them (`PATCH /api/projects/{id}/repos/{slug}`).
  While a project has a live MCP token, only the superadmin can add or remove
  its repositories, so the token cannot be widened behind its issuer's back.
- **Project MCP tokens.** The superadmin issues `cmcp_…` tokens bound to one
  project (`/api/projects/{id}/mcp-tokens`, or the MCP access card on the
  project page) with an expiry from 1 hour to 90 days. A token is shown once,
  stored as a hash, can be revoked, and carries `read:project_search`: it sees
  only `search_project` and `ask_project`, only that project's repositories,
  only inside their file scope. The card prints a ready
  `claude mcp add --transport http …` command with the right MCP URL, including
  an API path prefix when the API sits behind one. Guide:
  [`docs/archives-and-project-tokens.md`](docs/archives-and-project-tokens.md).
- **Code review: the pull-request picker is searchable.** Filter open pull
  requests by number, title, branch or author; the list keeps a bounded height
  instead of filling the screen. Rate limits: `CELMIS_RL_UPLOAD`,
  `CELMIS_RL_UPLOAD_PART`.

### Security

- The web app moves to next 16.4.0 and next-auth 5.0.0-beta.32
  (@auth/core 0.41.3), and build-time dependencies reached through styled-jsx
  are lifted to fixed releases: `pnpm audit --prod` is clean.

### Upgrading

- Database migration `d4a7f1c8e3b2` runs on start (adds `project_repos`
  include/exclude globs and the `mcp_project_tokens` table).

## [2.3.7] — 2026-10-07

### BREAKING: repository access is closed by default, and MCP tokens are per person

Read this before upgrading. The full guide is [`docs/mcp-access.md`](docs/mcp-access.md).

- **A repository nobody has a rule for is now visible only to the workspace
  owner, its admins and the superadmin.** Until now a repository with no team
  grant and no access rule was readable by every member (in `single_tenant`
  through both the REST API and MCP, in `multi_tenant` through MCP). After the
  upgrade members stop seeing such repositories everywhere: the repository
  list, projects, issues, dependency runs, chats, search, and all MCP tools.
  - To give a team its old access in one step:
    `analyzer access bootstrap --team <team> --visibility code --all-unruled`
    (without `--all-unruled` it only lists what is affected).
  - To restore the old behaviour as a stopgap on a single-tenant install, set
    `CELMIS_UNRULED_REPO_ACCESS=open`. It is ignored, with a warning, in
    `multi_tenant`. `GET /api/access/unruled` and a banner on the Team pages
    say how many repositories are affected.
- **A repository the caller may not read is answered like one that does not
  exist.** MCP tools no longer return `blocked_repos`, `access_notice`,
  `hidden_symbol_count` or any note naming a hidden repository, and the REST
  routes answer 404 where they used to answer 403 for a repository the caller
  cannot see. `/api/access/my` and `/api/access/rules` list only what the caller
  may read.
- **MCP tokens are issued by the superadmin, per person, with an explicit
  repository list.** `POST /api/admin/mcp-tokens` takes the person, the
  workspace, slugs and/or globs, an expiry (capped by
  `CELMIS_MCP_TOKEN_MAX_DAYS`, default 90) and `allow_write` (default false).
  The token is shown once. The list can be changed (PATCH) or the token revoked
  without reissuing; a change applies within 30 seconds. The new page is
  Administration, MCP tokens.
  - `POST /api/mcp/token` (self-service) now answers 403 unless
    `CELMIS_MCP_SELF_SERVICE=true`. When on, the token carries `*` and is still
    limited to what the person can see.
  - Tokens issued before this release carry no grant and are refused with 403 and
    a reason (an expired, forged or unknown token still gets 401).
    `CELMIS_MCP_LEGACY_TOKENS=accept` accepts them, read-only, for a
    transition; reissue them instead.
  - `analyzer mcp issue-token` now requires `--user`, `--repos` and
    `--workspace` (it used to take `--subject` and `--scopes`).
  - OAuth keeps working, but only for a person who holds an `oauth_grant`
    (`POST /api/admin/mcp-grants`); consent refuses without one, every OAuth
    token is limited to that grant's repositories, and refresh fails once the
    grant is gone. There is no dynamic client registration.
- **Every MCP call is audited**: who, token id, tool, repositories touched,
  result size and a hash of the arguments. Argument values and results are never
  stored. Read it at `GET /api/admin/mcp-calls` (CSV with `?format=csv`) or on
  the Calls tab. Retention is `CELMIS_MCP_AUDIT_RETENTION_DAYS` (default 180).
- Two migrations, `a7c41e9b2d10` (`mcp_tokens`) and `a7c41e9b2d11`
  (`mcp_call_log`), sit in the same chain as the review-feature revisions.
  Rolling back the code needs no downgrade.
- **Review fixes to the above.**
  - The in-app agent, the claude-engine review and documentation generation
    reach `/mcp/` with a short-lived `internal` grant (a row in `mcp_tokens`,
    read-only, never listed, bound to the workspace and the acting person).
    They had lost their Celmis tools because the verifier refuses a token with
    no row; the person's own access is still the ceiling.
  - The dependency tools (`get_dep_audit`, `list_dep_findings`, `audit_delta`),
    `list_issues`, `update_issue`, `list_reviews`, `get_review_run` and
    `list_alerts` now obey a token's repository list even when the person's own
    access is wider, and default-deny when there is no list. A run summary is
    rebuilt from what the caller may see. `export_sbom` for a repo-limited token
    must name one listed repository.
  - `ask_code` no longer returns `blocked_repos` and the model is told only
    that "other repositories" exist: no slug a caller cannot read reaches the
    prompt, and retrieval uses the token's list.
  - A team's read grant no longer overrides a research rule written for another
    team; a grant is the fallback only when the repo has no rule. A grant with an
    unknown permission value grants nothing.
  - A call refused at the HTTP edge (revoked, expired, unknown, legacy,
    wrong-holder token) leaves a `mcp_call_log` row (`status=denied`, tool
    `(refused:<reason>)`, token id, no arguments), at most once a minute per
    token. The queue counts and warns about dropped rows and is flushed at exit;
    `analyzer mcp issue-token` and `analyzer access bootstrap` write audit
    actions; `args_hash` is keyed to the installation.
  - A grant can be narrowed, not widened: PATCH rejects a repo list on a
    self-service token, a later expiry, and switching write on (the signed token
    carries them); issue a new token instead.
  - Smaller: the roster's emails and job error lines are for workspace admins
    only, a project none of whose repositories the caller can read is not shown,
    `get_review` treats a run whose repo cannot be named as unknown in every
    mode, a dev-profile token cannot call the full profile's read tools by name,
    and the token issue response warns when an entry like `acme/shop` matches the
    same repository on two providers.
- **Memories and learning answer 404, not 403, for a repository the caller
  cannot read.** The review features follow the same rule as the rest: a
  repository without a rule for the caller is not there. An editor who used to
  see a 403 now sees the same answer as for a repository that does not exist.
  The role gates stay: memories are for editors and above, productivity, Jira
  task context and credentials for owners and admins.
- **Review features are limited to repositories the caller may read.** The issues
  backlog and its summary and recheck, memories, learning, productivity
  developer tables, review feedback, the PR commands list, chat replies,
  the Jira task context and the requirements check all resolve visibility with
  the same resolver as MCP. A caller sees nothing of a repository that is closed
  to them; a repository that is only named (`metadata`) is listed by name, never
  opened.
- **New webhook events: every GitHub, GitLab and Bitbucket hook installed
  before this release needs "Repair all webhooks".** A hook from an earlier
  version does not send what the comment commands, the feedback learning, the
  productivity history and the index refresh read: on GitHub
  `issue_comment`, `pull_request_review_comment`, `pull_request_review_thread` and
  `push`; on GitLab `note_events`; on Bitbucket `pullrequest:comment_created`,
  `pullrequest:comment_updated`, `pullrequest:approved`, `pullrequest:unapproved`
  and `repo:push`. Reviews keep working without the repair, but `@celmis`
  commands, thumbs and thread resolutions are silently ignored and the webhooks
  page lists the hook as outdated. The installer subscribes new hooks only.
- **MCP calls are limited by default**: 120 calls per minute per token, 4 running at once and 60 seconds
  per call, and a developer-profile call that fans out names at most 40
  repositories. A client that bursts, or a slow call, now gets an error where it
  used to get an answer. Raise `CELMIS_MCP_RATE_PER_MINUTE`,
  `CELMIS_MCP_MAX_CONCURRENT` or `CELMIS_MCP_CALL_TIMEOUT_SECONDS` (`0` switches the
  rate limit off); the compose file forwards all three.
- **`migrate_consumers` no longer takes `user_id`.** It acts as the caller. A
  client or script that passed `user_id` must drop it and use a token of a person
  who is owner or admin of the workspace.

### Added


- **Ask the reviewer a question in a pull request comment, and get the answer in the thread.**
  Any text after `@celmis` that is not a command word (`@celmis why is this a race?`) is
  answered by the model, with the review comment it replies to, the replies under it, the
  diff of the file it is anchored on (the whole change's digest for a comment elsewhere) and
  the team's memories in front of it. The answer is posted as a reply in the same thread (a
  child comment on Bitbucket, a review-thread reply on GitHub and GitLab, a quoting comment
  for a plain GitHub comment) and carries the chat marker, so the next push keeps it. The
  pull request's text, the thread and the code are fenced as data, and the answer is
  cleaned before posting: no markers of ours, no images, no raw HTML, no live @-mentions.
  A bare "thanks" is a reaction, not a model call. Spend is booked under its own `pr_chat`
  surface on the Usage page; a workspace over its hard-stop budget gets one sentence. New
  knobs `REVIEW_CHAT_TIMEOUT_SECONDS` (90), `REVIEW_CHAT_MAX_CONTEXT_CHARS` (60000) and
  `REVIEW_CHAT_MAX_REPLY_CHARS` (6000); the per-repository "Answer questions" switch was
  already in Commands. Providers gained `get_thread`.

- **`@celmis remember` teaches the reviewers from a pull request comment.**
  `@celmis remember: <rule>` stores a team memory for the repository,
  `--org` for the whole workspace, `--dir=src/api` (or `--dir` on a comment
  written on a line of a file) for one directory. The rule goes through
  `memories.remember`: a name on the trusted list is active at once, anybody
  else's waits for approval on the Memories page, and the thread is told which
  happened. The command is listed in the comment guide and in `help` now that
  it exists. A GitHub or GitLab commenter is trusted by login as well as by
  numeric id.
- **An incremental review still checks the earlier issues of the whole pull
  request.** The backlog check reads the files of the whole PR, not only of the
  new commits.

- **Incremental review: a push is reviewed by what is new in it.** A new setting
  `review_scope` (repo over workspace over built-in, in General): `incremental`
  (the default) reads only the commits added since the last complete, posted
  review; `full` reads the whole pull request every time. Whatever is doubtful
  falls back to the whole PR: a first review, a force-push that dropped the
  reviewed commit, a provider that cannot list the commits, an empty or
  unreadable increment, a person asking again with a command or the Review
  button. A push of only merge commits, or the same head again, is a quiet skip
  that leaves the PR's status and counters as they were. Changes pulled in from
  the target branch by a merge are not the author's and are not reviewed.
  Comments are anchored and hashed against the whole PR, so a finding in a file
  the push never touched stays open. Our inline comments on lines the push
  removed are resolved as outdated (a thread a person replied in stays open),
  a finding already posted within three lines is not posted twice, and the
  summary says how many commits it read. Review runs record `scope` and
  `scope_base_sha`; the run page shows a new "scope" stage. Works on GitHub,
  GitLab and Bitbucket.
  An earlier comment is resolved as outdated only when it was posted at the
  commit the push starts from, so its line number is in the same numbering as
  the lines the push removed; older comments stay open, and a comment is
  compared with a new finding where its code stands now, not where it was
  first written. A push of only files nobody reads (lockfiles, generated
  files, the repository's ignore globs) ends quietly instead of reading the
  whole pull request again. The same finding twice within three lines counts
  as one comment on purpose: the fingerprint is rule, file and title, not the
  line.
- **PR commands: `@celmis start-review`, `review --force` and `help` work from a comment.**
  Comment webhooks (GitHub `issue_comment` and `pull_request_review_comment`,
  GitLab `Note Hook`, Bitbucket `pullrequest:comment_created` and
  `comment_updated`) are verified exactly like the pull-request webhooks and
  bound to the workspace of the registered repository. A comment becomes a
  command only when it starts a token with the bot handle (`@celmis` or the
  `/celmis` alias, from `REVIEW_BOT_HANDLE`); fenced code, inline code and
  quoted lines are never read, and a command word followed by prose is a
  question, not a command. `start-review` resumes a paused pull request and
  reviews the latest commit; `review --force` reviews everything again and
  skips the draft, title and cadence gates (never the size and enabled gates).
  Every command is recorded in a ledger (`pr_command_events`), which makes a
  redelivery or an edited comment run once, drives two rate limits (replies
  per pull request and commands per person, per hour; a refusal is announced
  once per window; refused commands from strangers do not use the pull
  request's budget, and forced reviews have their own small one) and feeds a "Comment commands" timeline on the
  pull-requests page. Who may command is a setting, `command_permission`
  (anyone with repository access, participants only, or anyone), next to
  `commands_enabled` and `chat_enabled`; all three inherit repository over
  workspace over built-in and have a new "Commands" section in the review
  settings. The bot never answers its own comments. Replies carry a chat
  marker, so the next push's cleanup does not delete them. The commands guide
  in the review summary is now on by default and lists only commands that are
  installed. Webhooks installed before this change do not deliver comments:
  the repositories page marks them "needs repair" and offers one button
  (`POST /api/repos/webhooks/repair-outdated`, admin only) that re-subscribes
  them all. `scripts/pr_commands_smoke.py` sends a signed synthetic comment to
  a running instance. Migration `a9d4f6b8c034`.
- **Learning from feedback: what the team dismissed is not posted again.**
  Every verdict a person gives a finding becomes a signal in an append-only
  table keyed by the finding's fingerprint, which does not depend on the pull
  request or the line: a reply (a thumb, `@celmis` plus a keyword, or free text
  read by one cheap model call), a reaction polled at the next review of the
  same pull request, a resolved thread (a weak signal, upgraded when the fix
  follows), the verdict on the reviews page, and what the issues ledger sees
  (fixed, or merged unfixed). A new `learned_filter` stage between the
  prefilter and the model's veto compares each finding with the repository's
  signals (same fingerprint; near-identical title in the same directory; or an
  embedding of 0.90 or more) and leaves out one that two people on two pull
  requests dismissed, or an exact repeat after one. Signals fade by a 90-day
  half-life, somebody who said "this was right" blocks the hiding, and a
  critical or `proven` finding is never hidden. Two review settings, repo over
  workspace over built-in, in a new Learning section: `learning_suppression`
  (`shadow`, the built-in, keeps every finding and reports what it would have
  left out; `on` leaves them out; `off`) and `learning_excluded_reviewers`
  (people whose feedback teaches nothing). A correction in a reply also offers
  a pending memory. `/memories` gains a Learning view (editor and above, only
  repositories one can read) with the counts, the most dismissed findings, the
  implementation rate and a way to forget a signal; the reviews page says how
  many findings feedback hid or would have hid. **Rules from history:** a
  "from history" job on the rules page, and an optional weekly tick
  (`REVIEW_LEARNING_RULES_SCHEDULE=weekly`), propose rules where several pull
  requests agree; they arrive pending with origin "learned". Signals and the
  comment map leave with their repository and the person on erasure.
  Provider adapters gained `list_comment_reactions`. Migration `c1f6b8d0e256`.
- **Replies and resolved threads reach the learning loop.** A reply in the thread
  of one of our findings is read as feedback before the comment-command path
  sees it, so `@celmis dismiss` or a thumbs-down teaches the reviewer instead of
  being answered as a chat question; a question in that thread (with or without
  the handle) goes on to the chat. A named command (`review`, `remember`) never
  does. Who may teach follows `command_permission`, checked only after the
  thread is known to be one of ours, so a stranger's reply costs no model call
  and ordinary conversation costs one lookup. GitHub hooks now also subscribe
  to `pull_request_review_thread` (a resolved thread is a weak signal, a
  reopened one withdraws it); existing hooks show "needs repair" until the
  repositories page's repair button is pressed. GitLab replies are matched by
  discussion id. The implementation rate on the Learning view is now the
  issues ledger's own (`implementation_stats` over pull requests merged in the
  window) and respects the repositories the reader may see.
- **Requirements check: the completed comment lists every acceptance criterion of the Jira task, and `@celmis -v business-logic <task>` checks one on demand.**
  A review that read a task now adds a "Requirements check" section to the
  summary comment, one row per acceptance criterion, marked met, partial,
  missing, contradicted or unclear, with the place in the diff that shows it.
  A finding of the business-logic agent that cites a criterion always wins;
  in `checklist` mode one short model call judges the rest, in `findings` mode
  nothing more is spent and the section says the criteria were not checked one
  by one, and `off` adds nothing. The mode is the inheritable setting
  `requirements_check_mode` (default `checklist`), set like every review
  setting at workspace, install and repository level, with the labels in all
  sixteen languages. The description gets a one-line link to the task, and the
  pull-requests page shows the task and its checklist above the review
  timeline (`GET /api/pull-requests/{id}/requirements`, readable by whoever
  may read the repository). The on-demand command runs only the business-logic
  agent, forced on, against a key, a link to the connected Jira site, or, when
  the repository sets `task_urls_enabled` (default off), a Confluence page of
  that same site; it answers in the thread, posts nothing to the code and
  records nothing on the pull request. Task text stays fenced as untrusted,
  links are https-only, and every failure is a plain sentence, never the
  provider's own words. The model sees the diff only through the redacting
  `code_context` channel, judges the whole pull request also after an
  incremental review, and the on-demand command honours `task_project_keys`
  (a page is refused while a project list is set). No migration.

- **Gates and cadence: a PR can be left alone, or paused when pushes pile up.**
  Four review settings, repo over workspace over built-in, in General:
  `ignored_title_keywords` (a title containing one, in any case, is not reviewed
  automatically: WIP, Revert, `[skip review]`), `review_cadence` (`automatic`
  reviews every push, `auto_pause` pauses a PR after `auto_pause_pushes` pushes
  (3) inside `auto_pause_window_minutes` (15), `manual` reviews only on
  request). A paused PR gets one note saying how to continue, in the repository's
  review language, and is resumed from the Resume button on the pull-requests
  page (which reviews everything pushed meanwhile); there is a Pause button too.
  The push that reaches the limit is itself the first one skipped. A person's
  request (comment command, Review button, CLI, MCP) skips the draft, title and
  cadence gates; `force` (`--force`, `force` on the trigger route) also skips the
  target-branch rule. One `ReviewRequest` (trigger, force, scope, resume) now
  carries that through the webhook, the poller, the queue and the orchestrator.
  New run stages `gate_title` and `gate_cadence`; the draft gate now runs before
  the repository context is built, so a draft costs no context. Migration
  `e7b2d4f6a812` adds the four columns to both policy tables and the pause state
  to `review_pull_requests`, and gives every PR whose last review was complete a
  baseline commit. A state that cannot be read never blocks a review. A pause
  note that could not be posted is tried again on the next delivery; a pause the
  cadence no longer asks for is forgotten; removing "WIP" from a title brings the
  pull request back for review (GitHub title edits, GitLab polling).
- **Auto-review shows which target branches it reviews.** The "Auto-review PRs"
  card on Reviews says under each repository "→ develop" (or "→ all
  branches"), where that comes from (repo, workspace or built-in default) and
  links to the repository's settings; "Connect auto-review" says per provider
  which branches deliveries are reviewed for and how many repositories override
  it. `GET /api/repos` carries `target_branches` and `target_branches_source`,
  resolved by the same repo > workspace > install resolver the orchestrator uses.

- **A compact, read-only MCP profile for coding assistants at `/mcp/dev`.** Nine
  tools (`repos`, `find`, `outline`, `read_symbol`, `refs`, `grep`, `map`, `ask`,
  `howto`) that answer in plain text, not JSON:
  the whole tool list is under 6,000 characters, every description is at most 200,
  and there is no output schema. Every answer starts with
  `idx: repo branch@sha age fresh|STALE|unknown`, so line numbers always say what
  revision they refer to; every list has a token budget, a stateless cursor and a
  "narrow with" hint. `find` ranks exact, prefix, token, substring and fuzzy
  matches, puts tests and vendored copies last, uses how often a symbol is called,
  and never lets one repository fill the page. `read_symbol` prints exactly the
  lines of one symbol from the indexed commit, with the middle elided past a
  limit; `grep` reads committed text only, never secret files, and names the
  enclosing symbol. The scope is `read:code`; a token without it is refused at the
  transport and again inside each tool. A repository the caller may not read looks
  exactly like one that does not exist, and all body text goes through one
  redaction hook.
- **A push to Bitbucket re-indexes the repository.** The hook now subscribes to
  `repo:push`, and a push to a branch goes to the same freshness check the daily
  sweep and the GitHub handler use; tags and deleted branches are ignored.
- **`get_api_surface` reads routes from source.** FastAPI/Flask decorators, Express,
  Laravel, Symfony and Go registrations are found with one `git grep` at the
  indexed revision (heuristic, and it says so); a repository with no readable
  revision answers `supported: false` instead of an empty list.
- **The index remembers which branch it indexed** (`repo_index_state.indexed_branch`,
  migration `b3e9d27f5a40`), and each symbol carries a usage rank written at index
  time.

- **A Claude Code plugin for the developer MCP profile (`celmis-code`).** Install
  with `/plugin marketplace add <repo>` then `/plugin install celmis-code@celmis`.
  It wires `${CELMIS_URL}/mcp/dev/` with `Authorization: Bearer ${CELMIS_TOKEN}`,
  and ships the `celmis-search` skill (search order, howto rules, freshness,
  cursors, guardrails), a read-only `celmis-explore` agent, and three hooks that
  are stdlib-only and fail open: a one-time session note on index freshness, a
  one-time hint before the first identifier-like Grep/Glob, and a post-call check
  that says which files the index sha is stale for (`git diff <sha> -- <paths>`,
  no shell) so the agent reads those locally. There is no local proxy and no
  `version` in `plugin.json`, so a plugin update follows each commit. Docs:
  `packaging/claude-plugin/celmis-code/README.md`, including the
  `headersHelper` form (`bin/celmis-auth-header`, keychain first) and a
  managed-settings snippet.
- **A local end-to-end harness for the developer profile.** `tests/e2e_local/`
  boots the real app in-process (random port, SQLite, real indexing, real MCP
  client over HTTP) on three fixture repositories (Python, TypeScript, Go) whose
  secrets are planted at run time only. `scripts/dev_mcp_e2e.py` runs 12 gold
  scenarios (howto, find, outline, symbols, refs, grep, and three secret probes),
  scores them against a deterministic no-Celmis baseline (tokens and calls),
  and fails on any leaked value, reporting only labels. Run it with `--spawn`
  or `--url`. Lane-dependent tests (ACL and token matrix, audit rows, scenarios,
  canary scan) skip by feature probe; set `CELMIS_E2E_STRICT=1` to make a skip a
  failure. `./scripts/e2e.sh mcp_dev` is a contract smoke against a live stack; it
  needs a real issued token (`E2E_MCP_TOKEN`) and skips without one, because a
  self-signed JWT is refused by `/mcp/dev/`. The runner's report now says whether
  the leak scan was armed (`--require-leak-scan` makes "not armed" a failure),
  `--min-scenarios N` fails a run that skipped too much, secrets are scanned in
  both the text and the raw JSON, and a name counts as listed only inside a
  howto answer's `inputs` section. New HTTP tests cover cursors (no gaps, no
  duplicates, stale cursor), default token budgets per tool, the fresh and STALE
  freshness line around a push, and single_tenant installs.

- **A review closes with "Code Review Completed", and the PR hears a greeting first.**
  The started comment says hello on a PR's first review ("Hi! I'm Celmis.
  Starting the review of commit `abcdef1` (3 files)...") and "New changes -
  updating the review" on a later push. The closing comment leads with the
  verdict and the counts (found, by severity, by category), lists the top five
  findings with their place and a pointer to the inline comments, and ends with
  the scope and a link to the repository's review settings. A commands guide
  can be added under it. The previous layout stays available as `classic`.
  All of it speaks the repository's `review_language` through the message
  catalog (`en`, `uk`).
- **Two new review settings, wired through every layer.** `completed_comment`
  (`completed` by default, or `classic`) and `commands_guide_enabled` (off by
  default) inherit like the other settings: workspace default, repository
  override, effective value in the API, a row each in the Summary section of the
  settings UI in all 16 locales (Ukrainian translated, the rest carry English
  text until translated), migration `d6a1c3e5f701`.
- **The PR description carries the findings.** Between the overview and the
  changes walkthrough there is a findings block (total, top findings, how many
  were left as inline comments, or that nothing was found). A re-run of the same
  commit rewrites the stamp without a clock time, so the description settles
  instead of changing on every run.
- **`ReviewBatch.summary_sections`.** One list of named, ordered blocks that the
  summary comment and the description render from, each marked for the comment,
  the description, or both. Later stages add a section instead of editing the
  composers.
- **Team memories.** Short facts the team teaches the reviewers, kept per
  workspace, per repository, or per directory of a repository, and told to
  every review of the files they concern. One store (`review_memories`, table
  and three settings in migration `f8c3e5a7b923`), one module
  (`src/review/memories.py`) for the writing: `remember(...)` decides who is
  trusted (the token owner, a workspace editor or admin, or a name on the
  `memory_trusted_commenters` list), asks the model once whether the fact is
  already known (skip, merge or create; a model that is down stores the fact
  rather than losing it), and files a stranger's request as pending. Only
  active memories reach a prompt: fenced as `<team_memories>` data under a
  preamble that puts the output contract above them, cut to
  `REVIEW_MEMORY_PROMPT_CHARS` (default 3000) broadest first, and a directory
  memory is told only for pull requests that touch it. New `/memories` page
  (approve, reject, edit, bulk, search, a budget meter) and `/api/memories`;
  the repository policy's prompt preview shows what a review is told. New
  **Learning** section in the review settings with `memories_enabled`,
  `knowledge_approval` and `memory_trusted_commenters`, inherited repository
  over workspace like every other setting. A removed repository takes its
  memories with it; a GDPR export lists what a person wrote and an erasure
  unlinks their name from it. Memories are for workspace leads: viewers and
  members get a 403 on every `/api/memories` endpoint (and no tab); an editor
  sees and edits a repository's memories only where a team of theirs grants
  access, owners and admins see all of their workspace, and a repository one
  may not read is never named, counted, previewed (also not in the prompt
  preview) or told to a chat answer.
- **Issues backlog and auto-resolve.** An issue from a merged PR now stays on
  the backlog (open, with its target branch) instead of vanishing with the PR,
  and Celmis closes it once the target branch no longer has the problem:
  after each review of a later PR, on a merge, on a push, from a manual
  "recheck" and in a daily sweep. A blob-hash skip and an "anchor still there"
  check come first; only a gone anchor goes to the model, which can say
  fixed, not fixed or unsure, and only "fixed" closes (a file counts as gone
  only when its deletion is confirmed, renames are followed). An issue closed
  by the machine reopens when its anchor comes back; a person's decision never
  is overruled. The implementation rate (implemented, unimplemented,
  dismissed, abandoned) is frozen at merge and later fixes are reported as
  `resolved_later`; `/api/issues` takes `scope`, `resolution`, `outcome` and
  `include_duplicates`, and `/summary` carries the backlog figures. Four new
  inheritable settings (workspace and repository, all 16 languages):
  `issues_auto_resolve`, `issues_resolve_llm_verify`, `issues_resolve_max_llm`
  (0-50, default 8, one call per file chunk, own budget surface
  `issue_resolve`) and `issues_announce_resolved`. New env:
  `CELMIS_ISSUES_RECHECK_DEBOUNCE_SECONDS`, `CELMIS_ISSUES_SWEEP_INTERVAL_HOURS`
  (0 turns the sweep off), `CELMIS_DISABLE_ISSUES_SWEEP=1`. Migration
  `b0e5a7c9d145`. Review fixes: a repeat whose canonical issue a person
  closed (or that was fixed before the repeat merged) joins the backlog
  instead of going unchecked; the revert watch covers only the last 60 days of
  auto-fixes and never crowds open issues out of a pass; a merge recheck that
  found the branch busy asks again; a head read from a possibly stale local
  clone no longer decides a merge-time check; finding titles are fenced in the
  verification prompt; the manual recheck runs one branch at a time with the
  repository owner's provider. Second review: a merge stamps the branch the PR
  really landed on (a retargeted stacked PR no longer keeps a deleted branch);
  repeats are linked, closed and reopened only within one target branch; a
  revert reopens an auto-fixed issue only where the issue was raised, not for
  the same line elsewhere in the file; the recheck reads the repository's own
  policy (an unbound repository's opt-out holds, an unreadable policy skips
  the pass); a pass over more than 500 open issues moves on (least recently
  checked first); the model's code region is capped in characters; asking for
  one PR's issues no longer hides its repeats.
- **The business-logic check reads the Jira task.** A pull request that names
  a task (`PROJ-6066` in the title, branch, description or commits, Cyrillic
  branch names and keyboard-layout lookalikes included) is now held to the
  task's summary, description and numbered acceptance criteria (`AC1`, `AC2`, …)
  as well as to its own text, and a pull request with an empty description is no
  longer skipped when its task can be read. Connect Jira on the Connections page
  (site address, Atlassian email, API token; or reuse the Bitbucket token on the
  server): the site must be an https Atlassian Cloud address or a host in
  `JIRA_ALLOWED_HOSTS`, resolve to public addresses, and the token is verified
  with `GET /rest/api/3/myself` before anything is saved. Five new inheritable
  settings (repository > workspace > install) in Review categories:
  `task_context_enabled`, `task_project_keys`, `task_acceptance_field`,
  `task_include_comments`, `business_logic_auto` (`when_task_found` switches the
  agent on for pull requests whose task was read; naming it in
  `disabled_agents` still wins). The task text reaches the model fenced as
  untrusted evidence, secrets masked, capped, and cached per workspace
  (`task_context_cache`, `JIRA_CACHE_TTL_SECONDS`); a Jira failure is a sentence
  in the skip reason, never a failed review. New findings rule ids
  `logic.requirement-missing`, `logic.requirement-partial`,
  `logic.requirement-contradicts`. Admins can preview a task as the model reads
  it (`GET /api/task-context/issue/{key}`, `/fields`, `/projects`). Migration
  `d2a7c9e1f367`; providers gained `fetch_commit_messages`.
  Who can make the bot read a task: the connection is one workspace token, so
  with `task_project_keys` empty a key written in a pull request is read from
  any project that token can browse, whoever wrote the pull request. Set
  `task_project_keys` to the projects reviews are meant to read. Disconnecting
  or replacing the Jira token also drops the stored task reads of the
  workspace; a review waits at most 30 seconds in all on Jira; project keys are
  letters and digits (no underscore) in settings, the key check and the finder.

- **Productivity metrics and the `/productivity` page (Enterprise, under the
  `analytics` licence feature).** Reads the pull request history the sync
  backend collects and answers how fast work moves and how stable releases are.
  Owners and admins only, like spend and review cost (the routes answer 403 to
  everyone else and the tab is not drawn for them):
  cycle time split into coding, pickup and review; lead time for changes;
  deployment frequency; change failure rate; time to recover; merged pull
  requests; pull request size and cycle time by size; and, when the review
  issues ledger records outcomes, the share of review suggestions taken. Every
  figure is a median with p75/p90 and a comparison against the previous equal
  period, with DORA bands for the four delivery figures. Filters by repository,
  repository group, author and target branch; a slowest-pull-requests table and a
  developer activity table that withholds a median below three merged pull
  requests. The page also hosts the data-source panel: switch a repository on,
  watch the backfill, start or re-read a sync, estimate the backfill and edit the
  workspace and per-repository settings. The routes live under
  `/api/analytics/productivity` and are absent without the licence; capabilities
  reports the `productivity` feature accordingly. Labels ship in all sixteen
  locales. Cycle time counts only pull requests whose detail has been read (an
  unread one can carry an approximate merge time); a target branch that no
  deployment lands on no longer zeroes the delivery figures; ignored accounts are
  not offered as authors; and the page says when the previous period is older
  than the synced history.
- **Productivity sync backend (AGPL core).** Six tables (`productivity_repo_settings`,
  `productivity_pull_requests`, `productivity_pr_events`, `productivity_deployments`,
  `productivity_deployment_prs`, `productivity_sync_state`; migration `e3b8d0f2a478`) and the
  writers that fill them, in `src/productivity/`. Opt-in per repository: nothing is read
  from a provider until `enabled` is set. Settings layer repo row, then workspace row,
  then built-in (`settings.py`). `sync.py` runs list, detail, release shas, revert
  resolution and deployment rebuild under a soft lease, a 480 s time budget, a
  per-credential token bucket (`ratelimit.py`) and a stored watermark, so a run
  that hits a budget or a 429 resumes where it stopped (continuation job
  `prodsync-next:`). Adapters for Bitbucket, GitHub (GraphQL) and GitLab are in
  `providers/`. Bot comments, the author's own comments and quoted lines do not
  count as the first review; PRs are classified revert, hotfix, bugfix or
  feature (Latin and Cyrillic titles); deployments come from merges into
  integration branches, provider deployments or tags, with failures and recovery
  time. The review webhook marks a PR stale and a merge refreshes it; approved
  and unapproved events are now subscribed. Scheduler env vars:
  `CELMIS_PRODUCTIVITY_INTERVAL_MINUTES` (60, 0 turns it off) and
  `CELMIS_PRODUCTIVITY_FIRST_DELAY_SECONDS` (180). The metrics reader, API and
  UI are Enterprise and not part of this change.

  Review fixes: a backfill's continuation job is keyed by the job that queues it
  (`prodsync-next:<repo>:<job id>`, likewise `prodpr-next:`), because the queue
  dedups against running rows and a shared key stopped the chain after two slices;
  one unreadable release PR no longer blocks the deployment rebuild; Bitbucket
  commit lists are walked when the API leaves out `size`; Bitbucket's 12-character
  PR hashes match the 40-character commit lists; deployments are upserted (stable
  ids) instead of deleted and re-inserted; a merge webhook reuses the stored provider
  deployments instead of re-reading them; a GitHub list cut short by the page cap is
  an error instead of a silently advanced watermark.

- **Markers are invisible on Bitbucket.** The `<!-- … -->` lines Celmis finds
  its own comments and the description block by were shown as text in every
  Bitbucket comment. One module, `src/review/markers.py`, now owns every
  marker (`review`, `status`, `summary`, `chat:v1`, `finding`): the code keeps
  writing the HTML form, the Bitbucket provider hides it on the way out
  (`[//]: # (x)`, or zero-width characters as the fallback) and reveals it on
  the way in. A marker counts only as a line of its own, so a quote reply that
  copied it is not ours. Old comments and descriptions with HTML markers are
  still recognised and are converted by the next write. If Bitbucket renders a
  hidden marker anyway (checked on the `content.html` of every write) the
  process switches to the zero-width form by itself; `REVIEW_MARKER_STYLE`
  (`auto|html|refdef|zwsp`) pins it. `scripts/probe_bitbucket_markdown.py`
  shows on a test PR which form Bitbucket hides.
- **No raw HTML on Bitbucket.** `<sub>`, `<details>`/`<summary>` and `&amp;`
  become `_italic_` and `**bold**` at the Bitbucket boundary; fenced and inline
  code is never touched. Comments and descriptions are capped (30 000
  characters) without cutting a marker, and the overview in the description
  gives way before the author's own text does.
- **A description whose markers were lost is repaired by its heading.** When
  someone edits the description in Bitbucket's editor and the hidden lines
  disappear, the next write finds our block by `## 🤖 Celmis summary` instead
  of stacking a second one.
- **Markers are whole lines outside code, whatever the line ends.** A marker
  quoted mid-sentence, in an inline code span or in a fenced block (a finding
  about Celmis' own code) is no longer rewritten into a real hidden marker; a
  description saved with Windows line ends is read like any other; a
  description over the limit is cut from its longest stretch of text, so the
  summary block keeps its start and end markers around its content.
- **The bot's own text comes from one catalog, in the review language**
  (`src/review/messages.py`, English and Ukrainian, English fallback): the
  "reviewing…" placeholder, the skipped/failed notes, the "not reviewed" note
  and the lost-diff note. `ReviewBatch.review_language` carries it.
- `REVIEW_BOT_HANDLE` (default `@celmis`), the mention PR commands will answer to.
- A guard test walks every inheritable review setting through the resolver,
  both policy tables, the four API schemas, `api.ts`, `model.ts`, the settings
  section and the English texts, so a new setting cannot be wired halfway.

- **`howto(topic, repo)` on the MCP server.** Asks how a repository does one of seven
  things (`db`, `auth`, `config`, `http_client`, `logging`, `messaging`, `cache`) and
  answers with the code slices that show the pattern, the names of the environment
  variables and settings it reads, and where each value comes from (`.env.example`,
  compose, Kubernetes `secretKeyRef`, CI variables, Dockerfile, app config). It never
  returns a value, and ends by telling the agent to copy the pattern, take the values
  from those sources and ask the user or ops. It answers only for repos the caller may
  read as code; a denied repo and a missing one give the same reply. Registered on
  `/mcp/` and stdio under the `read:graph` scope (carried by every token kind), and exposed as `register_howto(mcp)`
  for other servers.
- **Every MCP tool output goes through one redactor.** `redact_for_mcp`
  (`src/security/mcp_redact.py`) replaces secrets with `[REDACTED:label]` in DSNs
  (including URL-encoded ones), auth headers, `key = value` assignments in code, YAML,
  JSON and properties, PEM blocks, provider keys, Kubernetes `Secret` data and
  high-entropy strings. References stay (`os.getenv`, `${X}`, `secretKeyRef`, vault
  paths, `changeme`), as do SHAs, UUIDs and integrity hashes. It is always on and
  ignores `redaction_enabled`. `output_guard.install_output_guard` applies it to the
  result of every tool and fails closed: if redaction raises, the caller gets
  "output withheld: redaction failed" and nothing else.
- **Secret files are not indexed and not readable through the tools.**
  `src/security/secret_files.py` classifies a path as `deny` (`.env*`, private keys,
  credential stores, `*.tfstate`, kubeconfig, `kind: Secret`), `keys_only` (names kept,
  values masked) or `ok`. The graph walker skips denied files, the file readers and
  the exploration agent's grep refuse them with one message that does not say whether
  the path exists, and `GIT_PATHSPEC_EXCLUDES` keeps them out of git-backed scans.
  `secret_path_globs_extra` adds repo-specific patterns.
- **Review fixes for howto and the redactor.**
  - A committed symlink is never followed: `.env` behind a link, or a file outside the
    clone, was readable through `howto` and indexed by the walker. Symlinks are skipped
    by the file lists, the readers, the trace and the walker; the indexer no longer
    parses a real `.env`. The deny list gained `.envrc`, `.env-*`, `.pgpass`,
    `.my.cnf`, `.dockercfg`, `.s3cfg`, `client_secret*.json`, `*.keytab` and
    `*.pem.*`, and the git pathspecs ignore case.
  - Redaction no longer takes quadratic time on one long word (20k characters took
    12 s and stalled the loop). Runs of 2048+ non-space characters are replaced by
    `[REDACTED:long-literal]`, the PEM and PuTTY rules are linear, the guard runs the
    redaction off the event loop, and a redaction past its time budget is withheld.
  - Secret names are split on `_`, `-`, `.` and camelCase: `db_pass`, `dbPass`, `PGPASS`,
    `pw`, `creds`, `HMAC_KEY`, `AccountKey`, `PEPPER`, `SEED` and `{"name":
    "DB_PASSWORD", "value": ...}` pairs are redacted. A quoted 40/64-digit hex under a
    neutral name or a key-like name is redacted; SHAs after a hash-like word stay.
  - New forms: Azure `AccountKey` and `SharedAccessKey`, Slack, Discord, Teams and
    Telegram webhook secrets, pre-signed URL signatures, `curl -u`, `mysql -p`,
    `--password x`, `Cookie` and `Set-Cookie`, and `user:pw@host/db` without a scheme.
  - The detailed `howto` answer applies the caller's path rules to dependencies and
    related tests, slices are cut at 400 characters per line and at the caller's
    token budget, and the tool description is under 200 characters.

### Security

Found by the access audit of the dev MCP, and by wiring the review features onto it.
Each item has a regression test.


- **Reading a repository's reviews needs the right to read the repository.**
  `GET /api/reviews/{id}`, `/diff`, `/findings`, the history list and
  `GET /api/pull-requests/{id}/runs` used to check only the workspace. They now
  apply the same default-deny as MCP; a closed repository answers 404, exactly
  like a run that does not exist. A team access rule below `code` (`metadata`,
  `none`) now also narrows a team grant on the REST side, as it already did in
  MCP; owners and admins are never narrowed. `metadata` still lets a repository
  be named in a list, never opened.
- **Groups, Claude Code sessions and "who works on what".** A group only takes
  repositories its creator may read (a refused one gets the same 422 as an
  unregistered one); cross-repo drift greps only the siblings the reviewer may
  read, never `.env*`, keys or credential files, and masks secrets on the quoted
  line; `POST /api/agent-sessions` refuses a repository the caller may not read;
  `GET /api/repos/developers` lists only repositories the caller may see.
- **An MCP token dies with its account.** A deactivated person is refused on
  every call, and erasing a person (GDPR) revokes all their tokens
  (`mcp_tokens_revoked` in the answer).
- **`migrate_consumers` no longer takes `user_id`.** It acts as the caller,
  needs the owner or admin role of the workspace (or a global admin) and `code`
  access to each repository it changes. Breaking for clients that passed
  `user_id`.
- **`ask` honours the repository list of its token**, like every other tool.
- **`grep` is no longer an oracle for redacted values.** A hit whose match lies
  inside a redacted region is dropped, so a value cannot be recovered one
  character at a time by guessing the pattern.
- **Redaction** now also covers Kubernetes `name:` / `value:` pairs, XML
  elements and attribute pairs, `.npmrc` tokens, `docker login -p`, setter and
  `define('X_PASSWORD', ...)` calls, `Pwd=` in code strings, Dockerfile `ENV`,
  htpasswd/crypt hashes, full-width `:` and `=`, and "the password is ..." prose.
- **Credential files** added to the deny list: `*.tfvars.json`, PKCS12, GCP and
  Firebase key files, `wp-config.php`, `local_settings.py`, Ansible vault files,
  `values-secret*`, `htpasswd`, `vault-password.txt` and similar names; `.env `,
  `.env.` and names with zero-width characters no longer slip past.
- **`howto` output is not an injection channel.** Secret-store names and file
  names printed from a repository are validated and shown as inline code.
- **The Claude Code plugin refuses plain http.** `celmis-auth-header` will not
  print a token for a `CELMIS_URL` that is not https (localhost excepted), and
  the hooks say so instead of nudging towards the server.
- **Per-token limits on the MCP server** (against one token starving the rest):
  `CELMIS_MCP_RATE_PER_MINUTE` (default 120, `0` = off),
  `CELMIS_MCP_MAX_CONCURRENT` (default 4) and `CELMIS_MCP_CALL_TIMEOUT_SECONDS`
  (default 60). A call over a limit is answered as denied and audited. A dev
  call fans out to at most 40 repositories.

- **Role limits on the new surfaces are one rule, checked everywhere.**
  Memories are for editors and above (an editor sees only the memories of
  repositories their teams may read, plus the workspace-level ones),
  Productivity is for owners and admins, and the Jira connection and
  `/api/task-context/*` are for owners and admins; viewers and members get a
  403 and no navigation entry. The connection list stays open to every member
  (the dashboard needs to know whether a provider is connected) but now tells
  only owners and admins which account, host or e-mail is behind it, and no
  endpoint returns a token. The Connections page and its tab follow the same
  role and say so instead of offering forms that would answer 403.
- **A new route cannot skip its gate.** A test walks every mounted router and
  requires the matching role dependency on each route of the memories,
  learning, productivity, task-context and credential surfaces, and fails on a
  route that names one of those surfaces without being in its table. Role
  matrices (owner, admin, editor, member, viewer, a member of another
  workspace, somebody with no workspace, global admin) now run against the
  real membership rows for the Jira and credential endpoints and for every
  productivity endpoint.

- **One access model for every review feature.** `code_readable_repo_slugs`
  (REST) mirrors the MCP resolver: memories, learning, review policies, the
  issues backlog (list, summary, recheck, status change), review feedback and
  the PR commands list read repositories the caller may open, not merely name.
  A test compares it with `effective_access` for every principal, and a test
  walks every route and every MCP tool of both projects.
- **Chat replies and the business-logic answer go through the central
  redactor** (`redact_for_mcp`) before they are posted; if it fails, the answer
  is withheld, not posted.
- **Review feedback routes check the run.** `GET`, `PUT` and `DELETE` on
  `/api/feedback/run/...` answer 404 for a run in a repository the caller cannot
  read, and look rows up inside the workspace.
- **An MCP tool cannot open a role-gated surface the REST API closes** (a test
  checks the memories, issues, productivity and task-context tools).

### Fixed

- **Closing a repository graph no longer waits ~4 s.** Every MCP tool call opens
  the embedded graph and closes it again; the close sent SHUTDOWN and then let
  redis-py's default retry policy reconnect, with backoff, to the server it had
  just stopped. A `list_repos` over four repositories took ~16 s. The close is
  now one attempt; the snapshot is still written before the server exits.

- **Learning from feedback: review fixes.** A finding posted twice on one pull
  request keeps a thumb given on either comment (reactions are read per finding,
  not per comment, and a thumb is withdrawn only when no comment carries it); a
  question or a quoted keyword is no longer read as a dismissal or an accept, and
  a bare `-1` / `+1` counts only as the whole comment; a verdict from the reviews
  page is matched to the stored finding by file, title and rule; one unreadable
  comment no longer stops the reaction poll; erasing a person who signalled under
  both e-mail and user id no longer collides on the signals table.
- **The answer to `business-logic` is cleaned like a chat answer.** It quotes a task and a
  diff written by other people, so before it is posted as the bot its markers, images, raw
  HTML and live @-mentions are neutralised.
- **A push no longer deletes a finding somebody asked about through the bot's own account.**
  On an install where the token is a person's account, the question under a finding is
  written by the same account as the finding. A comment of the token's account with none of
  Celmis's markers now counts as a person's words and protects its thread from the cleanup,
  on all three providers.

- **`howto` and the full `/mcp` server no longer start every repository's graph on each
  call.** Working out which repositories a caller may reach listed them through a call that
  opens each repository's graph to count its symbols (a graph server start and stop, seconds
  each), so a `howto` call cost about four seconds per repository in the workspace (sixteen
  seconds with four repositories). It now lists the checkouts on disk by name. Measured on
  one repository: 3.7 s to 0.1 to 0.4 s.
- **`howto` slices start at the code, not at a comment.** A library named in a comment or a
  docstring ("PyJWKClient cannot load keys behind the proxy ...") counted as a use and
  anchored the slice there, so the answer showed a file's header instead of the code that
  verifies the token. Comment and docstring lines are now ignored when looking for the
  pattern. The header line also reads "indexed just now" instead of "indexed now ago".

- **`/mcp/dev` masks the secrets people actually commit.** `DB_PASSWORD=value`,
  `password: value`, `password='value'`, `redis://:value@host`, JSON `"token": "value"`
  and secret defaults in `os.environ.get(...)` / `process.env.X || '...'` are masked
  in `grep`, `read_symbol`, `refs`, `find` and `outline` (a pointer such as
  `${DB_PASSWORD}`, `settings.db_password` or `changeme` is left readable). Output
  is redacted before it is cut to a line or signature length, so a secret straddling
  the cut is no longer half-printed. `outline` prints names only for a repository the
  caller may see as metadata.
- **`/mcp/dev` cursors are about 20 characters** whatever the number of repositories
  (they were several kilobytes over a large fleet) and are pinned to the revisions of
  the repositories that produced hits, so a push elsewhere does not reset page 2.
- **`find` keeps the best candidates on large repositories.** Candidates were the 200
  shortest names; they are now ordered by how the name matches, then by how often it
  is called, so a typo or a common word still reaches the symbol that is used most.
- `grep` and the sibling-repository text refs say when the indexed revision is no
  longer in the clone instead of answering "no matches"; `repos` pages exactly as many
  repositories as the `idx:` line can describe; `ask` never lists a secret file as a
  source; root-level `.env`, `.npmrc` and similar are recognised as secret paths; a
  symbol that lost its last caller loses its usage rank on the next index pass.
- **`search_symbols` finds what was meant.** `kind` filters inside the query before
  the limit (twenty variables called `user` no longer hide the one function),
  matching is ranked and tolerates a typo (`mode`: auto, exact, prefix, fuzzy),
  repositories are interleaved, `end_line` is returned, `project_id` is optional,
  and the answer no longer lists repositories the caller may not read
  (`blocked_repos`, `access_notice`, `hidden_symbol_count` are gone from
  `search_symbols`, `find_consumers` and `get_api_surface`).
- **An incremental index no longer fails for ever on a busy repository.** When the
  shallow clone has moved past the revision the index was built from, the pass
  rebuilds from the checkout instead of asking git for a diff that cannot be taken.
- **Tool descriptions over 300 characters on `/mcp` are shortened.**
- **`howto` follows the code into its config loader.** A connection or auth pattern
  that reads `config.databaseUrl` now also shows the loader that reads
  `process.env.DATABASE_URL` or `os.Getenv(...)` (TypeScript, Go and others, not only
  pydantic settings), lists the names it reads that belong to the topic, and the
  secret-store path it names. Helper calls such as `required("DATABASE_URL")` count
  as reads inside a loader file.
- **Credential files named `<anything>credentials<anything>.json` are never indexed or
  returned**, and a JSON file whose content says `"type": "service_account"` is
  refused by the content check whatever its name.
- The `celmis-mcp` skill gave the tool count as 18; the server has 48 on `/mcp`, and the
  developer profile adds nine more on `/mcp/dev`. The skill now says so.
- **A refused finding folded into the summary no longer breaks it.** A body cut inside a
  code fence is closed again, so the rest of the summary (and Bitbucket's tag
  rewriting) is not swallowed as code; a malformed `Retry-After` on Bitbucket no
  longer aborts the review; the GitHub review body and the classic layout follow
  the review language.

- **Bitbucket: an inline comment the API would not place is no longer lost.**
  A refused finding is folded into the summary comment ("Findings without a
  place in the diff") with its position and explanation; GitLab does the same
  for a refused discussion.
- **Bitbucket: comments on unchanged lines are anchored by both sides** (`from`
  and `to`), a 429 is waited out once (honouring `Retry-After`, capped at 30 s)
  before the comment counts as refused, and the description update sends back
  every field it read (`draft`, `close_source_branch`) so a rewrite does not
  reset them.
- **A Bitbucket pull request is no longer "skipped, no diff" when it has one.**
  Bitbucket answers `/pullrequests/{id}/diff` with a 302 and an empty body;
  the guarded client never follows a redirect on its own, so the diff read as
  empty and the pull request got only a quiet "no diff content" note. The provider
  now follows the redirect itself — at most three hops, same host only — asks
  for `text/plain` and reads the bytes as UTF-8. When the diff is still empty
  and the diffstat lists files it retries (by commits, then 2 s and 5 s later)
  and then fails the run and says so on the pull request; a PR with no files
  stays a quiet skip. When the diffstat cannot be read either, the run fails
  with a sentence of its own (the diff could not be verified) instead of
  skipping quietly, and a transport error on a Bitbucket read is reported as
  "Bitbucket request failed (<error type>)" without the exception's text. GitHub and GitLab treat a 3xx as an error instead of an
  empty answer, and both now report how many files the PR has
  (`PullRequest.reported_files`), which the "check reviewable changes" gate
  uses to tell a lost diff from an empty change-set.
- **Cyrillic (and other non-ASCII) file names are anchored.** git writes such
  paths quoted with octal escapes in diff headers; they are decoded, so hunks,
  findings and inline comments name the real file.
- **Applying a fix on Bitbucket follows the redirect on the file read** and
  quotes the path.
- **Editing a pull request's title or description no longer starts another
  review** of a commit that is already reviewed (Bitbucket `pullrequest:updated`,
  GitLab `update`). A failed or skipped last run, a new commit and a PR never
  seen are still reviewed. This also keeps `summary_target=description` from
  re-triggering itself.
- **Integration pass over the new surfaces.**
  - Comments written through the token owner's account are that person's: the
    reviewer ignores only comments that carry its markers or come from a bot
    account, so a person on a personal token can use the commands and teach it.
  - The business-logic agent reads the whole pull request, also in an
    incremental review, so a requirement met by an earlier commit is not
    reported missing.
  - A thread the reviewer resolved itself after a push is not read as feedback
    from a person, and a reply, thumb or resolved thread from somebody who may
    not command the reviewer in that repository teaches nothing.
  - The guide at the end of the completed comment lists only the commands the
    repository has not switched off (chat, memories, task context), and the
    wording of `start-review` and `review --force` says what each really does.
  - "Same commit already reviewed" is decided only by the commit of the last
    complete, posted review, so a failed or partial run is retried.
  - `GET /api/pull-requests/{id}/commands` answers 404 to somebody who may not
    read the repository.
  - Memories hide the repository, pull request and author they were taught from
    when the reader may not read that repository.
  - A redundant index on the productivity events table was removed from its
    migration (the model never had it), so the migration chain and the models
    agree; every new revision downgrades.

### Tests


- `scripts/dev_mcp_journey.py` runs the whole developer journey over real HTTP on an
  in-process Celmis: users in six roles, teams and rules over REST, tokens issued by the
  superadmin over REST (developer A, developer B, expired, revoked), the scenarios through
  a real MCP client with per-call size and latency against a grep-and-read baseline, a
  security matrix (84 checks), and a leak scan of every captured output, log and audit row.
  `--real` runs the three developer requests on clones of real local services and keeps the
  results outside the repository. Two scenarios were added to the gold file (auth like
  service X; credentials of service X used in service Y).

- Leak tests plant runtime-generated fake secrets in a synthetic repo (DSNs, headers,
  YAML/JSON, PEM, base64, URL-encoded) and assert none reaches the output of `howto`
  or of any existing tool; a table of every tool on both servers fails when a tool is
  missing from it, and a negative control proves the check can fail.

### Integration review fixes

- **The pull-requests list names only repositories the caller may read.**
  `GET /api/pull-requests` (rows, the total and the repository facet) filters by
  the same per-repository permission as the other review routes; a member no
  longer sees titles and authors of a closed repository.
- **REST and MCP read a repository the same way.** When an access rule names only
  another team, a team that holds a read grant on the repository is now refused by
  REST too, as MCP already did: a rule narrows, a grant is only the fallback.
- **The repository groups API is scoped.** The list shows only the repositories
  the caller may read (a group with none is hidden, except from workspace
  managers); creating, renaming, adding to, removing from and deleting a group
  needs editor or above and `review` permission on every repository in it. A
  group the caller cannot see answers 404.
- **An access rule names a registered repository.** `PUT /api/access/rules`
  resolves the slug (`provider:owner/name`, a bare `owner/name`, or the registry
  slug) to the registered one and answers 404 for an unknown repository, so a
  rule can no longer be written against a spelling that never matches.
- **A self-service token carries exactly the write scopes it was granted**, not
  every write scope the profile allows.
- **Redaction ReDoS closed.** The central redactor and the secret scanner no
  longer take quadratic time on a long run of whitespace: runs longer than 64
  whitespace characters are collapsed before matching and every whitespace
  quantifier is bounded. One crafted file could pin a worker for minutes.
- **More real secret shapes are redacted:** markdown table rows and bold or
  code-quoted names, XML name/value pairs in either order, SQL
  `PASSWORD '...'` and `IDENTIFIED BY`, `sshpass -p`, and `netrc` entries.
- **The literals floor reaches every model-bound text.** `howto`, the PR chat,
  the cross-repository drift check and the exploration agent mask the values of
  secrets the instance itself knows, on top of the central rules. The floor moved
  to `src/security/secret_literals.py`.
- **Two more grep oracles closed.** The developer profile's `grep` and the
  exploration agent's `grep_in_file` match against the redacted text, so whether
  a guessed prefix "hits" no longer says anything about the hidden value.
- **The issue resolver's first check of a branch no longer waits on another
  pass.** On PostgreSQL the state row is created in its own transaction with
  `ON CONFLICT DO NOTHING`, so a second pass sees the row locked and reports
  busy at once.
- **The productivity migration is idempotent.** `e3b8d0f2a478` skips tables and
  indexes that already exist and its downgrade tolerates a second run, so a
  database that had them created by hand upgrades cleanly.
- **The compose file forwards every documented setting** the API reads (MCP
  limits, the previous JWT secrets, trusted proxy, extra secret path globs, the
  background-job intervals and budgets, the PR chat and bot-handle settings and
  the learning thresholds). The API service has no `env_file`, so a variable not
  listed there never reached it.
- **Docs corrected.** `@celmis pause` never existed (the Pause button does; the
  dead `command` pause reason is gone); `@celmis remember:` trust follows the token
  owner and `memory_trusted_commenters`, and workspace roles apply on the
  Memories page; the productivity API lives under `/api/analytics/productivity`;
  the MCP skill and `server.json` teach `issue-token --user --workspace --repos
  --days`; a refused token answers 403 with the reason; the howto scope is
  `read:graph` on `/mcp/` and `read:code` on `/mcp/dev/`; the MCP tool count is 49
  (33 read, 16 write) everywhere, and a test now reads it from the loaded tool
  map instead of the source literal; the Jira, learning, issue-sweep and
  productivity settings are documented; the plugin README says `CELMIS_URL` must
  be `https://`.
- A tracked merge leftover (`models.py.orig`) was removed and `*.orig` and `*.rej`
  are ignored; `scripts/dev_mcp_journey.py` and `scripts/dev_mcp_e2e.py` are
  executable.

### Upgrade notes

- Every GitHub, GitLab and Bitbucket webhook installed before this release lacks the
  comment, thread, approval or push events (see the BREAKING section for the list per
  provider) until it is repaired: use "Repair all webhooks" on the webhooks page. The
  installer subscribes new hooks only, so a hook nobody repairs keeps reviewing but
  ignores `@celmis` commands and feedback.
- The database moves in one chain with a single head. From v2.3.5 run
  `alembic upgrade head`: twelve revisions apply in order, ending at
  `b3e9d27f5a40`, and each one downgrades. The MCP token and audit tables sit
  between the feedback-learning revision and the index-branch column.
- Before the first start of this release: issue per-person MCP tokens, and run
  `analyzer access bootstrap --team <team> --visibility code --all-unruled` for
  the teams that need their old access (see the BREAKING section).
- The webhook repair above, the per-person MCP tokens and the access bootstrap are the
  only manual steps.
- `CELMIS_UNRULED_REPO_ACCESS=open` also restores the old reading of the review
  features on a single-tenant install. It is a stopgap.

## [2.3.5] — 2026-10-06

### Security

- **The in-app agent is bound to the asker's workspace membership.** Every
  agent verb (and the MCP tools built on the same actions) now refuses a
  caller who is not a member of the workspace; before, several reads and
  writes (spend, alerts, jobs, repository list, audits, docs, auto-review)
  ran for a non-member holding a workspace id.
- **Only the author of a plan — or the workspace owner — may press it.**
  `POST /api/automation/execute` let any member run another member's plan
  (plan ids are visible in history). Other members, other admins and global
  admins who are not the owner now get 403. Roles are still re-read at
  press time.
- **Issues without a named repository no longer include repositories the
  asker's team may not read** (rows, totals and status counts). New optional
  `exclude_repo` filter on `/api/issues`.

### Changed

- **Spend and budget are for workspace owners and admins** (and global
  admins): `/api/spend/summary`, `/daily`, `GET /budget`, the agent's and
  MCP's `get_spend` / `get_budget`, the Usage page and the settings spend
  card (shown as admin-only instead of an error). `/api/usage/summary` is
  unchanged.
- **Review cost is for workspace owners and admins**: `cost_usd` is left out
  of review history, run detail, PR runs, review analytics and the MCP
  `get_review` tool for other roles; the analytics cost tile is hidden, not
  shown as $0.
- **Workspace owners and admins hold every repository of their workspace**
  — in the grant check every per-repo route and agent/MCP action uses, and
  in code (research) access. Owners/admins of another workspace get
  nothing. Viewers, members and editors keep team grants.
- **Repository lists show only what the asker may read**: `GET /api/repos`,
  the agent's `list_repos`, MCP `list_workspace_repos`, the review-settings
  overview and the agent's settings snapshot (including its auto-review
  count).

### Fixed

- **Agent: changing a repository-scope review setting or guidelines always
  failed** (`TypeError` — the policy upsert gained an argument the action
  never passed).
- **Agent: the review-a-PR card refused viewers** that the action and route
  allow with a review grant.

### Tests

- `tests/automation/test_agent_role_matrix.py`: every agent verb × every
  role (viewer … owner, team grants, non-member, other workspace, global
  admin, superadmin), card-vs-action and agent-vs-MCP parity, cross-workspace
  ids, plan press. A new verb without a row fails CI.

## [2.3.3] — 2026-10-05

### Added

- **The in-app agent reads and explains, and does far more.** The chat went
  from 10 verbs to 33. New reads: the review settings in force for the
  workspace or one repository (each value with where it comes from — repo,
  workspace or built-in), review history and one run's findings, issues,
  questions about the code (the Q&A pipeline) and code search (symbols,
  usages, owners, architecture), spend, usage and budget, alerts, jobs, the
  dependency-audit delta and SBOM link, and workspace members. Settings,
  review runs, spend, usage, alerts, jobs and the audit delta are answered by
  a second model call that explains the real data rather than generic text.
  New writes, each shown as a plan and run only on a second press with the
  same role checks as the pages: review a PR or every open PR of a
  repository, re-index repositories, change an issue's status, set the
  budget, acknowledge an alert, retry or cancel a job, cancel an audit.
- **MCP: 48 tools on the HTTP mount (32 read, 16 write).** Every new agent
  verb is a tool too, plus the review-configuration verbs
  (`get_review_settings`, `update_review_setting`, `propose_review_rules`,
  `generate_review_rules`). New scope `write:config`. `/api/mcp/token` and the
  MCP settings page can issue write scopes on explicit request, bounded by
  the caller's role (`write:config` / `write:repos`: owner or admin;
  `write:reviews`: editor and above); the default token stays read-only.
- **Pull Requests summary cards.** Reviewed today, Awaiting review (open PRs
  into a targeted branch with no completed review at their current head,
  read from the providers' cached listings, so repositories in manual mode
  count too; partial results are shown as "≥N") and Needs attention (latest
  review failed, requested changes, or an open critical/error finding).
  Clicking a card filters the list to exactly those PRs.

### Changed

- **Target branches are applied before queueing.** Bulk review (page, agent
  and MCP — one shared action) no longer queues PRs whose base branch the
  repository's target branches leave out; they are returned as skipped and do
  not count toward the bulk limit. The open-PR list marks them, sorts them
  last and leaves them out of the "review all" count; reviewing one by hand
  asks for confirmation. Webhooks (GitHub, GitLab, Bitbucket) and the GitLab
  poller record the gate skip without running a review job. The
  orchestrator's target-branch gate stays the authority.
- **MCP write tools enforce their scope on every call** on the HTTP mount,
  not only in `tools/list`. Legacy scope-less tokens and `admin` still pass.

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
