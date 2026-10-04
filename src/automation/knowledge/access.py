"""People: roles, invites, access requests, teams, code access, SSO, licence.

The roles section is COMPUTED from src/users/roles.py — who may grant what is
asked of `grantable_roles` itself — so a change to the grant rule changes
what the agent says with no edit here. The rest is written from
web/app/(app)/admin/{workspaces,teams,access,users,access-requests},
src/api/routers/{invites,access_requests,teams,access}.py and src/ee.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.automation.knowledge._base import Section


def _roles_body() -> str:
    from src.users import roles as r

    ladder = sorted(r.WORKSPACE_ROLE_RANK, key=r.WORKSPACE_ROLE_RANK.__getitem__)
    # A plain workspace member, not the master account: what THEIR role lets
    # them grant is the question; the superadmin may grant everything.
    plain = SimpleNamespace(id="x", email="", is_admin=False, is_active=True,
                            has_google=False, has_oidc=False)

    def _names(roles) -> str:
        ordered = sorted(roles, key=r.WORKSPACE_ROLE_RANK.__getitem__)
        return ", ".join(f"`{x}`" for x in ordered) if ordered else "nobody"

    what = {
        "viewer": "reads everything in the workspace.",
        "member": "reads, and changes the status of review issues.",
        "editor": ("the prompt editor: the workspace agent prompts and every "
                   "repository's code review settings (prompts, rules, "
                   "agents, models, branches, filters) and analytics. Not "
                   "the workspace defaults, members, invites, teams, LLM "
                   "keys, git connections or the licence."),
        "admin": ("everything an editor does, plus members and invites, "
                  "teams and code access, LLM keys and models, git "
                  "connections, notification channels, the budget, the job "
                  "queue's retry/stop and the audit log."),
        "owner": ("everything an admin does, plus deleting the workspace. "
                  "Everybody owns their personal workspace."),
    }
    lines = ["Workspace roles, lowest to highest (a person can hold a "
             "different role in each workspace):"]
    for role in ladder:
        flags = []
        if role in r.PROMPT_EDITOR_ROLES:
            flags.append("may edit prompts and review policies")
        if role in r.WORKSPACE_ADMIN_ROLES:
            flags.append("administers the workspace")
        extra = f" ({'; '.join(flags)})" if flags else ""
        lines.append(f"- `{role}`{extra} — {what.get(role, '')}".rstrip(" —"))

    lines.append("")
    lines.append("Who may grant which role (computed from the product's own "
                 "rule):")
    grantable_by_someone: set[str] = set()
    for role in reversed(ladder):
        grants = r.grantable_roles(plain, role)
        grantable_by_someone |= set(grants)
        if grants:
            lines.append(
                f"- A workspace `{role}` may give and take back: "
                f"{_names(grants)} — and may change or remove only people "
                f"who already hold one of those roles.")
    others = [x for x in ladder if r.grantable_roles(plain, x) == frozenset()]
    if others:
        lines.append(f"- {_names(others)} may grant no roles.")
    superadmin_only = set(ladder) - grantable_by_someone
    if superadmin_only:
        lines.append(
            f"- {_names(superadmin_only)} can be granted, changed to or "
            f"from, or removed ONLY by the superadmin.")
    lines.append("- The superadmin may grant every role in every workspace.")
    lines.append("""
The superadmin is the installation's master account — signed in with
`CELMIS_MASTER_EMAIL` and `CELMIS_MASTER_KEY` from the server's `.env` — and
nobody else. A global admin (promoted with `analyzer auth make-admin
<email>`, or by the SSO group in `OIDC_ADMIN_ROLE`) has platform powers
(Administration pages, every workspace visible, editing prompts and policies
anywhere) but grants workspace roles only as far as their own role in that
workspace allows. Signing up grants no admin rights to anybody.

