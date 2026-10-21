# celmis-code: Claude Code plugin for Celmis

Search and read code across all the repositories your Celmis token covers, from
Claude Code, with a small tool list and a hard token budget on every answer.

The plugin ships:

| Part | What it does |
|---|---|
| `.claude-plugin/plugin.json` | Declares the plugin's two settings (`userConfig`): the server address and your token. Claude Code asks for them when the plugin is enabled. |
| `.mcp.json` | Connects to the compact read-only endpoint `/mcp/dev/` (eleven tools) at the address you saved and sends your saved token as `Authorization: Bearer`. The plugin never contains a token. |
| `skills/celmis-search` | Teaches the order `repos` -> `find` -> `outline` -> `read_symbol` -> `refs`, `grep` for literals, `howto` for "do it like service X", `brief` then `skills` at the start of a task, and when not to use Celmis. |
| `agents/celmis-explore` | An optional subagent for open-ended exploration, so search output stays out of the main conversation. |
| `hooks/` | A start-up note, a one-time Grep nudge, and the dirty-guard (below). Fail-open, standard library only. |
| `bin/celmis-auth-header` | Not used by the plugin's own entry. A `headersHelper` for a server entry you write yourself (below): reads the token from the OS keychain (service `celmis-token`), else from `CELMIS_TOKEN`, and refuses an `http://` address that is not on this machine. |
| `bin/celmis-pack` | Optional: publishes, checks and fetches a knowledge pack from a folder, with a per-pack publish token (below). |

## Install

1. Ask your Celmis superadmin for a token (or make one on the web app's MCP
   settings page). Tokens are issued per person, for a list of repositories, and
   the value is shown once; keep it at hand for step 3.
2. Add the marketplace and install the plugin:

   ```text
   /plugin marketplace add Celmis-labs/Celmis
   /plugin install celmis-code@celmis
   ```

   For a local checkout of this repository use
   `claude --plugin-dir packaging/claude-plugin/celmis-code`. A plugin loaded that
   way has the id `celmis-code@inline`: use it in place of `celmis-code@celmis` in
   the `configure` commands below.
3. Claude Code asks for two values when the plugin is enabled:

   | Value | What to give |
   |---|---|
   | Celmis server address | The address the **Connect** page of the web app (Codebase section) shows: `https://`, no trailing slash, no `/mcp`. On the bundled deployment it ends in `/backend`, because the API (and `/mcp/dev/`) is served under that prefix, for example `https://celmis.example.com/backend`. |
   | Celmis token | The token from step 1. The input is masked. |

   Nothing needs to be exported in a shell or written in a settings file.
4. Restart Claude Code and run `/mcp`; `celmis` should be connected. Its tools
   appear as `mcp__plugin_celmis-code_celmis__<tool>`.

Until both values are set Claude Code does not start the `celmis` server at all:
`/mcp` has no `celmis` entry and the tools are absent, the start-up note says
what to do, and Claude Code carries on with Grep and Read. Nothing else in the
session breaks.

**Where the values are kept.** The address is not a secret: Claude Code writes it
under `pluginConfigs` in your user settings, `~/.claude/settings.json`. The token is
declared `sensitive`, so Claude Code keeps it in the macOS Keychain (falling back to
`~/.claude/.credentials.json` if the Keychain refuses the write); on Linux and
Windows it is in `~/.claude/.credentials.json` (mode 0600 on Linux). It
is in no settings file, no shell profile and no project file. The plugin's three hooks
are given the two values in their environment (as `CLAUDE_PLUGIN_OPTION_CELMIS_URL`
and `CLAUDE_PLUGIN_OPTION_CELMIS_TOKEN`) and only test that they are not empty. A
project's or a local `pluginConfigs` is ignored by Claude Code on purpose, so a
cloned repository cannot point the plugin at its own server.

**Change a value later.** Run `/plugin configure celmis-code@celmis` (or `/plugin`,
Installed, `celmis-code`, Configure options), then restart Claude Code. From a shell,
for example in a provisioning script, `claude plugin configure celmis-code@celmis
--values-stdin` reads a JSON object on its standard input; a key you leave out keeps
its value, so this replaces only the token, from the clipboard on macOS, and never
puts it in the process list or in your shell history (if your Claude Code does not
know `plugin configure`, update it with `claude update`):

```sh
printf '{"celmis_token":"%s"}' "$(pbpaste | tr -cd 'A-Za-z0-9._~+/=-')" | claude plugin configure celmis-code@celmis --values-stdin
```

It prints `Configuration saved. Restart Claude Code to apply it.` Do not hand the
token to `claude plugin install` through its `--config` option: a command-line
argument is visible to every process on the machine and stays in your shell history.

