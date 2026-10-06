# MCP access: who can reach which repository

This page is for the person who runs a Celmis install. It covers how access to
repositories works, how to give a developer an MCP token, and what changed when
the default turned to deny.

## The rule

A repository is visible to a person when one of these is true:

1. They are the superadmin, a global admin, or the owner or an admin of the
   workspace the repository is registered in.
2. A team they belong to has a grant on it (`read`, `review` or `admin`).
3. A team they belong to has an access rule on it at `metadata` or `code`.

A repository with neither a grant nor a rule is **unruled**. Only the people in
point 1 see an unruled repository. This is the same in `single_tenant` and
`multi_tenant`, and the same on the web, the REST API and every MCP tool.

A repository you may not see is not "forbidden", it is absent: the answer is
the same as for a name that does not exist, and no list, count, notice or error
mentions it.

Deny globs of an access rule still hide paths inside a repository you can see.

A team's grant and an access rule work together in one way: **a rule narrows,
a grant is only the fallback.** If a repository has any access rule, only the
teams the rules name get in (at the rule's visibility); a read grant held by
another team does not open it. A repository with no rule at all is opened by a
team's grant. A grant whose permission value is not one the app knows
(`read`, `review`, `admin`) grants nothing.

## Upgrading

After the upgrade, members lose sight of every unruled repository. Check what
is affected before you upgrade, or right after:

- Open Team, Code access (or Teams). A banner says how many repositories only
  admins can see.
- `GET /api/access/unruled` returns the same list (workspace admins only).
- On the server: `analyzer access bootstrap --team <team> --workspace <ws>`
  lists them without changing anything.

To give a team the access it effectively had before:

```bash
analyzer access bootstrap --team engineering --visibility code --all-unruled
```

This writes one access rule per unruled repository and, unless the visibility is
`none`, a `read` grant, so the repository appears on the repositories page too.
Use `--repos 'acme/shop-*'` to limit it.

### Rolling back the default

For a single-tenant install that needs the old behaviour for a while:

```env
CELMIS_UNRULED_REPO_ACCESS=open
```

It is read at start-up. In `multi_tenant` it is **ignored**, and a warning is
logged, because "every member of every tenant can read every unruled
repository" is not a setting a shared installation should have.

## MCP tokens

A token belongs to one person and names the repositories it reaches.

Issue one on **Administration, MCP tokens** (superadmin only), or:

```bash
export CELMIS_MCP_TOKEN=$(analyzer mcp issue-token \
  --user dev@example.com --workspace acme --repos 'acme/shop-*,acme/ui')
```

| Field | Meaning |
|---|---|
| person | The account the token acts as. It must be a member of the workspace. |
| workspace | The workspace the token answers for. A pattern matches only repositories registered there. |
| repositories | Exact slugs and/or globs (`acme/shop-*`). `*` is every repository of the workspace. |
| expiry | Days, capped by `CELMIS_MCP_TOKEN_MAX_DAYS` (default 90). |
| write | Off by default. Write tools need the `full` profile and this switch. |

The token is shown once and is not stored. What the server keeps is the grant
behind it: person, workspace, repositories, expiry, last use. That is why the
list can be edited and the token revoked without handing out a new one. A
change or a revocation applies within 30 seconds.

The list the superadmin writes is what the token reaches: it grants code access
to the matching repositories of that workspace whether or not a team rule exists
(that is the point of issuing a token for a repository nobody has a rule for).
It is still limited in three ways: it matches only repositories registered in
its own workspace; it stops working when the holder is no longer a member of
that workspace; and the deny globs of an access rule keep hiding the paths they
name, so credentials and similar files stay out whatever the list says.

Connect an editor with the snippet shown after issuing. It reads the token from
an environment variable so the file can be committed:

```json
{
  "mcpServers": {
    "celmis": {
      "type": "http",
      "url": "https://celmis.example.com/mcp/dev/",
      "headers": { "Authorization": "Bearer ${CELMIS_MCP_TOKEN}" }
    }
  }
}
```

### Self-service

Off by default: `POST /api/mcp/token` answers 403. Set
`CELMIS_MCP_SELF_SERVICE=true` to let people issue their own. Such a token
carries `*` and is capped by the person's own access (unlike a token the
superadmin issued, which reaches exactly its list). Settings, MCP shows each
person their own tokens (never the value) and lets them revoke them.

### Changing a token after it was issued

The repository list can be narrowed or replaced, the expiry brought forward and
writing switched off, all without reissuing. Three things are fixed in the
signed token and cannot be changed: a later expiry, switching writing on, and a
repository list on a self-service token (that one reaches what its holder
reaches). For those, revoke and issue a new token. An entry such as `acme/shop`
matches the repository under both providers if both are registered; the issue
response warns about it, and listing exact slugs avoids it.

### The server's own calls

The in-app agent, the claude-engine review and documentation generation call
`/mcp/` on the same server. Each mints a short-lived `internal` grant for the
person it acts for (read-only, bound to the workspace, never listed, swept after
a day): it carries no repository list, so the person's own access is the whole
ceiling.

### Tokens issued before this release

They carry no grant and are refused with 403 and the reason "token predates
per-person grants" (a token that is expired, forged or unknown still gets a plain
401). Reissue them. For a short transition,
`CELMIS_MCP_LEGACY_TOKENS=accept` accepts them as read-only and default-deny;
those calls are audited as `legacy`.

## OAuth

OAuth sign-in from an MCP client still works, with two conditions:

- The person needs an `oauth_grant`, created by the superadmin
  (`POST /api/admin/mcp-grants`, with the repositories it covers). Without one
  the consent step answers 403.
- The access token is limited to that grant's repositories, and refresh stops
  working as soon as the grant is revoked or expires.

There is no dynamic client registration: clients are registered by a workspace
admin. The `client_credentials` grant names a client, not a person, so its
tokens are not accepted by the MCP server and never by `/mcp/dev`.

OAuth is not the recommended path for developers; a per-person token is simpler
to reason about and to revoke.

## The call log

Every MCP call writes one row: time, person, token id, tool, the repositories it
touched (when there are 20 or fewer), the size of the result, the duration and a
hash of the arguments. **Argument values and results are never stored**, and
neither are secrets.

A call refused before any tool runs (a revoked, expired, unknown, legacy or
wrong-holder token) is logged too: status `denied`, tool `(refused:<reason>)`,
the token id and the person from the signed claims, no arguments, at most one
row a minute per token and reason. The digest of the arguments is keyed to the
installation (derived from the signing secret), so a short query or a slug
cannot be recovered from it by trying candidates.

Read it on the Calls tab, or `GET /api/admin/mcp-calls` (filters: `user`,
`token`, `tool`, `status`, `workspace`, `days`; `?format=csv` for a
spreadsheet). Rows older than `CELMIS_MCP_AUDIT_RETENTION_DAYS` (default 180)
are deleted. Each call is also written to the general audit log as `mcp.call`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CELMIS_UNRULED_REPO_ACCESS` | `deny` | `open` restores the old behaviour in `single_tenant` only. |
| `CELMIS_MCP_SELF_SERVICE` | `false` | Let any signed-in user issue their own token. |
| `CELMIS_MCP_TOKEN_ISSUERS` | `superadmin` | `platform_admin` also lets global admins issue. |
| `CELMIS_MCP_TOKEN_MAX_DAYS` | `90` | Longest lifetime of an issued token. |
| `CELMIS_MCP_LEGACY_TOKENS` | `refuse` | `accept` takes pre-grant tokens as read-only. |
| `CELMIS_MCP_AUDIT_RETENTION_DAYS` | `180` | How long call rows are kept. |