An invite carries only a role its creator could grant, re-checked when it is
accepted. Shared workspaces are created by the superadmin; everyone gets a
personal workspace at sign-up and is its owner. If you need a role you cannot
be given by your workspace admin, ask the superadmin (the person who runs the
server).""")
    return "\n".join(lines)


SECTIONS = (
    Section(
        id="roles",
        title="Roles and permissions (who can do and grant what)",
        keywords=(
            "role", "roles", "permission", "rights", "access", "viewer",
            "member", "editor", "admin", "owner", "superadmin", "global admin",
            "grant", "promote", "demote", "can i", "allowed",
            "рол", "прав", "доступ", "редактор", "адмін", "власник",
            "суперадмін", "учасник", "глядач", "надати", "підвищ", "можу",
            "дозвол",
            "админ", "владел", "суперадмин", "участник", "выдать", "могу",
        ),
        strong=("role", "рол", "superadmin", "суперадмін", "суперадмин",
                "editor", "редактор", "grant"),
        body=_roles_body(),
    ),
    Section(
        id="members-invites",
        title="Workspaces, members and invitations",
        keywords=(
            "invite", "invitation", "member", "members", "add user",
            "add person", "teammate", "colleague", "workspace", "join",
            "link", "remove", "password reset", "reset link",
            "запрос", "запрош", "колег", "учасник", "додати", "людин",
            "користувач", "робоч", "простір", "приєдн", "посилан", "скинут",
            "пригла", "пользоват", "рабоч", "пространств", "присоедин",
            "ссылк",
        ),
        strong=("invite", "invitation", "запрос", "запрош", "пригла",
                "workspace", "простір", "пространств"),
        body="""
Where: [Workspaces](/admin/workspaces) (Team & access → "Workspaces"). Each
workspace is a card; open it to see "Members" and "Invitations". Managing
members needs owner or admin of that workspace (or the superadmin); others
read "Only this workspace's owner or admin manages its members: the owner grants admin, editor, member and viewer, an admin only member and viewer; the owner role is granted by the superadmin."

Add somebody who already has an account (and shares a workspace with you):
1. In "Members" pick them in "— select user —", choose the role, press
   "Add" ("Member added").
2. Change a role with the dropdown next to the member ("Role changed"); the
   trash icon removes them. You cannot change your own role.

Invite somebody by email or link ("Invitations"):
1. Type the address (placeholder "teammate@company.com"), choose the role
   (only the roles you may grant are offered).
2. "Invite by email" — an email-bound, single-use link valid 14 days; with
   SMTP configured it is also emailed ("The invitation was also emailed to {email}"), otherwise copy it and send it yourself. If the person already
   has an account you can see, they are added at once.
   Or "Create link" — an open link (up to 25 uses); "Link expiry" "Expires in 5 minutes" (default) or "Never expires".
3. The person opens `/invite/<token>`, signs in or creates an account, and
   presses "Join workspace". Signing in with Google or SSO with the invited
   (verified) address joins automatically; a password account must press
   the button. "Revoke this invite?" removes a pending one.
Invite errors: not valid (copied incompletely), revoked, expired, already
used, or issued for a different email address — ask for a new one.

Password reset for a member: the key icon "Copy a password-reset link" (valid
15 minutes, send it privately). Self-service "Forgot password?" on the login
page emails a link only when SMTP is configured.

Create a shared workspace: superadmin only ("Create workspace": "Name",
"Description", "Create"; or "+ New workspace" in the switcher). Delete: the
owner or the superadmin; the `default` workspace cannot be deleted. Switch
workspaces with the switcher in the top bar.
""",
    ),
    Section(
        id="access-requests",
        title="Access requests (no team workspace yet)",
        keywords=(
            "access request", "request access", "no access", "approve",
            "decline", "reject", "pending", "join team",
            "запит", "доступ", "схвал", "відхил", "немає доступ",
            "запрос", "одобр", "отклон", "нет доступ",
        ),
        strong=("access request", "request access", "запит на доступ",
                "запрос доступ", "нема", "немає доступ"),
        body="""
A signed-in person whose only workspace is their personal one sees "You have no access to team workspaces yet — send a request." on the dashboard.
1. Open [Access request](/access-request) ("Request access"), optionally fill
   "Comment (optional)" (your team, your role, who can confirm it; up to 1000
   characters) and press "Send request".
2. One pending request at a time; "Cancel request" withdraws it. The page
   shows the decision ("Access granted: {workspaces}" or the reason for a
   decline); after a decline you may ask again.
Got an invitation link from a colleague? Open it instead — it adds you at
once.

Deciding is for the SUPERADMIN only, on
[Access requests](/admin/access-requests) (Administration): for each request
pick one or more workspaces and roles under "Grant access" ("Another workspace" adds a row) and press "Approve", or write "Reason (required, the person will see it)" and press "Decline". Every decision is audited; access
given another way (Users page, an invite) closes the request as approved.
With SMTP, both sides are emailed.

