# Roles and access

A person's role in a workspace decides which pages and endpoints they can use.
The roles, lowest to highest: viewer, member, editor, admin, owner. A global
admin (the instance operator) passes every workspace check.

## What each role may do

| Area | viewer | member | editor | admin | owner |
| --- | --- | --- | --- | --- | --- |
| Read reviews, pull requests, findings of readable repositories | yes | yes | yes | yes | yes |
| Change a review issue's status | no | yes | yes | yes | yes |
| Review settings, prompts, rules, per-repository policies | no | no | yes | yes | yes |
| Review analytics (cost, outcomes) | no | no | yes | yes | yes |
| Team memories and the Learning view (`/memories`) | no | no | yes | yes | yes |
| Productivity metrics (`/productivity`) | no | no | no | yes | yes |
| Jira connection and the Jira helper pages | no | no | no | yes | yes |
| Git connections, LLM keys, webhooks (create, repair) | no | no | no | yes | yes |
| Members and invitations (who may grant which role: see Workspace roles in the README) | no | no | no | yes | yes |
| Deleting the workspace | no | no | no | no | yes |

"Readable repositories" means the repositories the person may read through the
workspace, a team grant or the provider itself. Owners and admins hold every
repository of the workspace.

A repository nobody has a rule for is closed to members. Every review feature
above (reviews, issues, memories, learning, feedback, the commands and the
requirements of a pull request, productivity tables, chat replies, the Jira
check) uses the same resolver as MCP: a repository the person may not read is
answered like one that does not exist (404), and a repository that is only
named (`metadata` visibility) is listed by name and never opened. The role
gates in the table still apply on top. How rules, grants, MCP tokens and the
audit work: [MCP access](mcp-access.md).

## Rules worth knowing

- Memories are for editors and above, reads included, and a repository's
  memories are listed, counted, previewed and attributed only to somebody who may
  read that repository. A memory taught on a repository the person cannot read
  does not name it to them.
- Productivity is for owners and admins only, because the developer tables put
  names next to numbers.
- Every endpoint that saves, verifies or removes a credential (a Jira token, a
  git token) is for admins and owners, and no endpoint ever returns a stored
  secret; the connection list says only that one exists.
- The commands given on a pull request (`GET /api/pull-requests/{id}/commands`)
  are shown only to people who may read the repository, otherwise the answer is
  404, as for a pull request that does not exist.
- Webhooks are verified (HMAC or token) before anything is read, and a delivery
  is bound to the workspace of the registered repository.

## Who may command the reviewer in a pull request

That is a different question, answered by the provider rather than by the
workspace role: see `command_permission` in [PR commands](PR_COMMANDS.md). The
same rule decides whose feedback teaches the reviewer
([Memories and learning](MEMORIES_AND_LEARNING.md)). A person's workspace role
makes their `@celmis remember:` active at once when it is editor or above;
anybody else's is held for approval.

## Setting a role up

1. Invite people from the members page, choosing the lowest role that does the
   job. Most people who only read findings need viewer or member.
2. Give editor to the people who tune the reviewer (rules, prompts, memories).
3. Give admin to the few who manage connections, webhooks and members.
4. Keep owner for the person who answers for the workspace.
