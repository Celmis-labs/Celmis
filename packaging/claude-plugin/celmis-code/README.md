# celmis-code: Claude Code plugin for Celmis

Search and read code across all the repositories your Celmis token covers, from
Claude Code, with a small tool list and a hard token budget on every answer.

The plugin ships:

| Part | What it does |
|---|---|
| `.mcp.json` | Connects to the compact read-only endpoint `/mcp/dev/` (nine tools). The token is read from the environment; the plugin never contains one. |
| `skills/celmis-search` | Teaches the order `repos` -> `find` -> `outline` -> `read_symbol` -> `refs`, `grep` for literals, `howto` for "do it like service X", and when not to use Celmis. |
| `agents/celmis-explore` | An optional subagent for open-ended exploration, so search output stays out of the main conversation. |
| `hooks/` | A start-up note, a one-time Grep nudge, and the dirty-guard (below). Fail-open, standard library only. |
| `bin/celmis-auth-header` | Optional: reads the token from the OS keychain. |

## Install

1. Ask your Celmis superadmin for a token. Tokens are issued per person, for a
   list of repositories, and the value is shown once. Store it in your
   password manager.
2. Export two variables in the shell that starts Claude Code:

   ```bash
   export CELMIS_URL=https://celmis.example.com      # no trailing slash, no /mcp
   export CELMIS_TOKEN=...                           # the issued token
   ```

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
`secret-tool`, then from `CELMIS_TOKEN`.

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
      "url": "https://celmis.example.com/mcp/dev/",
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

`repos`, `find`, `outline`, `read_symbol`, `refs`, `grep`, `map`, `ask` and
`howto`. All are read-only. Every answer begins with an `idx:` line:

```text
idx: acme/shop develop@3f9c2ab1 2h fresh · acme/billing main@91d0e7c4 3d STALE
```

Line numbers are true at that commit. `howto(topic, repo)` returns the code to
copy and the NAMES of the variables and secret references involved, and where
their values come from. It never returns values, and the server redacts any
literal secret in code before it leaves.

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