The superadmin also manages everybody on [Users](/admin/users): roles per
workspace, adding to and removing from workspaces, and the "No team access"
filter for accounts with only a personal workspace.
""",
    ),
    Section(
        id="teams-code-access",
        title="Teams and code access (who can see which repository)",
        keywords=(
            "team", "teams", "code access", "visibility", "deny", "allow",
            "glob", "hide", "private", "sensitive", "secret files",
            "repository access", "permission",
            "команд", "видимост", "прихов", "прихова", "заборон",
            "конфіденц", "доступ до код",
            "скры", "запрет", "видимост", "конфиденц",
        ),
        strong=("team", "teams", "команд", "visibility", "видимост", "deny"),
        body="""
Two separate controls, and they do not imply each other: a team GRANT says
what a team may DO with a repository, a research RULE says what it may
EXPLORE in it. A grant does not open Q&A, and a rule needs no grant.

[Teams](/admin/teams) ("Teams & access"): group people into teams and grant
each team a permission on repositories — `read` (repository details and
intel), `review` (start reviews, edit and reset the repository's review
policy) or `admin` (also delete the repository). A person's effective
permission is the highest one earned through any of their teams; global
admins bypass the checks.
1. "Create team": "Name", "Description", "Add".
2. Under "Members" type the person's internal user id (the field mentions
   email, but only the id is matched today — an email answers `Not a member
   of this workspace`; the id is the `user_id` in
   `GET /api/workspaces/<workspace id>/members`), pick a team label (a label
   only, it grants nothing) and press "Add". Only people who are already
   members of the workspace can join one of its teams.
3. "Repository access": pick a repository and permission, press "Grant".
Until a repository has any grant, single-tenant installations leave it open
to everyone in the workspace; multi-tenant ones refuse (`No team is granted
access to …`).

[Code access](/admin/access) ("Research access"): what a team may explore in
a repository through Ask the code (Q&A) and MCP.
1. "Add / update rule" — "Repository", "Team", "Visibility" ("None (hidden)",
   "Metadata only" = documentation and structure, no source, "Full code").
2. "Deny globs (always hidden)" — paths that are refused even at full code
   (presets "Credentials", "Crypto / algorithms", "DB connections");
   "Allow globs (optional)" — an exhaustive allow-list, deny still wins. A
   pattern without wildcards covers the whole folder.
3. "Save" ("Rule saved"). One rule per team and repository; saving again
   overwrites it. Rules of several teams add up — the most open one wins.
A repository with no rule at all is open in full on single-tenant and closed
on multi-tenant. Once it has ANY rule, every team without one of its own sees
nothing of it — so the first rule you add for one team hides the repository
from everybody else who has no rule (global admins excepted).
Code search and generated docs are not filtered by these rules.
Anyone in the workspace can open both pages; only owner or admin can change
them.
""",
    ),
    Section(
        id="neighbour-team-code",
        title="Letting another (neighbouring) team explore your code",
        keywords=(
            "another team", "other team", "neighbour", "neighbor", "external",
            "contractor", "outsider", "explore", "browse", "read-only",
            "read only", "see the code", "look at the code", "team", "code",
            "сусідн", "команд", "перегляд", "переглянут", "дослідж",
            "код", "підрядник", "зовнішн", "лише читан",
            "соседн", "просматрива", "исследова", "подрядчик", "внешн",
        ),
        strong=("another team", "other team", "neighbour", "neighbor",
                "explore", "сусідн", "іншій команд", "іншої команд",
                "інша команд", "дослідж", "перегляд", "соседн",
                "другой команд", "другим команд", "исследова", "просматрива"),
        body="""
Yes — give them a team, a research rule per repository, and (for their
editor) an MCP token. Two controls decide it, and both are on the Team &
access pages: research RULES on [Code access](/admin/access) decide what a
team may explore (Ask the code and MCP); team GRANTS on
[Teams](/admin/teams) decide what it may do (`read`, `review`, `admin`).
Exploring needs only a rule. Changing either page needs owner or admin of
the workspace.

1. Let them into the workspace: [Workspaces & members](/admin/workspaces) →
   the workspace → "Invitations" → their address, role `viewer` (enough to
   ask the code and use MCP; `member` if they should also change issue
   statuses) → "Invite by email" or "Create link". Someone with an account
   but no team workspace can instead send an
   [Access request](/access-request), which the superadmin decides.
2. Make their team: [Teams](/admin/teams) → "Create team" ("Name",
   "Description", "Add"); under "Members" add each person by internal user
   id and press "Add".
