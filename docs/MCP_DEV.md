# The compact developer profile (`/mcp/dev`)

A second MCP endpoint next to `/mcp`, shaped for a coding assistant that asks
many small questions a day: nine read-only tools, plain-text answers, a tool
list under 6,000 characters.

```
claude mcp add --transport http celmis https://celmis.example.com/mcp/dev \
  --header "Authorization: Bearer $CELMIS_TOKEN"
```

The token needs the scope `read:code`. A token without it is refused with 403,
and every tool checks again. `/mcp` is unchanged.

The token is issued by the superadmin for one person with an explicit list of
repositories (see [mcp-access.md](mcp-access.md)). A repository that is not on
the list, or that has no access rule yet, does not exist for the caller: every
tool answers "repo not found or not accessible" for both. A dev token cannot
call the full `/mcp` tools, and every call is audited (facts, never content).

## Tools

| Tool | Use it for |
|---|---|
| `repos` | Which repositories you can read, at what level, how fresh. Call first. |
| `find` | Ranked symbol search across your repos (exact, prefix, token, substring, fuzzy). |
| `outline` | The symbols of a file, or files and top symbols of a directory. |
| `read_symbol` | One symbol's body from the indexed commit, middle elided past `max_lines`. |
| `refs` | Callers, callees or importers; text mentions in sibling repos. |
| `grep` | A literal or regex in committed text; secret files are never searched. |
| `map` | The layout of a repository, ordered by how much code each directory holds. |
| `ask` | A written answer from the Q&A pipeline. Paid, rate limited. |

## How to read an answer

The first line is always `idx: <repo> <branch>@<sha> <age> fresh|STALE|unknown`.
Line numbers refer to that revision, which can differ from your checkout: for
files you are editing, and for literals inside the repository you are working
in, use local search. `STALE` means the remote has moved on since the index was
built.

A list that was cut ends with `… +N more · cursor=<token> · narrow with <hint>`.
Pass the cursor back to get the next page (it is about 20 characters). A cursor
from before a re-index of a repository that produced the hits, or for another
question, returns the first page with a note.

## What is and is not returned

* A repository you cannot read is absent everywhere, and naming it answers like
  a repository that does not exist.
* Metadata-level access lists symbol locations but never bodies or grep lines.
* Paths denied by a rule never appear in any tool.
* Secret files (`.env`, keys, `secrets/`) are excluded by git itself, and all
  body text passes one redaction step before it is printed, before it is cut to
  a line length. Besides tokens, keys and DSNs, that step masks a literal next to a
  secret-looking name (`DB_PASSWORD=...`, `password: ...`, `password='...'`,
  `redis://:...@host`); a pointer (`${DB_PASSWORD}`, `settings.db_password`) stays,
  so the pattern is readable without the value.

## Freshness

A push to the tracked branch re-indexes the repository: GitHub and Bitbucket
(`repo:push`) hooks go through the same freshness check as the daily sweep. The
hook installer subscribes Bitbucket to `repo:push` for new installs; an existing
Bitbucket hook needs the event added once (reinstall or repair the webhook).

## Use it from Claude Code (plugin)

The repository ships a plugin, `packaging/claude-plugin/celmis-code`, that connects
`/mcp/dev/`, teaches the search order (`repos`, `find`, `outline`, `read_symbol`,
`refs`; `howto` for "do it like service X") and adds three small hooks. It holds no
token: the endpoint and the token come from two environment variables.

```bash
export CELMIS_URL=https://celmis.example.com     # no trailing slash, no /mcp
export CELMIS_TOKEN=...                          # issued by your superadmin, shown once
```

```text
/plugin marketplace add Celmis-labs/Celmis
/plugin install celmis-code@celmis
```

From a checkout: `claude --plugin-dir packaging/claude-plugin/celmis-code`. Run `/mcp`
and check that `celmis` is connected. The plugin's README covers the keychain helper,
rolling it out to a team, and why there is no local proxy.

## "Do it like service X" (`howto`)

The requests developers make most are about another service: "make a database
connection like in service X", "find how X authorises requests and do the same".
`howto(topic, repo)` answers with the code to copy (connection construction, config
loader, middleware), the NAMES of the variables and secret references it reads, and
where each value comes from (`.env.example`, compose environment names, pipeline
variable names, a vault path). It never returns a value. The last line tells the agent
what to do next: copy the pattern, take the values from the listed source, ask the user
or ops. See [mcp-howto.md](mcp-howto.md) for the topics and the redaction rules.

## What it costs

Every answer is cut to a token budget, so the cost is bounded: the tool list is about
1,300 tokens, a `find`, `outline`, `read_symbol`, `refs` or `grep` answer is typically
40 to 150 tokens (hard ceiling about 4,000), and a `howto` answer 500 to 1,000. The
alternative is an agent that greps and reads whole files: on three real services of
60 to 330 files each, the same three requests (database connection, credentials, auth)
cost about 85% fewer tokens through Celmis. On a very small repository the saving is
small or negative, because reading it whole is cheap; the gain grows with the size of
what would otherwise be read.

## Limits per token

The work behind an answer is limited too, per token (a person with two tokens has two
budgets): `CELMIS_MCP_RATE_PER_MINUTE` calls per minute (default 120, `0` switches it
off), `CELMIS_MCP_MAX_CONCURRENT` calls at once (default 4) and
`CELMIS_MCP_CALL_TIMEOUT_SECONDS` per call (default 60). A call over a limit is
answered with a short message that says what to narrow or when to retry, and is
audited as denied. One call searches at most 40 repositories; name one with `repo=` to
go deeper.

## Checking an installation end to end

`scripts/dev_mcp_journey.py` starts Celmis in-process on a throwaway database and drives
it over real HTTP: users in six roles, teams and rules, tokens issued by a superadmin
(two developers with different repositories, one expired, one revoked), a real MCP
client running the developer scenarios against a grep-and-read baseline, and a security
matrix (another developer's repository absent from every tool, expired and revoked
tokens refused, repositories without a rule invisible, write refused, no planted secret
in any output, log or audit row).

```bash
scripts/dev_mcp_journey.py --out ~/.cache/celmis-journey/run1
scripts/dev_mcp_journey.py --real svc-a,svc-b --src-root ~/code --out ~/private/journey
```

`--real` indexes `git clone file://` copies of your own services and runs the three
requests on them. Its output must stay outside this repository (the script refuses
otherwise) and it never prints a secret value: a leak is reported as a file path and a
variable name. To check a running server instead, use `scripts/dev_mcp_e2e.py --url`.