`CELMIS_URL` and `CELMIS_TOKEN` in the environment are not read by the plugin
any more (see "Upgrading from 3.0.1").

The address must be `https://`. Nothing in the plugin's entry checks the scheme
(Claude Code has no option type that does), so the bearer token goes wherever the
address you saved points. The start-up note warns when the address is not https and
the Grep nudge stays silent, but the server entry itself still connects: correct the
address with `/plugin configure`. Only a server on this machine, `localhost`,
`127.0.0.1` or `[::1]`, may reasonably use `http://`.

**Windows.** The plugin's server entry needs no script, so the two values work as on
the other systems. The hooks need Python 3 (`python3`, or `python`). The plugin is
not tested on native Windows; `bin/celmis-auth-header` (below) is a POSIX `sh` script
and needs WSL or Git for Windows.

### Upgrading from 3.0.1

3.0.1 read `CELMIS_URL` and `CELMIS_TOKEN` from the environment of the shell that
started Claude Code. 3.0.2 no longer reads those two variables: the plugin asks Claude
Code for its address and token instead (above), which keeps the token in the keychain
rather than in a shell profile and in the environment of every process Claude Code
starts. (Claude Code removes any variable whose name contains `TOKEN`, `SECRET`,
`PASSWORD`, `KEY` or `AUTH` from the environment of a plugin's `headersHelper`, so the
variable could not be handed to a helper either.) After the upgrade a setup that used
the variables shows no Celmis tools, and the start-up note says the plugin is not
configured. Run `/plugin configure celmis-code@celmis` once, give the address and the
token, restart Claude Code, and remove the two `export` lines (and the keychain entry
`celmis-token`, if you stored one) when nothing else of yours reads them.
`celmis-pack` is unchanged and still reads its own `CELMIS_URL`, `CELMIS_PACK_TOKEN`
and `celmis-token` (below).

### Using your own server entry

Prefer the plugin's entry: its tools are named `mcp__plugin_celmis-code_celmis__*`,
which the plugin's skill and agent use. A server you copy into your own settings is
named `mcp__celmis__*` instead, so they would not find it. If you still need your own
entry (another address, say, or a token that must come from a keychain or a script of
yours), Claude Code runs a `headersHelper` of a *user* or *local* server with your
full environment, which a plugin's or a project's server does not get. `${CLAUDE_PLUGIN_ROOT}`
exists only inside the plugin, so name the script by an absolute path. Install a copy at
a stable place and store the token where it looks:

```sh
install -m 0755 "<plugin directory>/bin/celmis-auth-header" ~/.local/bin/celmis-auth-header
```

On macOS copy the token, then run

```sh
printf 'add-generic-password -U -s celmis-token -a %s -w %s\n' "$USER" "$(pbpaste | tr -cd 'A-Za-z0-9._~+/=-')" | security -i && pbcopy </dev/null
```

On Linux, `secret-tool store --label celmis-token service celmis-token`; or
`export CELMIS_TOKEN=...` in the shell that starts Claude Code (the helper reads the
keychain first and the variable second, so a stale keychain entry wins over the
variable: remove it when you switch). Do not type the value at the prompt of
`security`: it keeps only the first 128 characters, which cuts an MCP token. The
helper and `celmis-pack verify` refuse a token that looks cut short (it starts with
`eyJ` but is not three parts joined by dots) and say so, with its length and never its
value.

```json
{
  "mcpServers": {
    "celmis": {
      "type": "http",
      "url": "https://celmis.example.com/backend/mcp/dev/",
      "headersHelper": "\"/home/you/.local/bin/celmis-auth-header\""
    }
  }
}
```

Use the real absolute path of your home directory (no `~`, no variables) and keep
the quotes inside the string: the command runs through a shell, so a path with a
space in it, such as `/Users/Ann Lee/...`, is only one word when quoted. Set
`CELMIS_URL` in the environment of Claude Code if you want the helper to refuse an
`http://` address (it checks `CELMIS_URL`, not the `url` of your entry). Disable the
plugin's own server entry in `/mcp` when you do this, or the two will both connect. The
hooks look only at the plugin's own two options, so with your entry alone they say the
plugin is not configured; the tools still work.

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

