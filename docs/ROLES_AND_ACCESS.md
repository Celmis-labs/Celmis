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
| Read the Codebase packs the audience lets you see ([Codebase](CODEBASE.md)) | yes | yes | yes | yes | yes |
| Create a Codebase knowledge pack (changing one is the pack's own rule: its maintainers) | no | no | yes | yes | yes |
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

- Codebase packs have two layers. The role decides who may read the section at
  all (any role) and who may create a pack (editor and above). What a person
  sees and changes in a given pack is the pack's audience (`workspace`, `teams`
  or `restricted`), its team grants and the people named on it: an owner or
  admin maintains `workspace` and `teams` packs, but a `restricted` pack is
  opened only by the teams and people it names, and a pack the person may not
  see is answered like one that does not exist (404). See [Codebase](CODEBASE.md).
- A **viewer** only reads a pack: a `maintain` grant (a name on the pack or a
  team grant) gives nothing to someone whose workspace role is viewer, naming a
  viewer as a maintainer is refused, and demoting a maintainer to viewer ends
  their right to change the pack and the publish tokens they made, while the
  pack stays readable to them.
- Removing someone from a workspace also takes them out of that workspace's teams
  (their teams in other workspaces are untouched) and revokes the pack publish
  tokens and invite links they made there, and the MCP tokens they hold for it
  (see [MCP access](mcp-access.md)); adding them back restores none of it. A change
  of role revokes nothing. Deleting your own account follows the same rules for
  every workspace you were in, and deleting a workspace revokes the MCP tokens
  bound to it.
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

## Seats

Roles say what a person may do; seats say how many people the installation
holds. Without a licence a Celmis holds **up to five people**, and at most five
of them sign in through single sign-on; a licence lifts both limits, and never
gives fewer than five ([Licensing](../LICENSING.md)).

- A seat is an active account. The master (break-glass) account and the seeded
  local user take none, and deactivating or erasing an account frees its seat.
- The limit only stops **adding a person**: signing up, a first Google or single
  sign-on sign-in, a new address in `analyzer auth google`, switching a
  deactivated account on again. A person who already has an account is never
  refused. They sign in, accept invites and are added to workspaces and teams
  as before, even when the installation has more people than the limit allows
  (it did when the limit arrived, or a licence ran out).
- The refusal tells a person who is not signed in the limit and nothing about
  how many people the installation holds.
- Inviting someone who has no account yet issues the link as usual, but the
  invite form warns when no seat is free: the newcomer cannot sign up until a
  seat is freed or a licence lifts the limit. Only people who already have an
  account can join.
- A global admin sees the numbers on **Admin → Health → Seats**. People who may
  invite into a team workspace learn only whether a seat is free (a yes or no),
  never how many people the rest of the installation holds; in a personal
  workspace nobody is invited, so the question is refused.
- **Switching an account off and on** is the master account's, from
  **Admin → Users** or `analyzer auth deactivate|reactivate EMAIL`. A
  deactivated account is refused on every way in at once (a session and its
  refresh, the password, Google, single sign-on, MCP tokens, OAuth refresh
  tokens, pack tokens) and keeps its data; switching it on needs a free seat. The master account, the
  local default account and the account holding the master address cannot be
  switched off. A project token belongs to no account, so there is nothing to
  switch off.

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
