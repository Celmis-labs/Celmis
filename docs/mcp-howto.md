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
- Slices are cut at 400 characters per line and at the caller's `budget_tokens` in total,
  so a one-line minified file cannot flood the answer.
