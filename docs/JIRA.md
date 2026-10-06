# Jira: the task behind a pull request

When a pull request names a Jira task, the business-logic agent can read it and
check that the change does what the task asks. Celmis only reads Jira. It never
writes a comment, a transition or a field.

## Connect a site

Connecting and removing a Jira credential is for workspace admins and owners.
On the Connections page choose Jira and give:

- the site URL, for example `https://acme.atlassian.net`;
- the Atlassian account e-mail and an API token for it (or, when the workspace
  already has a Bitbucket connection with the same Atlassian account, reuse it).

The credential is verified when it is saved (a read of the account and the
visible projects). The token is stored encrypted and is never returned by any
endpoint; the connection list says only that a credential exists and which
account it belongs to. Saving or removing a credential discards what the old one
read.

The client is read-only, follows no redirects (a redirect means the site URL is
wrong, and following it is how a token would leave its host), and can reach only
the connection's own host. Every failure is turned into a sentence that names the
host and the status, never the token.

## Which task a pull request names

A task key looks like `PROJ-123`. Celmis looks in the title, then the branch name,
the description and the commit messages, and reads at most three tasks. Keys such
as `UTF-8` or `SHA-256` are not taken for projects: a key counts when its project
is allow-listed (`task_project_keys`) or the connected site confirms the project
exists. A browse link counts only when it is on the connection's own host.

## Self-hosted Jira

A workspace's site must be `https://<site>.atlassian.net` (or `.jira.com`). A Data
Center or otherwise self-hosted Jira is accepted only when its host is listed in
`JIRA_ALLOWED_HOSTS` (a JSON list in the environment, an exact host or a subdomain,
e.g. `["jira.example.internal"]`). A listed host may resolve to a private address
(a LAN or VPN Jira), never to a link-local, multicast or unspecified one. It is not
implied by `EGRESS_ALLOW_PRIVATE_NETWORK`. Empty means Atlassian Cloud only.

## Settings

Repository over workspace over built-in.

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `task_context_enabled` | on | Read the task a pull request names and give it to the business-logic agent. |
| `task_project_keys` | none | Project keys that may be taken from text (`["PROJ"]`). Empty: the site's own project list decides. |
| `task_acceptance_field` | none | The custom field (`customfield_12345`) that holds acceptance criteria, when they are not in the description. |
| `task_include_comments` | 0 | How many of the latest task comments to include (0 to 10). |
| `business_logic_auto` | `off` | `when_task_found` runs the business-logic agent in every review that finds a task. `off`: only on request. |
| `requirements_check_mode` | `checklist` | `off`, `findings` or `checklist`; see below. |
| `task_urls_enabled` | off | Let an on-demand check read a Confluence page of the same site when a comment links to it. |

The admin pages that help to fill these in (the project list, the custom field
list, a preview of a task) are for workspace admins and owners too.

## The requirements check

Acceptance criteria are numbered AC1, AC2 and so on, from a list in the task or
the acceptance field. After a review that read them, the completed comment can
carry a checklist:

```
### Requirements check: PROJ-123 Export the report as CSV (In Review)
- met   AC1 The export has a header row (src/export.py:41)
- part  AC2 Amounts use two decimals: partly met
- miss  AC3 An empty report downloads an empty file: not implemented
- ?     AC4 The file opens in Excel: could not be judged
```

(rendered with check marks and warning signs). In `findings` mode it costs
nothing: a criterion is marked only when the business-logic agent cited it, and
the others read "no gap reported". In `checklist` mode one extra short model
call judges every criterion against the diff. The agent's findings still win,
and a "met" without a cited line in a file the pull request changed is
downgraded to "could not be judged".

Everything that comes from Jira is data: fenced in the prompt, and printed in the
comment with `@` defused and no angle brackets, so a task description cannot ping
anybody or smuggle markup into a comment.

## On demand

`@celmis -v business-logic PROJ-123` checks the change against a task named in a
comment, by key or by a link to the issue on the connected site. One agent runs,
nothing is posted on the code, and the answer is a reply in the thread. Every
failure is a sentence in the answer. Links to other sites and to Google Docs are
refused. See [PR commands](PR_COMMANDS.md) for who may ask.
