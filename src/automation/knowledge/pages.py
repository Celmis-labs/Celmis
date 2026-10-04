"""The map: every page, where it sits in the sidebar, and what it is for."""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="pages",
        title="Every page and what it is for",
        keywords=(
            "page", "menu", "sidebar", "navigat", "where", "find", "section",
            "сторінк", "розділ", "меню", "де", "знайти", "знайду", "куди",
            "страниц", "раздел", "где", "найти",
        ),
        body="""
The left sidebar has these sections (their names as on screen); each opens a
row of tabs at the top of the page.

- "Dashboard": [Dashboard](/dashboard) (overview and setup banners), "Get started" = [setup wizard](/onboarding), "What you can do" =
  [capabilities](/capabilities).
- "Repositories" (sources): [Repositories](/repositories) (add, index, auto
  review per repository), "Dependencies" = [dependency audits, SBOM and
  evidence pack](/dependencies), "Documentation" = [generated
  docs](/docs), "Repo intel" = [repository intelligence](/admin/intel).
- "Code review": "Review history" = [reviews](/reviews), [Issues](/issues)
  (findings followed across pushes), [Pull requests](/pull-requests),
  "Review rules" = [rules library](/admin/review-rules), "Settings" =
  [code review settings](/review-settings) (Global defaults and every
  repository's overrides, agents, filters, prompts, summary, messages),
  [Analytics](/analytics) (owner/admin/editor, enterprise licence); under
  "More": [Compliance](/admin/compliance), [Deprecations](/admin/deprecations).
- "Ask the code": [Projects](/projects) (ask questions over a group of
  repositories), [Chats](/chats), [Search](/search) (code search).
- "Agent": "Sessions" = [Claude Code sessions](/claude) that edit a
  repository and open a pull request.
- "Monitoring": "Incoming alerts" = [alerts](/alerts), "Notification channels" = [notifications](/admin/notifications), "Job queue" =
  [jobs](/admin/jobs), "Audit log" = [audit](/admin/audit), "Server logs" =
  [logs](/admin/logs) (global admins only).
- "Usage & cost": [spend and budget](/admin/usage).
- "Team & access": "Workspaces" = [workspaces and
  members](/admin/workspaces), [Teams](/admin/teams), "Code access" =
  [per-team repository access](/admin/access).
- "Settings": "Account" = [account](/settings), "LLM Setup" = [LLM keys,
  models and embeddings](/settings/llm), "Model Catalog" =
  [models and prices](/settings/models), "Git connections" =
  [connections](/connections), "MCP" = [MCP for your editor](/settings/mcp).
- "Administration" (global admins only): "System status" =
  [health and edition/licence](/admin/health), [GDPR](/admin/gdpr),
  "OAuth clients" = [OAuth clients](/admin/oauth-clients); superadmin only:
  [Users](/admin/users) and "Access requests" =
  [access requests](/admin/access-requests).
- Outside the sidebar: "Celmis agent" = [this assistant as a full
  page](/automation) with past chats (also the round button at the bottom
  right of every page), "Access request" = [ask for team
  access](/access-request).

The workspace switcher in the top bar (building icon, workspace name and your role) decides which workspace every page shows;
roles are per workspace, so a page may show buttons in one workspace and not
in another.
""",
    ),
    Section(
        id="first-steps",
        title="Getting started: from an empty install to the first review",
        keywords=(
            "start", "begin", "first", "setup", "set up", "onboard", "new",
            "почат", "почина", "перш", "налашт", "з чого", "старт",
            "начат", "начина", "перв", "настро", "с чего",
        ),
        body="""
The [setup wizard](/onboarding) ("Get started") walks through the same order:

1. Model provider: on [LLM Setup](/settings/llm) paste a provider key, save
   and test it (workspace owner/admin). Without a working model nothing is
   reviewed, documented or answered.
2. Git provider: on [Git connections](/connections) save a GitHub, GitLab or
   Bitbucket token (workspace owner/admin). Use a machine account rather than
   your personal token.
3. Repositories: on [Repositories](/repositories) add repositories (pick them
   from the connected provider, or paste a clone URL). Indexing is queued
   automatically and its progress shows on the same page and in the
   [Job queue](/admin/jobs).
4. Review: press "Install webhook" on the repository's row (owner or admin;
   it creates the provider webhook and switches auto-review on), or switch it
   on in the "Auto-review PRs" panel on [Review history](/reviews) and set up
   the webhook by hand; or review one pull request with the "Run a review"
   card there.
5. Tune: [Code review settings](/review-settings) — "Global" for every
   repository, "Per repository" for one repository's overrides; the agent
   prompts are its "Custom prompts" section.
6. Ask: group indexed repositories into a project on [Projects](/projects)
   and ask questions over them.

Findings appear on [Issues](/issues) and [Pull requests](/pull-requests) only
after a pull request has been reviewed at least once.
""",
    ),
)
