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
