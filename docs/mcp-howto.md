# howto and the MCP redaction layer

`howto(topic, repo)` answers "how does this repo do X". Topics: `db`, `auth`,
`config`, `http_client`, `logging`, `messaging`, `cache`.

The answer has code slices, the names of the environment variables and settings
the code reads, and where each value comes from (file and line of the
`.env.example` entry, compose service, `secretKeyRef`, CI variable). It never has a
value. The last lines say: copy this pattern, get the values from the sources
listed, ask the user or ops.

- Scope: `read:graph` on `/mcp/`, which every token kind carries (standard, personal access
  token, OAuth); `read:code` on `/mcp/dev/`, the only scope a dev token has. A token limited
  to other scopes does not see the tool. What a caller may read
  is decided by the access rules, not by the scope.
- Access: only repos the caller may read as code are candidates, and `deny_globs`
  hide paths. A repo that is denied and one that does not exist give the same reply.
- First line: `idx: <slug> <branch>@<sha> <age> fresh|STALE|unknown`.
  `set_idx_provider(fn)` lets another server supply it.
- Other servers register the tool with `register_howto(mcp, scoped=...)`.

## Redaction

Every tool output passes `src/mcp_server/output_guard.py::redact_result`, which calls
`src/security/mcp_redact.py::redact_for_mcp`. It is always on. If it fails, the
output is replaced with `output withheld: redaction failed`.

`src/security/secret_files.py` decides which files are never indexed or read
(`deny`) and which show names only (`keys_only`). Add patterns per deployment with
`SECRET_PATH_GLOBS_EXTRA`.

### Which files are secret

- Verdicts: `deny` (never shown or indexed), `keys_only` (an example env file: names
  kept, values withheld) and `ok`.
- Name rules are applied to the NFKC-normalised, case-folded path, so `ſecrets.json`,
  `.ENV` and a fullwidth `.env` equal the original.
- `credentials.<data extension>` and the bare `credentials` are secret;
  `credentials.py|ts|java|go|rs|rb|cs|md|rst` are not.
- `secret/` and `private_keys/` are also ordinary package names, so they hide only data
  files; source and prose are shown. `secrets/`, `.secrets/`, `.ssh/`, `.gnupg/`,
  `.aws/`, `.kube/` and `.git/` hide everything below them. Other stores hidden by name
  include `.dockerconfigjson`, `*.jceks`, `*.bks`, `.boto`, `.azure/accessTokens.json`,
  `.config/gh/hosts.yml`, `local.settings.json`, `sftp-config.json`, `.ftpconfig`,
  `.yarnrc`, `.yarnrc.yml`, `settings-security.xml`, `.m2/settings.xml`,
  `.composer/auth.json` and `auth.json` at the repository root only (a translation file
  such as `locales/en/auth.json` stays visible). `.yarnrc*` and `.m2/settings.xml` are
  hidden whole because their registry logins are YAML, XML or space-separated, which the
  example-file masker cannot keep safe.
- Also hidden by name: Cloudflare Workers' `.dev.vars` (`.dev.vars.example` is shown like
  an example env file), `rclone.conf`, `.databrickscfg`, `kaggle.json`, `.mylogin.cnf`,
  `.pg_service.conf`, `.smbcredentials`, Hugging Face's `token`, an extensionless
  `secrets`, `credentials.cnf|config|hcl|tfrc`, `keystore.properties`,
  `signing.properties`, `.terraformrc`, `.ftppass`, `wg0.conf` (and any other file that
  holds a WireGuard key, by its content), the user-level `.gradle/gradle.properties`, and
  the CLI token files of hcloud, doctl, ngrok, glab and gcloud. A project's own
  `gradle.properties` is shown like an example env file: names kept, values withheld.
- Content sniff: a private key (PEM or PuTTY), a service-account key, a Kubernetes
  `Secret` and a WireGuard `PrivateKey` / `PresharedKey` line. It reads the whole file,
  up to 2 MiB; a larger data file is withheld. It reads the file the way a person would:
  any line ending (CRLF, a lone CR, form feed, U+2028), a byte order mark, UTF-16 and
  UTF-32 (with or without a BOM), and a manifest that is indented, in a list
  (`- kind: Secret`, the shape of `kubectl get -o yaml`), quoted, tagged or written in
  YAML flow style (`{kind: Secret}`); `type: service_account` counts in YAML as well as
  in JSON. These layouts are read in data files only. Source code and prose (`.py`,
  `.ts`, `.go`, `.md` ...), binary media and lockfiles are not read from disk for the
  sniff at all: they merely mention the words, and a TypeScript `kind: "Secret";` stays
  readable. Source and prose are held to the strict shapes (a private key, a bare
  `kind: Secret` at the start of a line, the JSON `"kind": "Secret"` and
  `"type": "service_account"`) only where the tool already holds the content and passes
  it to the classifier: the knowledge ingest of uploaded notes and the tracer behind
  `howto`. A file shown as `keys_only` is masked instead of sniffed.
