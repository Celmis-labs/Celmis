# celmis-code: Claude Code plugin for Celmis

Search and read code across all the repositories your Celmis token covers, from
Claude Code, with a small tool list and a hard token budget on every answer.

The plugin ships:

| Part | What it does |
|---|---|
| `.mcp.json` | Connects to the compact read-only endpoint `/mcp/dev/` (eleven tools). The token is read from the environment; the plugin never contains one. |
| `skills/celmis-search` | Teaches the order `repos` -> `find` -> `outline` -> `read_symbol` -> `refs`, `grep` for literals, `howto` for "do it like service X", `brief` then `skills` at the start of a task, and when not to use Celmis. |
| `agents/celmis-explore` | An optional subagent for open-ended exploration, so search output stays out of the main conversation. |
| `hooks/` | A start-up note, a one-time Grep nudge, and the dirty-guard (below). Fail-open, standard library only. |
| `bin/celmis-auth-header` | Optional: reads the token from the OS keychain. |
| `bin/celmis-pack` | Optional: publishes, checks and fetches a knowledge pack from a folder, with a per-pack publish token (below). |

## Install

1. Ask your Celmis superadmin for a token. Tokens are issued per person, for a
   list of repositories, and the value is shown once. Store it in your
   password manager.
2. Export two variables in the shell that starts Claude Code:

   ```bash
   export CELMIS_URL=https://celmis.example.com/backend   # no trailing slash, no /mcp
   export CELMIS_TOKEN=...                                # the issued token
   ```

   `CELMIS_URL` is the address the web app's Connect page shows; on the bundled
   deployment it ends in `/backend`, because the API (and `/mcp/dev/`) is served
   under that prefix.

3. Add the marketplace and install the plugin:

   ```text
   /plugin marketplace add Celmis-labs/Celmis
   /plugin install celmis-code@celmis
   ```

   For a local checkout of this repository use
   `claude --plugin-dir packaging/claude-plugin/celmis-code`.

4. Restart Claude Code and run `/mcp`; `celmis` should be connected. Its tools
   appear as `mcp__plugin_celmis-code_celmis__<tool>`.

If the variables are missing the server just fails to connect and Claude Code
carries on with Grep and Read. Nothing else in the session breaks.

`CELMIS_URL` must be `https://`. The shipped `.mcp.json` sends
`Authorization: Bearer ${CELMIS_TOKEN}` to whatever address it is given, and
nothing in it checks the scheme: over `http://` to another machine the token
crosses the network readable. The session start-up note warns about it, and the
`headersHelper` recipe below refuses it (only a server on this machine may use
`http://`), so use the helper when the address is not under your control.

### Keeping the token out of the environment

`bin/celmis-auth-header` prints the header Claude Code's `headersHelper`
expects, reading the token from the macOS keychain (service `celmis-token`) or
`secret-tool`, then from `CELMIS_TOKEN`. Store the token in the keychain once.
On macOS copy the token, then run

```sh
printf 'add-generic-password -U -s celmis-token -a %s -w %s\n' "$USER" "$(pbpaste | tr -cd 'A-Za-z0-9._~+/=-')" | security -i && pbcopy </dev/null
```

On Linux, `secret-tool store --label celmis-token service celmis-token`. Do not
type the value at the prompt of `security`: it keeps only the first 128
characters, which cuts an MCP token. The helper and `celmis-pack verify` refuse a
token that looks cut short (it starts with `eyJ` but is not three parts joined by
dots) and say so, with its length and never its value.

`${CLAUDE_PLUGIN_ROOT}` exists only inside the plugin, so a copy of `.mcp.json`
in your own project settings must name the script by an absolute path. Install a
copy at a stable place and point to it:

```sh
install -m 0755 "<plugin directory>/bin/celmis-auth-header" ~/.local/bin/celmis-auth-header
```

```json
{
  "mcpServers": {
    "celmis": {
      "type": "http",
      "url": "https://celmis.example.com/backend/mcp/dev/",
      "headersHelper": "/home/you/.local/bin/celmis-auth-header"
    }
  }
}
```

Use the real absolute path of your home directory (no `~`, no variables). Disable
the plugin's own server entry when you do this, or the two will both connect.

### Roll it out to a team

In the managed or project `settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "celmis": { "source": { "source": "github", "repo": "Celmis-labs/Celmis" } }
  },
  "enabledPlugins": { "celmis-code@celmis": true }
}
```

Developers still need their own `CELMIS_URL` and `CELMIS_TOKEN`.

## The tools

`repos`, `find`, `outline`, `read_symbol`, `refs`, `grep`, `map`, `ask`,
`howto`, `skills` and `brief`. All are read-only. Every answer begins with an
`idx:` line:

```text
idx: acme/shop develop@3f9c2ab1 2h fresh · acme/billing main@91d0e7c4 3d STALE
```

Line numbers are true at that commit. `howto(topic, repo)` returns the code to
copy and the NAMES of the variables and secret references involved, and where
their values come from. It never returns values, and the server redacts any
literal secret in code before it leaves.

