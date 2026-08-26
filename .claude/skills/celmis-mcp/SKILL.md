---
name: celmis-mcp
description: Connect Claude Code (or any MCP client) to a running Celmis instance and query the code index — symbols, callers, cross-repo edges, dependency audits — instead of grepping a checkout. Use when setting up the MCP connection, when an MCP call fails, or when deciding which Celmis tool answers a question.
---

# Querying Celmis over MCP

Celmis indexes repositories into a symbol graph and exposes it over MCP. An
agent connected to it can ask **which symbols exist**, **who calls this**, and
**what breaks if I change it** — across repositories it has never cloned.

Prefer these tools to `grep` when the question is about structure. Grep finds a
string; the graph finds the callers, including the ones that reach a symbol
under an alias or through a re-export.

**THE TWO TRANSPORTS EXPOSE DIFFERENT TOOLS.** This is the first thing to know
and it surprises everyone: the HTTP mount at `/mcp/` serves **18** tools built
for multi-repo work (projects, API surfaces, ownership, incident routing);
`analyzer mcp serve` over stdio serves **13** older, graph-shaped ones
(`find_symbol`, `find_callers`, `query_graph`). Neither is a subset of the
other. Sections 4 and 5 below describe the HTTP set, which is what a
Claude Code client normally connects to.

---

## 1. Is the stack running?

```bash
curl -s localhost:8000/healthz
```

`{"status":"ok",…}` means the API is up. If it is not, start it:

```bash
docker compose --env-file .env up -d
```

MCP is mounted at `/mcp/` on the same port.

---

## 2. Get a token

Two ways. Both mint a JWT; the difference is who signs it.

**From the UI** — Settings → MCP → issue a token. Scoped to your account and
its workspace. This is the one to use day to day.

**From the CLI** — for local development:

```bash
docker compose exec api analyzer mcp issue-token \
  --subject default \
  --scopes "read:graph read:groups" \
  --duration 86400
```

Requires `MCP_JWT_SECRET` (or `CELMIS_JWT_SECRET`) in the environment; the
command fails loudly if neither is set rather than issuing something unsigned.

### Scopes

The scope names come from the stdio server's decorators and are what
`issue-token` accepts:

| Scope | Opens |
|---|---|
| `read:graph` | the symbol and caller lookups |
| `read:groups` | listing repositories and projects |
| `write:repos` | registering a repository, starting an audit |
| `review:pr` | reading and running reviews |

Ask for the narrowest set that answers your question. A read-only token cannot
register a repository or spend money on a review, which is the point.

A token minted from the UI is scoped to your account and its workspace, and it
carries what that account may reach — `get_my_access` reports the result.

---

## 3. Configure the client

### HTTP (recommended — the stack is already serving it)

```jsonc
// ~/.claude.json for every project, or .mcp.json in one
{
  "mcpServers": {
    "celmis": {
      "type": "http",
      "url": "http://localhost:8000/mcp/",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The **trailing slash on `/mcp/` is required**. Without it Starlette answers
307, and a redirected POST is not something the streamable-HTTP client is
guaranteed to follow.

### stdio (no HTTP hop)

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

`-T` matters: without it Compose allocates a TTY and the JSON-RPC framing
breaks.

Restart the client after editing the config, then confirm with `/mcp`.

---

## 4. The tools

Eighteen over HTTP, verified against a live instance on 2026-08-25.

### Finding your way in

| Tool | Answers |
|---|---|
| `list_workspace_repos` | the OPERATIONAL state of every repository: indexed, documented, auto-review on or off, which branch |
| `list_projects` | which repositories are grouped as one product |
| `get_project` | one project's repos, roles and description |
| `list_accessible_repos` | what THIS caller may research, and at what visibility |
| `get_my_access` | the caller's effective access to one repository |

Start with `list_workspace_repos`. It returns a `repo_slug` for each — every
other tool takes that slug, never a path.

### Reading the graph

| Tool | Answers |
|---|---|
| `search_symbols` | does a function/class/endpoint by this name exist, and where (needs `project_id`) |
| `find_consumers` | which repositories call a given fully-qualified symbol |
| `get_api_surface` | the HTTP/RPC handlers detected in a repository — paths, methods, handlers |
| `get_architecture` | the cached architecture summary, as markdown |
| `get_owner` | who owns a file, falling back to its nearest owned ancestor |
| `list_deprecations` | every tracked deprecated symbol and who still consumes it |

`find_consumers` is the one with no local equivalent: it is how you learn that
a backend handler is called by a mobile client in a repository you have never
cloned. `get_architecture` returns `{"note": "no summary yet — trigger
rebuild"}` rather than an empty string pretending to be an answer.

### Integration and incidents

| Tool | Does |
|---|---|
| `bootstrap_client` | given a project and a target repository, what a client needs to call it |
| `start_integration_walk` | an ordered checklist for a cross-team integration |
| `route_incident` | given a stack trace, which repository and owner it belongs to |

### Review and dependencies

| Tool | Does |
|---|---|
| `get_review` | the latest review run for a PR reference like `github:owner/repo#42` |
| `get_review_policy` | the effective policy for a repository — which agents run, which models |
| `get_dep_audit` | status and summary of an audit; **omit `run_id`** for the latest |
| `list_dep_findings` | findings of one run, worst severity first. `run_id` is REQUIRED here — unlike `get_dep_audit` |

That asymmetry catches people, including this document's first draft: the
"omit run_id" sentence belongs to `get_dep_audit` alone.

---

## 5. Using it well

**Ask the graph, not the filesystem.** "Who calls this?" is `find_consumers`,
not a grep. The grep misses aliased imports and finds every mention in a
comment.

**Slug, not path.** Every tool takes the `repo_slug` from
`list_workspace_repos` (`github_owner-name`), never a directory.

**An empty result is an answer.** `find_consumers` returning nothing means
nothing calls it — that is the fact you wanted. Do not fall back to grep to
disagree with it; check `analyzer graph-stats <repo>` if you suspect the file
was never parsed.

**`search_symbols` needs a `project_id`, not a repo.** It searches every
repository in a project at once, which is the point of it. Get the id from
`list_projects`.

**Cross-repo answers need a project.** Repositories have to be grouped into one
before `find_consumers` can see an edge that crosses between them.

---

## 6. When it fails

| Symptom | Cause |
|---|---|
| `401` / `invalid token` | expired (default lifetime 1 hour) — mint another |
| `403` / `missing scope` | the token lacks the scope that tool needs — see the table above |
| `307` then nothing | the trailing slash is missing from `/mcp/` |
| server never starts (stdio) | `-T` missing from `docker compose exec` |
| `mcp_auth_unavailable` in the API log | neither `MCP_JWT_SECRET` nor `CELMIS_JWT_SECRET` is set |
| `search_symbols` finds nothing | wrong `project_id`, wrong spelling, or the language has no parser |
| `Field required [type=missing]` | the tool needs an argument you omitted — the message names it |
| a tool you expected is absent | you are on the other transport; see the note at the top |
| tools missing from `/mcp` | client not restarted after the config edit |

Read the API's own account of it:

```bash
docker compose logs api | grep -i mcp | tail -20
```