- Symlinks and files with a second hard link are never read by the readers: a committed
  link cannot carry a read of a secret under another name. A regular file whose link
  count is above one is withheld under every one of its names, so a checkout made by
  hard links (`cp -al`, `rsync --link-dest`, a Nix or ostree store, a build cache that
  links files into the tree) is unreadable. `git` itself never makes such a file in a
  clone's working tree; the remedy is a copy that does not share files (`cp -a`,
  `git clone --no-hardlinks`). There is no setting to allow it: the file's name says
  nothing about the file it is.
- `SECRET_PATH_GLOBS_EXTRA` can only add to the list. Each glob reads like a `.gitignore`
  line and is case-blind:
  - a glob with no `/` in it (`config`, `*.example`) or one that starts with `**/`
    (`**/secret.txt`) applies at any depth, the repository root included;
  - any other `/`, a leading one included, ties it to the repository root
    (`/config/*`, `./config/*`, `deploy/internal-*.txt`); a leading `./` is dropped;
  - a glob hides what it names and everything below it: `config`, `config/` and
    `/config/*` all hide `config/app.txt`;
  - a trailing `/` names a directory only (`config/` hides `src/config/app.txt`, not a
    file called `config`); `a/**/b.txt` also matches `a/b.txt`;
  - `*` also matches `/` (`deploy/*` reaches `deploy/a/b`); `?` and `[abc]` are as in
    `fnmatch`; `\` is read as `/`; `*`, `**` and `**/` hide everything; there is no `!`
    negation, a glob only ever adds.

  It also hides the example env names.
- A path the system cannot resolve (an over-long name, a symlink loop, a NUL byte) is a
  generic refusal, like a missing file, and never an error carrying the server's path.

### Example env files (`.env.example`, `.env.sample`, ...)

- Every `KEY=` is shown, with any empty or placeholder value (`changeme`,
  `<your-name>`, `${VAR}`, `https://api.example.com`); every other value is
  `[value withheld]`.
- A value that spans lines (quoted string, PEM, JSON, heredoc, `$( )`, YAML `|` or `>`,
  backslash) is withheld to its closing line, and the continuation lines are empty so
  line numbers stay. A value that never closes withholds the rest of the file. A line
  that is not `KEY=value` is `[line withheld]` unless it is a bare upper-case variable
  name.
- A commented-out assignment (`# OLD_KEY=...`, also `# Set API_KEY=... to enable`, and a
  `# password: ...` whose key is a name a secret goes by) is masked like an assignment:
  its value is withheld unless empty or a placeholder, and the comment lines a commented
  PEM block or quoted value goes on over are withheld with it. A comment after a
  placeholder value is kept when it is plain words (`PORT=8080  # default port`) and
  shown as `# [comment withheld]` when it holds anything that could be a token (a digit
  among letters, a hyphen or underscore inside a word, a URL, `=`). A comment line with
  no `KEY=` (a heading, `#!/bin/sh`, `# NOTE: ...`) is shown as written.
- A line ends at any line terminator `str.splitlines` knows (CR, form feed, NEL, U+2028
  ...), not only at `\n`; the masked file has one line per such line.

Names are judged after splitting on `_`, `-`, `.` and camelCase, so `db_pass`, `dbPass`,
`PGPASS`, `creds` and `{"name": "DB_PASSWORD", "value": ...}` pairs count. A bare `key`,
`seed`, `pin` or `hmac_key` counts when the value looks generated. Connection-string keys
(`AccountKey`, `SharedAccessKey`), webhook URLs, pre-signed URL signatures, `curl -u`,
`mysql -p` and `Cookie` headers are redacted too.

Limits, stated honestly:

- A bare, unquoted `password = <value>` in a code file is a variable name as often as a
  literal. It is redacted when the value has digits among letters (6+ characters, no
  underscore, not `user1`); a letters-only value of that shape may stay. Quoted values,
  headers with a scheme word and URLs are always redacted.
- A 40 or 64 digit hex string is kept when it stands alone or under a name that says it
  is a SHA or hash (it is how commits and integrity hashes look). Under a key-like name or
  in quotes after a neutral name it is redacted.
- A run of 2048 or more characters without whitespace (data URI, minified bundle) is
  replaced by `[REDACTED:long-literal]`; this also keeps every rule linear in time.
- Symlinks in a repository are never read or indexed: the link's name says nothing about
  its target (`.env`, another clone, a system file).
- The tools that read git blobs rather than the working tree (`git grep`, the paths
  built on `git show`) decide by name only: the content sniff needs the bytes on disk, and
  the pathspecs are a belt, not the rule. A secret that is hidden only by its content and
  sits in such a blob is not withheld there.
- A data file above 2 MiB is withheld from every reader of the working tree (lockfiles
  excepted). To make one visible, rename it to a source or prose extension, or accept
  the loss.
- Slices are cut at 400 characters per line and at the caller's `budget_tokens` in total,
  so a one-line minified file cannot flood the answer.