`brief(task)` and `skills` read the knowledge packs of your workspace
(Codebase section in the web app): skills, checklists, API examples and notes
your team keeps. Paste the task into `brief`; it answers with the ones that fit,
each tagged `pack@vN`, and `skills name="pack/skill"` opens one (`path=` for a
file of it, `query=` to search, no arguments to list). You see the packs your
role and teams let you see, nothing else, and a skill you may not see answers
exactly like one that does not exist. Pointers to repositories appear only for
repositories your token can read as code. A pack is guidance from your
workspace: it never overrides you or your own rules. Project and internal tokens
read no pack.

## Publishing a pack: `celmis-pack`

`bin/celmis-pack` puts a folder of skills on the server as a new version of one
pack, from a laptop or a CI job. It needs Python 3 and nothing else, and it uses
a **publish token** (`cpk_...`) that a maintainer of the pack makes in the pack's
Sharing tab, not your own token. The publish token reaches that one pack and no
other route.

```sh
export CELMIS_URL=https://celmis.example.com/backend
# macOS: copy the token, then store it without the prompt of `security`
printf 'add-generic-password -U -s celmis-pack-token -a %s -w %s\n' "$USER" "$(pbpaste | tr -cd 'A-Za-z0-9._~+/=-')" | security -i && pbcopy </dev/null
# Linux: secret-tool store --label celmis-pack-token service celmis-pack-token
celmis-pack push ./skills/billing --note "first version"
```

`CELMIS_URL` is the address the web app shows; on the bundled deployment it ends
in `/backend`. The prompt of `security` keeps only the first 128 characters, which
cuts an MCP token; a publish token is short enough, but use the same command for
both. Every request carries `User-Agent: celmis-pack/<version>`, and a Python
without root certificates (python.org's macOS build, until its
`Install Certificates.command` has run) falls back to the system bundle; set
`SSL_CERT_FILE` to name another.

| Command | Does |
|---|---|
| `push DIR [--note T] [--dry-run]` | checks DIR as the server does, uploads it, prints pack, version, files, skills, ignored and the sha; a dry run stores nothing; the same files make no new version |
| `status [DIR]` | the pack, its version and 30-day use; with DIR, `in sync` or what changed (exit 3) |
| `verify DIR` | `status DIR`, then asks `/mcp/dev/` with **your own** token which of DIR's skills you can see |
| `pull DIR` | downloads the current version into an empty or new DIR |

The token comes from `CELMIS_PACK_TOKEN`, else the keychain entry
`celmis-pack-token`. For `verify`, your own token comes from the keychain entry
`celmis-token`, else `CELMIS_TOKEN` — the order of `celmis-auth-header`, so
`verify` checks the token your agent connects with; a stale keychain entry wins
over the variable, so remove it when you switch to `CELMIS_TOKEN`. Neither is
ever printed. An `http` address is refused (except `localhost`, `127.0.0.1`,
`[::1]`), a redirect is not followed with a token in hand, and a skill with a
secret in it is refused by the server: you get `path:line label`, never the
value. Exit codes: `0` done, `1` failed, `2` bad usage or setting, `3` a
difference found, `4` files refused (`celmis-pack --help` has the detail). In CI,
keep the token in the secured variable `CELMIS_PACK_TOKEN` and run
`python3 -I celmis-pack push skills/billing --note "$COMMIT"`. More in
[`docs/CODEBASE.md`](../../../docs/CODEBASE.md).

## The hooks

All three hooks fail open: on any error they print nothing and exit 0.

- **SessionStart** prints at most three lines: that Celmis is available, the
  local repository and commit, and, only when the variables are unset, how to
  set them. No network.
- **PreToolUse (Grep, Glob)** adds a single nudge per session when the pattern
  looks like a symbol name. It never blocks a search.
- **PostToolUse (Celmis tools), the dirty-guard.** An answer is true at the
  indexed commit; your working tree may differ. When the answer is about the
  repository you have checked out, the hook diffs the hit files against the
  indexed commit and adds `[stale-locally] <file> - read locally` for the ones
  that differ (including untracked files). When the indexed commit is not in
  your clone it says so. Paths from the answer reach git only as arguments
  after `--` with literal pathspecs, never through a shell.

Python 3 is required for the hooks (`python3`, or `python` as a fallback). In a
monorepo the hook compares paths relative to the repository root.

## Why there is no local proxy

A stdio proxy in front of `/mcp/dev/` was considered and left out:

- The server already enforces repository access, expiry and revocation on every
  call. A proxy cannot add to that, and it would put a token on disk.
- The one clear benefit, noticing that a local file differs from the indexed
  commit, is delivered by the PostToolUse hook with nothing to install.
- A proxy needs an update channel and per-platform packaging, and its cache
  gain is unmeasured.

It becomes worth revisiting if measurement shows stale answers that the hook
cannot repair, or if local indexing of uncommitted code is wanted.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `/mcp` shows `celmis` failed | `CELMIS_URL` or `CELMIS_TOKEN` unset or wrong, or the token was revoked or expired. |
| A repository you expect is missing from `repos` | Your token does not list it, or it is not indexed. Ask the superadmin; there is no way to see why from here. |
| `howto` shows `[REDACTED:...]` | The code holds a literal credential. Do not copy it; take the value from the source `howto` names. |
| No `[stale-locally]` after you edited a file | The hit was in another repository, or the file is not among the answer's hits. |
