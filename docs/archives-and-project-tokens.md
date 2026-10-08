# Archives, project file scope and project MCP tokens

Three features that work together: code that has no git remote can be added
from an archive, a project can narrow which files of a repository it uses, and
an outside AI client can search one project through a scoped token.

## Add code from an archive

Only the superadmin can do this: Repositories, tab **Archive**.

**Chunked, resumable upload.** A proxy or CDN in front of the API commonly caps
the size of one request body (for example around 100 MB), so a large archive
cannot travel in a single request. The page therefore always sends the archive
in parts, with a progress bar, up to 3 attempts per part and a cancel button:

1. `POST /api/repos/upload/sessions` with `{slug, name, filename, size, sha256?}`
   returns `{session_id, chunk_size, parts}`. The extension, the total size
   (`CELMIS_MAX_UPLOAD_BYTES`), the slug and the free disk space (at least twice
   the archive size) are checked here, before any byte is sent.
2. `PUT /api/repos/upload/sessions/{id}/parts/{n}` with the raw part as the body
   (`n` from 0; every part is exactly `chunk_size` bytes except the last). Parts
   may arrive in any order and may be re-sent. `GET .../sessions/{id}` lists the
   parts already received, so an interrupted upload can resume.
3. `POST .../sessions/{id}/complete` joins the parts, verifies the total size
   (and the sha-256 when given) and then runs the same safe extraction and
   indexing as below. `DELETE .../sessions/{id}` aborts and deletes the parts.

Parts are kept under the data directory (`uploads/`), not a temporary
filesystem. A session expires after 24 hours (`CELMIS_UPLOAD_SESSION_TTL`);
expired sessions are removed when a new one opens and at startup. The part size
is `CELMIS_UPLOAD_CHUNK_BYTES` (default 32 MiB, never above 90 MiB); the request
body cap for a part is that size plus 1 MB.

The single-request `POST /api/repos/upload` (multipart) remains for small
archives and scripts.

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
- With the chunked upload a proxy only needs to allow a body of one part (about
  33 MB by default).

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
claude mcp add --transport http project-search <API base>/mcp/ \
  --header "Authorization: Bearer <token>"
```

`<API base>` is where the API is served. Behind a path-prefix reverse proxy the
prefix is part of it (for example `https://<your-host>/backend/mcp/`); the MCP
access card shows the exact URL for your deployment.
