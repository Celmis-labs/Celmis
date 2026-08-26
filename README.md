<div align="center">

# Celmis

**Self-hosted code intelligence — multi-repo Q&A, AI pull-request review, cross-repo drift detection**

</div>

Celmis indexes private codebases into a symbol graph and a vector store, then
answers questions about them and reviews pull requests against them. It runs on
one machine under `docker compose`: Postgres, Qdrant, the API, the web UI and a
sandbox, with a model provider of your choice behind them.

Three surfaces:

- **Ask the code** — a question in a chat UI, answered with **file:line
  citations** drawn from as many repositories as you point it at. Answers
  stream over SSE.
- **Pull-request review** — five agents read the diff and post findings to
  GitHub, GitLab or Bitbucket, with a cross-repo drift detector that catches
  configuration divergence a single-repo reviewer cannot see.
- **Supply-chain checks that cannot produce a false positive** — malicious
  package detection, lock-file drift and SBOM export, decided by reading files
  rather than by asking a model.

---

## Table of contents

- [Results](#results)
- [Audit of the false positives](#audit-of-the-false-positives)
- [Test repositories](#test-repositories)
- [Quick start](#quick-start)
- [First user and admin](#first-user-and-admin)
- [Connect a repository](#connect-a-repository)
- [Ask the code](#ask-the-code)
- [Pull-request review](#pull-request-review)
- [Deterministic checks](#deterministic-checks--no-model-no-false-positives)
- [Connect Claude Code and other MCP clients](#connect-claude-code-and-other-mcp-clients)
- [Configuration](#configuration)
- [Operations](#operations)
- [Local development](#local-development)
- [CLI reference](#cli-reference)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## Results

Celmis was run on the [Martian Code Review Bench](https://github.com/withmartian/code-review-benchmark)
offline set: 50 curated pull requests, 173 human-written golden comments, scored
against the gold set by an LLM judge. Measured on `e0db376` with
`gemini-3.6-flash` at temperature 0.1, no reasoning tokens.

| Judge | F1 | Precision | Recall | Rank |
|---|---:|---:|---:|---:|
| claude-opus-4.5 | 47.5% | 52.4% | 43.4% | **17 / 50** |
| claude-sonnet-4.5 | 44.9% | 48.0% | 42.2% | **17 / 50** |
| gpt-5.2 | 42.7% | 46.0% | 39.9% | **17 / 50** |

The F1 moves 4.8 points depending on who judges. The rank does not move at all —
seventeenth under all three. Below us in every one of the three: CodeRabbit
(19/25/23), every version of Greptile (26–29), Kodus (21/23/21), Copilot,
Claude Code, Gemini, and CodeAnt.

The whole run cost **$5.88** — $0.118 per pull request — and produced 153
findings, 3.06 per PR (defect 114, security 27, contract 6, structural 6).

**Why this comparison is fair.** Martian ships its own evaluations of 49 tools
in the benchmark repository, produced by the same three judges on the same 50
PRs against the same goldens. We did not re-score anybody: their rows are taken
as published and ours is appended. Reproduce the whole table with:

```bash
python3 autoloop/offline_table.py anthropic_claude-sonnet-4-5-20250929
```

**Offline is not the public leaderboard.** Martian runs two benchmarks. The
public leaderboard is the *online* one — 200,000 real pull requests scored by
what developers actually fixed. This table is the *offline* one — 50 curated PRs
scored against a gold set. They measure different things and the numbers are not
interchangeable. Claims of the form "tool X is #1 on Martian" usually refer to
the online table, a different metric, or a different judge.

**What this number does not contain.** The graph was empty for all 50 PRs
(`graph_status` null, drift empty on every one), because the benchmark set is
isolated single-repository pull requests — there is no sibling service for a
symbol to have consumers in. Cross-repository drift, the thing this product
carries a symbol graph for, contributed exactly nothing to the score above. It
is not measurable here, and we are not claiming it from this table. See
[Test repositories](#test-repositories) to watch it work on real code instead.

## Audit of the false positives

Benchmark scoring has a structural floor: the judge matches our comment against
a finite list of human-written goldens, so a correct finding the annotator never
wrote down is counted false **by construction**. We opened all 79 of ours in the
source at the measured commit and assigned a verdict to each.

Of 79 findings scored as false positives, **33 are real defects** the gold set
does not contain, 38 are genuinely wrong, and 8 could not be settled from the
code. That puts the true precision of this run between 69.7% and 75.0% rather
than the measured 48.0% — but that corrected figure **cannot be compared with
anything in the table above**, because nobody has audited the other tools the
same way and their false positives almost certainly contain a similar share of
real defects; for comparison with other tools the measured 48.0% is the honest
number, because it is the same method applied to everyone.

Twenty-four of the 38 genuinely-wrong findings share four root causes, and none
of them is "the model is weak" — all four are about what the model was shown.
The largest is an identifier declared in the same file but outside the excerpt
the agent received: a method parameter 26 lines up, an import on line 3, an
`attr_reader` on line 18.

The full report gives the claim, the code at that commit, the verdict, the
reasoning and a permalink for each of the 79, so any verdict can be disputed
with the same evidence in front of you.

## Test repositories

Every review in the run above is still live and public. These are real pull
requests from real projects, forked with their history, carrying the inline
comments Celmis wrote:

| Fork | PRs |
|---|---:|
| [celmis-bench/keycloak](https://github.com/celmis-bench/keycloak) | 9 |
| [celmis-bench/grafana](https://github.com/celmis-bench/grafana) | 10 |
| [celmis-bench/discourse-graphite](https://github.com/celmis-bench/discourse-graphite) | 10 |
| [celmis-bench/cal.diy](https://github.com/celmis-bench/cal.diy) | 10 |
| [celmis-bench/sentry](https://github.com/celmis-bench/sentry) | 6 |
| [celmis-bench/sentry-greptile](https://github.com/celmis-bench/sentry-greptile) | 4 |

Worth opening first:

- [keycloak#17](https://github.com/celmis-bench/keycloak/pull/17) — a null
  dereference and a recovery-code indexing question in Keycloak's test storage
  provider
- [grafana#16](https://github.com/celmis-bench/grafana/pull/16) — a Storage
  failure recorded against the Legacy metric, one of three instances of the same
  mistake in that file
- [cal.diy#11](https://github.com/celmis-bench/cal.diy/pull/11) — `forEach` with
  an async callback, so the deletions are fire-and-forget and the surrounding
  `try` catches nothing
- [sentry#11](https://github.com/celmis-bench/sentry/pull/11) — seven inline
  comments on one Kafka consumer PR

You are reading unedited output, including the findings the audit above marks
wrong. Nothing was removed after scoring.

## Quick start

### What you need

| | | |
|---|---|---|
| **Docker** | 24+ with Compose v2 | Docker Desktop on macOS/Windows, the native engine on Linux |
| **A model API key** | one of | Google Gemini, Anthropic, OpenAI, OpenRouter, Groq or Mistral. A free Gemini key is enough to evaluate: <https://aistudio.google.com/app/apikey> |
| **RAM** | ~4 GB free | Measured on a real indexing run: 1.1 GB peak across all five containers, 565 MB at rest |

Postgres and Qdrant are **bundled** — no external cluster to provision. No
Python or Node.js install is needed for the Docker flow.

### Start it

```bash
git clone <your-fork-url> celmis
cd celmis

# Generates .env and fills every secret in the format each one needs.
# Idempotent: run it again after a pull and it fills only the new blanks.
./scripts/init-env.sh

docker compose --env-file .env up -d

# Wait for healthy — first boot pulls three images and applies migrations
docker compose ps
```

Open <http://localhost>.

Nothing is built here. The three images are pulled from the registry named by
`CELMIS_REGISTRY` at the tag in `CELMIS_TAG`, for `linux/amd64` and
`linux/arm64` — Apple Silicon and an ARM server both get a native image.
Building them on the machine that runs them was measured at 485 seconds and
4.2GB of disk for `api` alone, which is why installing no longer means
compiling.

Port 80, not 3000: a reverse proxy puts the app and its API on one origin and
serves the API under `/backend`. That is not a deployment preference — the
browser bundle asks for a relative path, which is the only way one published
image can serve every installation instead of just the one it was built on.

To work ON Celmis rather than run it, add the dev overlay and you get local
builds back:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

`init-env.sh --check` reports what is still empty without writing anything.

### Stop

```bash
docker compose down       # stop, keep your data
docker compose down -v    # stop and DELETE every volume
```

---

## First user and admin

The sign-up form on `/login` works as soon as the stack is healthy. That
account is a **normal user** — signing up grants no admin rights, not even to
the first person through the door.

Global admin comes from the environment instead: sign in with
`CELMIS_MASTER_EMAIL` and `CELMIS_MASTER_KEY` (as the password), both in
`.env`. Whoever runs the box is the admin, which is the model a self-hosted
install wants rather than whoever reached the form first. The path does not
exist unless both variables are set, and every use of it is written to the
audit log.

To promote a normal account:

```bash
docker compose exec api analyzer auth make-admin you@example.com
```

---

## Connect a repository

1. **Settings → LLM Setup** — paste a provider key. It is encrypted with
   `CREDENTIAL_MASTER_KEY` before it touches the database, and the UI only
   ever shows you the first and last four characters again.
2. **Connections** — add a GitHub, GitLab or Bitbucket token. Use a **machine
   account**, not your own: a personal token reaches every repository you can
   see, and tokens end up in backups, logs and screenshots.
3. **Repositories → Add** — pick repositories from the provider, or paste a
   clone URL. Indexing is queued; the job shows on the same page.

Indexing builds two things from the same checkout: a symbol graph (definitions,
calls, imports — what the review agents reason over) and embeddings in Qdrant
(what Q&A retrieves). A 120k-symbol repository takes about a minute on four
cores.

Twenty-three languages parse into the graph. A file in a language without a
parser is said so out loud rather than silently skipped — `analyzer graph-stats`
lists what was and was not read.

---

## Ask the code

**Ask the code** in the sidebar, or from a terminal:

```bash
docker compose exec api analyzer ask "where do we validate webhook signatures?"
docker compose exec api analyzer chat           # interactive session
```

Answers carry `path:line` citations, and every one is checked against the
source before it is shown — the reply says so ("All N citations verified against
the source"), and a claim the retriever cannot support is dropped rather than
paraphrased. Each answer also carries what it cost: elapsed time and
input→output tokens.

**Ask the code lives inside a Project.** A project groups repositories that
belong to one product, and a question runs across all of them at once — which
is how an answer can span a gateway, a worker and a billing service that live
in three repositories. Create one under **Ask the code → Projects**; only
repositories with an indexed vault can join.

Retrieval is scoped to what the asker may see. A member without access to a
repository does not get its code quoted back to them, and a user from another
workspace gets a 404 rather than a filtered list — the existence of the
repository is not disclosed either.

---

## Pull-request review

Point a repository's webhook at Celmis, or run a review by hand:

```bash
docker compose exec api analyzer review github owner/repo 42 --post
```

### What reads the diff

| Agent | Looks for | Model? |
|---|---|---|
| **defect** | bugs inside a single file — the logic in front of it | yes, reads the diff twice |
| **contract** | mismatches BETWEEN files: a caller and a callee that no longer agree | yes |
| **security** | injection, authz gaps, secret handling, unsafe deserialisation | yes |
| **structural** | ast-grep rules — patterns that are wrong by shape | no |
| **cve** | known vulnerabilities in the dependencies this PR changes | no |

Two more stages run after them: **breaking_change** (a regex-plus-graph pass
over public API changes) and **compliance** (one call per rule you have
written, if any).

The **verifier** is a sixth stage and is **off by default**: a second model
pass over every finding, which drops low-confidence ones and merges duplicates.
It is the slowest single call the pipeline makes, and whether it earns its price
is a judgement about one repository's tolerance for noise — switch it on per
repository at **Admin → Review policies**. The deterministic prefilter (exact
dedup, near-duplicate clustering, a rule deny-list, a confidence floor and the
severity sort) always runs and is not this.

### What a review will tell you about itself

A review that could not do its whole job says so in the comment it posts,
rather than returning fewer findings and letting them read as a clean diff:

- an agent that failed is named, **with the reason** — a timeout, an exhausted
  quota and a rejected key are three different problems with three different
  owners;
- a review that passed its wall-clock budget opens with `⚠ REVIEW CUT SHORT`
  and names the setting to raise;
- a diff over `REVIEW_MAX_DIFF_SIZE_BYTES` is refused outright, because a
  review of the first fifth of a change presented as a review of the change is
  worse than no review;
- the second pass of the defect agent failing is disclosed, because a review
  that read the diff once is thinner than one that read it twice.

### Per-repository policy

**Admin → Review policies** sets, for one repository: which agents run, which
model each one uses, per-agent prompt overrides, target branches, folder rules,
suppressed rule ids, and whether the verifier runs.

---

## Deterministic checks — no model, no false positives

Every check below is decided by reading files. No language model takes part in
deciding that something is wrong, so the false-positive rate is zero by
construction rather than by tuning.

That distinction is the whole point. Around twenty percent false positives is
where developers stop reading a tool's comments at all — one costs seconds of
attention, a thousand costs you a team that has learned to skip everything the
tool says. A model is used here to explain and to prioritise, never to detect.

| Check | Reads | Catches |
|---|---|---|
| `install_script` | `package.json` lifecycle hooks | a dependency that runs code at install time |
| `python_build_hooks` | `pyproject.toml` / `setup.py` | build-time code execution in a Python package |
| `cargo_build_script` | `Cargo.toml` | a crate with a `build.rs` |
| `non_registry` | manifests and lock files | a dependency pulled from a git URL or tarball instead of a registry |
| `suspect_name` | the dependency list | typosquats — a name one edit away from a popular package |
| `lock_drift` | manifest vs lock file | a lock file that no longer matches what the manifest declares |
| `cross_repo_drift` | the PR diff, then sibling repositories | a constant changed in one repository and left behind in the others |

Ordinary CVE scanning is deliberately **not** on that list. OSV-Scanner already
does it, it is free, and it is the de-facto standard — Celmis runs it (plus each
ecosystem's own auditor: `pip-audit`, `npm audit`, `govulncheck`,
`cargo audit`) and treats the result as an input rather than as a feature.

**On compliance.** Celmis produces the artefacts an audit asks for — a
CycloneDX SBOM, a dependency inventory, a findings history with timestamps and
the evidence each finding rests on. It **does not claim your filing is
adequate**, and no tool honestly can: what an auditor accepts depends on your
sector, your jurisdiction and your own controls. Produce the artefacts; let
the people whose job it is assess them.

---

## Connect Claude Code and other MCP clients

Celmis exposes its index over MCP, so an agent can search symbols, read API
surfaces and find consumers instead of grepping a checkout it does not have.

**Over HTTP** (the running stack serves it at `/mcp/`):

```bash
# Mint a token (or issue one from Settings → MCP in the UI)
docker compose exec api analyzer mcp issue-token \
  --scopes "read:graph read:groups" --duration 86400
```

```jsonc
// ~/.claude.json  (or .mcp.json in a project)
{
  "mcpServers": {
    "celmis": {
      "type": "http",
      "url": "http://localhost:8000/mcp/",
      "headers": { "Authorization": "Bearer <the token you just minted>" }
    }
  }
}
```

**Over stdio**, without the HTTP hop:

```jsonc
{
  "mcpServers": {
    "celmis": {
      "command": "docker",
      "args": ["compose", "exec", "-T", "api", "analyzer", "mcp", "serve"]
    }
  }
}
```

### What an agent can ask

The HTTP mount serves **18 tools**. They answer the questions a grep cannot:

| | |
|---|---|
| `list_workspace_repos` | which repositories exist, indexed, documented, auto-review on |
| `search_symbols` | where a function or endpoint is defined, across a whole project |
| `find_consumers` | which repositories call a symbol — including ones you never cloned |
| `get_api_surface` | the HTTP handlers a service actually exposes |
| `get_owner` · `list_deprecations` | who owns a file; what is on the way out and who still uses it |
| `route_incident` | given a stack trace, which repository and owner it belongs to |
| `bootstrap_client` · `start_integration_walk` | what a client needs to call another team's service |
| `get_dep_audit` · `list_dep_findings` | the last audit and its findings, worst first |
| `get_review` · `get_review_policy` | the latest review of a PR, and which agents run where |

**The two transports are not the same set.** `analyzer mcp serve` over stdio
serves 13 older, graph-shaped tools (`find_symbol`, `find_callers`,
`query_graph`); the HTTP mount serves the 18 above. Neither is a subset of the
other — pick the transport for the tools you want.

A step-by-step guide, with the scopes each tool needs and the failure modes,
is in [`.claude/skills/celmis-mcp/SKILL.md`](.claude/skills/celmis-mcp/SKILL.md).
Claude Code picks it up automatically when this repository is open.

---

### What the agent can ask for

Eight tools, served over Streamable HTTP at `/mcp/` and authenticated with the
same bearer token as `/api/`:

| Tool | Answers |
|---|---|
| `list_repos` | which repositories are indexed, and how fresh each index is |
| `list_groups` | which repositories are grouped together, so cross-repo questions have a scope |
| `find_symbol` | where a name is defined, across every indexed repository |
| `get_symbol` | the definition itself, with its file and line range |
| `find_callers` | what calls this — the question a grep answers badly and a graph answers exactly |
| `find_callees` | what this calls, one hop out |
| `cross_repo_edges` | calls that **cross a repository boundary** |
| `query_graph` | read-only Cypher, for questions the seven above do not shape |

`cross_repo_edges` is the one worth understanding, because it is the reason this
product carries a symbol graph at all. A diff-only reviewer — every tool in the
benchmark table above, including this one when the graph is empty — can tell you
that a function signature changed. It cannot tell you that a service in a
different repository still calls the old shape, because it never had that
repository open. Group the repositories once, and that question becomes
answerable:

```
> which services outside this repo call PaymentGateway.charge?
```

This is also why our benchmark rank understates the product rather than
describing it: the benchmark set is isolated single-repository pull requests, so
there is no sibling repository for an edge to cross. The capability is real and
the benchmark cannot see it — which is a statement about the benchmark, not a
claim you should take on faith. Point an MCP client at your own group and check.

## Configuration

`./scripts/init-env.sh` writes `.env` from [`.env.example`](.env.example) and
generates every secret. The example ships each secret **empty** on purpose: a
previous version put the generating command beside the variable, dotenv files
have no inline comments, and every install that copied it ran with a master
password printed in the repository.

Settings reach the containers only through the `environment:` block in
`docker-compose.yml` — the image carries no `.env`. A variable not named there
takes its code default no matter what your `.env` says.
`GET /healthz` reports the review clocks as the process actually resolved them,
which is how you check what arrived.

The clocks are documented as a set in `.env.example`, with the invariant that
binds them:

```
REVIEW_LLM_TIMEOUT_SECONDS × (1 + RETRY_FACTOR)  ≤  REVIEW_TIMEOUT_SECONDS
```

Raise one and the other has to follow; a test enforces it.

| Variable | Default | |
|---|---|---|
| `REVIEW_TIMEOUT_SECONDS` | 900 | wall clock for one review; past it the tail stages stand down and the comment says so |
| `REVIEW_LLM_TIMEOUT_SECONDS` | 300 | one model call. Raise to ~600 for a slow reasoning model |
| `REVIEW_LLM_TIMEOUT_RETRY_FACTOR` | 2.0 | how much longer the retry gets after a timeout; 1.0 disables the widening |
| `REVIEW_MAX_DIFF_SIZE_BYTES` | 500000 | larger diffs are refused, not truncated |
| `REVIEW_VERIFIER_ENABLED` | false | the LLM false-positive veto |
| `REVIEW_AGENT_CONCURRENCY` | 3 | provider calls in flight per review |
| `CELMIS_JOB_LEASE_SECONDS` | 600 | ceiling on worker silence before a job may be reclaimed |
| `CELMIS_DEPLOYMENT_MODE` | single_tenant | `multi_tenant` isolates workspaces from each other |

---

## Operations

```bash
docker compose logs -f api            # follow the API
docker compose exec api analyzer graph-stats <repo>   # what parsed, what did not
./scripts/backup.sh                   # Postgres + volumes
./scripts/restore.sh <archive>
```

**Admin → Monitoring** shows queue depth, spend per workspace and per-agent
model settings. **Usage & cost** breaks spend down by surface, so a batch
documentation build does not read as chat.

Deploy to a server is `./scripts/deploy-on-server.sh v0.1.0`, run **on the
server**: it pulls the published images, brings the stack up behind Caddy and
stamps the build the AGPL footer links to. Nothing outside that machine needs a
credential for it. See
[docs/ORACLE_CICD.md](docs/ORACLE_CICD.md), or
[docs/HETZNER.md](docs/HETZNER.md) for a plain VM.

---

## Local development

```bash
# Postgres and Qdrant from compose, everything else on the host
docker compose up -d postgres qdrant

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head
uvicorn src.api.main:app --reload --port 8000

cd web && npm install && npm run dev     # http://localhost:3000
```

```bash
pytest -q                # the suite
ruff check .             # lint, ratcheted at zero
cd web && npx tsc --noEmit
```

---

## CLI reference

`analyzer` is installed by `pip install -e .`; inside Docker use
`docker compose exec api analyzer …`. Every command takes `--help`.

| | |
|---|---|
| `analyzer init` | create the workspace layout |
| `analyzer index <path\|url>` | parse a repository into the graph |
| `analyzer ask "<question>"` | one question, cited answer |
| `analyzer chat` | interactive session |
| `analyzer review <provider> <repo> <pr>` | review a pull request; `--post` publishes |
| `analyzer generate` | build the documentation vault |
| `analyzer refresh` | re-index what changed |
| `analyzer graph-stats <repo>` | what parsed, per language |
| `analyzer serve` | the API without Docker |
| `analyzer review-serve` | the webhook receiver alone |

Grouped subcommands: `analyzer repo`, `analyzer group`, `analyzer auth`,
`analyzer mcp`, `analyzer scip`.

---

## Architecture

```
                      ┌──────────────┐
   GitHub / GitLab ──▶│   webhook    │──┐
   Bitbucket          └──────────────┘  │
                                        ▼
   Browser ──▶ web (Next.js) ──▶ api (FastAPI) ──▶ Postgres   jobs, policies, audit
                                     │              Qdrant     embeddings
                                     │              sandbox    untrusted execution
                                     ▼
                              model provider
                       (direct, or via a LiteLLM gateway)
```

- **Postgres** holds jobs, policies, run history, spend and the audit log. The
  durable job queue is a table — dequeue is `SELECT … FOR UPDATE SKIP LOCKED`,
  and a worker renews its lease while it works rather than guessing a duration
  up front.
- **Qdrant** holds embeddings, one collection per installation with workspace
  isolation enforced in the filter.
- **sandbox** runs anything untrusted — a test suite, a build — as its own uid
  on its own network, with no database, no keys and a read-only root.
- **LiteLLM** is optional. Set `LITELLM_PROXY_URL` and `LITELLM_MASTER_KEY`
  together and every call routes through the gateway; leave either empty and
  provider keys are used directly.

---

## Troubleshooting

**A container will not start.** `docker compose logs <service>`. The API says
at startup which optional features are unavailable and why, rather than failing
quietly.

**Reviews produce nothing.** Check `GET /healthz` for the resolved clocks, then
`docker compose logs api | grep agent_`. Each agent logs its elapsed time, its
model and its failure code.

**A timeout, not an outage.** `local_timeout` means this installation's own
deadline elapsed before the provider answered — raise
`REVIEW_LLM_TIMEOUT_SECONDS`. It is deliberately not reported as a provider
fault.

**Q&A cites nothing.** The repository is probably not indexed, or is indexed
without embeddings. **Repositories** shows the state of each; `analyzer
graph-stats <repo>` shows what parsed.

**The sandbox is always busy.** `SANDBOX_SLOTS` is how many jobs run at once
and is the knob that costs memory. `SANDBOX_SLOT_WAIT` is how long a caller
queues before being told to come back.

---

## Project layout

```
src/
  api/          FastAPI app, routers, schemas
  review/       PR review — agents, orchestrator, providers, policies
  indexing/     parsers, symbol graph, embeddings
  qa/           retrieval and answer composition
  generation/   documentation vault
  llm/          provider clients, error taxonomy, cost ledger
  sync/         git providers, the durable job queue, workers
  sandbox/      the isolated execution server
  mcp_server/   the MCP surface
  security/     redaction, patterns, log filtering
web/            Next.js UI (App Router, 16 locales)
tests/          5200+ tests
deploy/         Caddy overlay and the LiteLLM gateway config
docs/           deploy guides and the end-to-end walk-through
bench/          benchmark harness and results
```

---

## Provenance and rights

This repository has a single root commit over about a hundred thousand lines —
the shape a code drop of unclear origin has to a provenance scanner, and one
that needs an explanation rather than a shrug. It has one:
[PROVENANCE.md](PROVENANCE.md) states the licence position and the origin of
the code — development happened privately before this commit, and none of it
is needed to build, audit or fork what is here.

That file is a record of facts, not the licence. The licence is
[AGPL-3.0](LICENSE), with one exception: anything under `ee/`, and any file
whose name contains `.ee.`, is covered by [LICENSE_EE](LICENSE_EE) instead.
`ee/` holds no product code today — the boundary was drawn before the first
tag because adding it afterwards means re-asking every contributor who has
already sent work under an unqualified AGPL.

Everything shipped here is AGPL, including the parts that look commercial: the
audit console, usage and spend, compliance checks, installation metrics.
Security controls are never enterprise-only — the audit *log* is written under
AGPL and always will be. See [CONTRIBUTING.md](CONTRIBUTING.md) for where new
code goes.