3. Decide what they see, per repository: [Code access](/admin/access) →
   "Add / update rule" → "Repository", "Team", "Visibility" — "Metadata only"
   (documentation and structure, no source) or "Full code";
   "Deny globs (always hidden)" with the presets "Credentials",
   "Crypto / algorithms", "DB connections"; "Allow globs (optional)" to
   show only part of a repository → "Save".
4. Keep your own access: once a repository has any rule, a team without a
   rule sees none of it. Add a "Full code" rule for your own team(s) on the
   same repositories in the same go.
5. A grant on [Teams](/admin/teams) only if they should do more than
   explore: `read` for repository details and intel, `review` to run
   reviews and edit that repository's review policy.

How they explore:
- In the browser: [Projects](/projects) → "New project" → "Select repos" →
  "Create", then "New chat". Answers cite only what their rule allows and
  say how many files were hidden; "Code display: off" explains without
  quoting source.
- From their IDE: [MCP](/settings/mcp) → "Generate a token" → "Copy token";
  read-only scopes `read:graph`, `read:groups`, `read:reviews`, valid 30
  days, shown once, issued as that person, so their rules apply to every
  call. It cannot be revoked early — it expires. MCP answers from the
  workspace where the person holds their HIGHEST role, which for most people
  is their personal workspace (they own it), not yours — check with the
  `get_my_access` tool; until then the browser is the reliable way.

What they can and cannot see:
- They can see the names of every repository of the workspace, every
  project and chat in it (chats are shared: an answer quoted to a colleague
  with full access stays readable to them), [Code search](/search) and the
  generated [Docs](/docs) — neither is filtered by research rules — and
  review policies (read-only).
- They cannot change teams, rules, members, invitations, LLM keys or git
  connections (owner/admin), prompts or review policies (editor and up), or
  open analytics.

single_tenant vs multi_tenant (the operator's `CELMIS_DEPLOYMENT_MODE`,
`single_tenant` by default): single_tenant fails OPEN where nothing is
configured — a repository with no research rule is full code to everybody,
one with no team grant is open — and multi_tenant fails closed and also
refuses any repository not registered in the caller's workspace. On
single_tenant, put a rule on every repository they must not read in full.
The strongest separation is a workspace of their own holding only the
repositories they may see, with them invited there and nowhere else.
""",
    ),
    Section(
        id="sso-licence",
        title="Sign-in, SSO/OIDC, Google, and the enterprise licence",
        keywords=(
            "sso", "oidc", "saml", "okta", "keycloak", "azure", "entra",
            "google", "login", "sign in", "sign-in", "signup", "sign up",
            "password", "licence", "license", "enterprise", "edition",
            "community", "activate",
            "вхід", "увійти", "логін", "пароль", "ліценз", "ентерпрайз",
            "реєстрац",
            "вход", "войти", "логин", "лиценз", "регистрац",
        ),
        strong=("sso", "oidc", "licence", "license", "ліценз", "лиценз",
                "enterprise"),
        body="""
Sign-in options (login page): email + password ("Sign in" / "Sign up"; off
when the operator sets `AUTH_PASSWORD_LOGIN=false`), "Continue with Google"
(when the operator configures Google OAuth; not licence-gated), and company
SSO "Sign in with {name}" (OIDC; needs the enterprise licence). With password
login off, only the master account can use the fold-out "Administrator sign-in".

Set up SSO (OIDC) — done by whoever runs the server:
1. Register Celmis at the identity provider with the redirect URI
   `https://<your-host>/api/auth/callback/oidc`.
2. Set `AUTH_OIDC_ISSUER`, `AUTH_OIDC_CLIENT_ID`, `AUTH_OIDC_CLIENT_SECRET`
   and optionally `AUTH_OIDC_NAME` (the button label) in `.env`; optionally
   `OIDC_ADMIN_ROLE` (an IdP role/group that makes a person a global admin)
   and `OIDC_ADMIN_ROLE_SYNC=true` (also removes it). Restart.
3. Activate a licence that includes single sign-on (below). Only verified
   emails are accepted; invites addressed to that email are redeemed at
   sign-in.

Enterprise licence — [System status](/admin/health), "Edition" card (global
admin): shows "Community" or "Enterprise", "Licensed to", "Expires",
"Features". Paste the key into "Paste licence key" and press "Activate";
"Remove licence" switches enterprise features off for everyone at once. The
operator may instead set `CELMIS_LICENSE_KEY` or `CELMIS_LICENSE_FILE` and
restart (the environment then wins). The licence gates exactly two features:
single sign-on and [Analytics](/analytics); everything else is in the
community edition.
""",
    ),
)
