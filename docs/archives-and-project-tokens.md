# Archives, project file scope and project MCP tokens

Three features that work together: code that has no git remote can be added
from an archive, a project can narrow which files of a repository it uses, and
an outside AI client can search one project through a scoped token.

## Add code from an archive

Only the superadmin can do this: Repositories, tab **Archive** (or
`POST /api/repos/upload`, multipart: `slug`, `name`, `file`).

- Formats: `.zip`, `.tar.gz`, `.tgz`. Default size limit 250 MB
  (`CELMIS_MAX_UPLOAD_BYTES`); upload rate limit `CELMIS_RL_UPLOAD`
  (default 10 per window).
- Unpacked limits: 2 GiB in total, 150 000 files, 128 MiB per file. Absolute
  paths, `..`, links and device files are rejected; a single wrapping top
  folder is removed.
- Non-ASCII names are supported: names are normalised to NFC, the zip UTF-8
  flag is honoured and legacy cp437 names are re-read as UTF-8 when valid.
- The repository is stored as `upload/<slug>`. Its "commit" is the SHA-256 of
  the archive. There is no git history, no webhooks and no freshness check
  (state `not_applicable`). Uploading again with the same slug replaces the
  files and re-indexes.
- A reverse proxy in front of the API must allow a body of the same size.

Languages without a symbol extractor (for example `.bsl`, `.xml`, `.txt`) are
still searchable: Q&A and project MCP fall back to plain, Unicode-aware text
search over the allowed files (binaries and very large files are skipped).

## File scope of a project repository

Each repository in a project can have include and exclude glob patterns
(`PATCH /api/projects/{id}/repos/{slug}` with `include_globs` and
`exclude_globs`, or the **File scope** control on the project page). An empty
include list means every file; exclude always wins. The scope narrows Q&A,
text search and the project MCP tools.

## Project MCP tokens

Superadmin only: `GET/POST /api/projects/{id}/mcp-tokens`,
`DELETE /api/projects/{id}/mcp-tokens/{token_id}`, or the **MCP access** card
on the project page.

- Opaque `cmcp_...` token, only its hash is stored, shown once.
- Lifetime from 1 hour to 90 days (default 30 days). Expired or revoked
  tokens are refused on the next call; `last_used_at` is recorded.
- Scope `read:project_search`; tools `search_project` and `ask_project`. Other
  tools are hidden and refused. The token never reaches another project.

Connect Claude Code (note the trailing slash):

```bash
claude mcp add --transport http project-search https://<your-host>/mcp/ \
  --header "Authorization: Bearer <token>"
```