Each developer is then asked for the address and the token the first time the
plugin is enabled. A token is per person and goes only into that person's own
prompt (stored in their keychain), never into the shared settings file. The address is
the same for everyone and is not a secret; Claude Code's documentation lists
`pluginConfigs` among the settings read from user and managed settings, so an
administrator can seed `"pluginConfigs": { "celmis-code@celmis": { "options": {
"celmis_url": "https://celmis.example.com/backend" } } }` in managed settings and
leave only the token to ask for. (A project's or local `pluginConfigs` is ignored, so
that does not work in a project's `.claude/settings.json`; managed seeding was not
tried here.)

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
`celmis-token`, else `CELMIS_TOKEN` — the order of `celmis-auth-header`, the helper
for your own server entry. The plugin's token is kept by Claude Code, which gives it
to no other program, so `verify` cannot read it: to check what your agent sees, keep
the same token in one of those two places as well. A stale keychain entry wins over
the variable, so remove it when you switch to `CELMIS_TOKEN`. Neither is ever
printed. An `http` address is refused (except `localhost`, `127.0.0.1`,
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
  local repository and commit, and, only when the plugin is not configured (the
  address or the token option is empty), `/plugin configure celmis-code@celmis`. When
  the saved address is not `https://` it says so instead. No network.
- **PreToolUse (Grep, Glob)** adds a single nudge per session when the pattern
  looks like a symbol name and the plugin is configured. It never blocks a search.
- **PostToolUse (Celmis tools), the dirty-guard.** An answer is true at the
  indexed commit; your working tree may differ. When the answer is about the
  repository you have checked out, the hook diffs the hit files against the
  indexed commit and adds `[stale-locally] <file> - read locally` for the ones
  that differ (including untracked files). When the indexed commit is not in
  your clone it says so. Paths from the answer reach git only as arguments
  after `--` with literal pathspecs, never through a shell.

Claude Code gives each hook the plugin's two options in its environment
(`CLAUDE_PLUGIN_OPTION_CELMIS_URL` and `CLAUDE_PLUGIN_OPTION_CELMIS_TOKEN`). The hooks
ask whether the token is there, never what it is: it is tested for being non-empty
and is not printed, logged or put in the context sent to the model. They look nowhere
else. `CELMIS_URL` and `CELMIS_TOKEN` in your shell, or a keychain item of yours,
do not make the plugin "connected", because the plugin's tools do not use them.

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
| No `celmis` server in `/mcp`, no Celmis tools, and the start-up note says the plugin is not configured | The address or the token option is not set yet (Claude Code starts the server only when both are), or you upgraded from 3.0.1 and the plugin no longer reads `CELMIS_URL` / `CELMIS_TOKEN`. Run `/plugin configure celmis-code@celmis`, then restart Claude Code. `claude plugin configure celmis-code@celmis` lists which options are not set. |
| `claude plugin install` ends with `userConfig options not yet set` | The same: the install worked, the two values are still to give. |
| `/mcp` shows `celmis` failed with `Server rejected the configured Authorization header (HTTP 401)` | The server refused the saved token: it is wrong, cut short, revoked or expired. Get a new one from your Celmis superadmin and replace it with `/plugin configure celmis-code@celmis`. A token that starts with `eyJ` and is not three parts joined by dots was pasted incomplete. With this header set, Claude Code does not fall back to a sign-in flow. |
| `/mcp` shows `celmis` failed to connect | With HTTP 404, the saved address points at the wrong place: it has `/mcp` or a trailing slash in it, or it lacks the `/backend` part on the bundled deployment. With no HTTP status, the server is not reachable from this machine (address, DNS, VPN or firewall). Compare the address with the Connect page. |
| A change made with `/plugin configure` has no effect | It applies after a restart of Claude Code. |
| The start-up note says the address is not https | Change it with `/plugin configure celmis-code@celmis`. Only `localhost`, `127.0.0.1` and `[::1]` should use `http://`; nothing in the plugin's entry enforces it. |
| A repository you expect is missing from `repos` | Your token does not list it, or it is not indexed. Ask the superadmin; there is no way to see why from here. |
| `howto` shows `[REDACTED:...]` | The code holds a literal credential. Do not copy it; take the value from the source `howto` names. |
| No `[stale-locally]` after you edited a file | The hit was in another repository, or the file is not among the answer's hits. |
| Your own server entry with `celmis-auth-header` fails | Claude Code does not show the helper's message. Run `celmis-auth-header >/dev/null` in a shell where `CELMIS_URL` is set: it prints the reason on stderr, exits 1, and the token goes to the discarded stdout. `no token in the keychain (service celmis-token) or in CELMIS_TOKEN`: store it as in "Using your own server entry" (the publish token of `celmis-pack` lives under another service, `celmis-pack-token`, and is not used here). `the token looks cut short`: it was typed at the prompt of `security`; run the `printf ... \| security -i` command again, `-U` replaces the entry. `CELMIS_URL is not https`: use the `https://` address. |
