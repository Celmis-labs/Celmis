"""Asking the code, Claude Code sessions, and this agent.

Written from web/app/(app)/{projects,chats,search,claude,automation} and
src/api/routers/claude_code.py, src/automation.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="ask-the-code",
        title="Ask the code: projects, chats and search",
        keywords=(
            "ask", "question", "q&a", "qa", "chat", "project", "search",
            "symbol", "cross-repo", "answer", "citation", "semantic",
            "питан", "запит", "чат", "проєкт", "проект", "пошук", "відповід",
            "вопрос", "поиск", "ответ",
        ),
        strong=("q&a", "project", "проєкт", "проект", "ask the code"),
        body="""
Answers about the code with file:line citations, over one or several
repositories, only from code the asker may see ([Code access](/admin/access)).

Prepare each repository on [Repositories](/repositories): it must be indexed
("indexed" badge) and, for semantic search, have its vault ("Generate vault",
needs an LLM key). Without a vault answers still work, without semantic
search.

1. [Projects](/projects) → "New project": "Name", "Description (optional)",
   "Select repos" (badges "vault ready" / "no vault yet") → "Create".
2. Open the project; "Add repo to project" adds more.
3. "New chat" (needs at least one repository) and ask. Enter sends,
   Shift+Enter is a new line. "Code display: on" quotes real code;
   "Code display: off" explains logic without source. Sources are listed
   under each answer.
[Chats](/chats) lists every conversation in the workspace.
[Search](/search) finds code symbols across every indexed repository and
searches the generated docs semantically.

The chat model is chosen on [LLM Setup](/settings/llm) ("Chat / Q&A"). An
answer that cites nothing usually means the repository is not indexed or has
no vault yet.
""",
    ),
    Section(
        id="claude-sessions",
        title="Claude Code sessions (fix from here, coding agent)",
        keywords=(
            "claude code", "session", "coding agent", "fix from here",
            "setup-token", "subscription", "finish & push", "pull request",
            "fix", "sandbox",
            "сесі", "підписк", "виправ", "агент",
            "сесси", "подписк", "исправ",
        ),
        strong=("claude code", "session", "сесі", "сесси", "setup-token",
                "subscription", "підписк", "подписк"),
        body="""
[Sessions](/claude) (sidebar "Agent"): a Claude Code agent edits a repository
inside your installation and the runner commits, pushes a branch and opens a
pull request (never to the default branch). Sessions keep running after you
close the tab.

Connect a subscription once:
1. Press "Connect Claude account".
2. On any machine with Node.js: `npm install -g @anthropic-ai/claude-code`,
   then `claude setup-token`; finish the browser sign-in and copy the
   `sk-ant-oat…` token the terminal prints.
3. Paste it, choose "Personal — my sessions only", press "Save token". A
   workspace-shared slot ("Workspace — shared API key") takes an Anthropic API
   key and only an owner or admin may save it.
Needs a Claude Pro or Max subscription.

Run one: under "New session" pick up to five repositories or a project,
describe the task (a stack trace, alert text or plain words), choose the mode
and model, press "Start session". "Finish & push" makes the commit and opens
the PR. A provider limit pauses a session instead of losing it; an idle one
finishes after 15 minutes. "Fix from here" on [Dependencies](/dependencies)
and on [Incoming alerts](/alerts) opens a session already holding the
finding or alert.
""",
    ),
    Section(
        id="celmis-agent",
        title="This assistant (the Celmis agent)",
        keywords=(
            "celmis agent", "assistant", "bot", "this chat", "you", "bulk",
            "across repositories", "plan", "run it",
            "асистент", "помічник", "бот", "масов", "план",
            "ассистент", "помощник", "массов",
        ),
        strong=("celmis agent", "assistant", "асистент", "ассистент"),
        body="""
The round button at the bottom right of every page; [full view](/automation)
keeps past chats. It answers how-to questions and does work across a SET of
repositories: list repositories and their state, the last dependency audit and
its findings, show and explain the review settings in force (for the workspace
or one repository, and where each value comes from), recent review runs and
what one run found, review issues, answer a question about the code, find a
symbol, its usages or a file's owner, explain the product; and — shown as a plan you approve with
"Run it" — generate documentation, start a dependency audit, or switch
automatic review on or off (and pin a branch) for many repositories at once,
add or draft review rules, change a review setting (see the review
rules section), review one pull request or every open one of a repository,
re-index repositories, and set the status of review issues. Nothing runs
until you press the second button.

It remembers the conversation: the last few turns of the current chat (your
sentences and what it answered or planned) go with each new message, so
«а для цього репо?» or «do it» refer to what was just discussed. Only your
own turns of this chat in this workspace; "New chat" (+), signing out and
switching workspace start from nothing. Its model is "Celmis agent" on
[LLM Setup](/settings/llm); its spend appears as Celmis agent on
[Usage & cost](/admin/usage).
""",
    ),
    Section(
        id="agent-review-config",
        title="Review rules and review settings from the agent chat",
        keywords=(
            "review rule", "review rules", "rule", "rules", "check", "checks",
            "add rule", "generate rules", "suggest rules", "approve",
            "auto-approve", "request changes", "drafts", "setting", "agent",
            "chat", "this repo",
            "правил", "перевірк", "згенеру", "додай", "увімкн", "схвал",
            "налаштуван", "чат", "агент", "цього репо",
            "проверк", "сгенериру", "добавь", "включ", "одобр", "настройк",
        ),
        strong=("review rule", "review rules", "add rule", "правила рев",
                "правил", "перевірк", "проверк", "approve", "згенеру",
                "сгенериру"),
        body="""
The Celmis agent (round button, or [Celmis agent](/automation)) changes
review configuration from a sentence. Each change is shown first as a plan
card with the exact change — every rule as it will be stored (title,
severity, paths, agents, instruction), or the setting and its new value —
and nothing is written until you press "Run it" ("Cancel" leaves it).

- Add rules: «add review rules for this repo: no raw SQL in handlers;
  every endpoint checks the tenant», «додай до перевірок цього репо
  правила …». One rule per item, optional path glob (`src/api/**`),
  severity (info, warning, error, critical) and agents. Where the
  installation has the review-rules list they are saved as PENDING
  proposals an editor approves (proposing needs `member` or higher and the
  `review` grant on the repository where teams grant access); otherwise they
  are appended to the repository's legacy folder rules (shown under
  "Advanced" in its [settings](/review-settings)) and used from the next
  review — that needs `editor` or higher, like editing the repository's
  settings. At most 10 rules at a time, 20 per repository.
- Draft rules: «згенеруй правила для репо X» — drafts from the code, for
  approval, where the installation can generate them; otherwise the agent
  says so and offers to take the rules from you.
- Change a setting: «увімкни approve для репо X», «set the comment
  threshold to error for the workspace». Settings it can change:
  `approve_when_clean`, `request_changes_on_critical`, `run_on_drafts`,
  `committable_suggestions`, `comment_min_severity`,
  `max_inline_comments`, `summary_enabled`, `review_language`,
  `disabled_agents` — the ones this installation does not have yet are
  refused by name. For the workspace it writes the "Global" scope of
  [Code review settings](/review-settings) (owner or admin); for one
  repository that repository's settings (editor or higher, plus `review` on
  the repository) — the same checks as saving that page, so the agent can
  never do what you could not do there.
- Set an agent's guidelines: «додай до security агента для репо X:
  перевіряй …» — key `agent_prompt_guidelines`, value {agent: text}, at
  most 2,000 characters each, "" removes them. Guidelines are ADDED to the
  agent's built-in prompt; the agent never replaces a prompt (that is
  "Advanced: replace the built-in prompt" on the page). Editor or higher at
  both scopes, like editing prompts on the page.
Follow-ups work: after «add rules for billing-api: …», «і для payments
теж» proposes the same rules for payments.
""",
    ),
    Section(
        id="agent-reviews-and-code",
        title="Reviews, issues, indexing and questions about the code from the agent",
        keywords=(
            "review pr", "review pull request", "review runs", "review run",
            "findings", "issue", "issues", "index", "reindex", "re-index",
            "ask the code", "search code", "who owns", "owner", "usages",
            "рев'ю", "ревю", "знахідк", "індекс", "переіндекс", "власник",
            "кто владеет", "ревью", "находк", "индекс",
        ),
        strong=("review pr", "review pull request", "re-index", "reindex",
                "who owns", "ask the code", "переіндекс"),
        body="""
The Celmis agent (round button, or [Celmis agent](/automation)) covers the
daily review work as well as configuration.

Reads (answered at once, nothing to approve):
- «how did the last reviews of billing-api go» — recent runs with status,
  verdict and findings by severity, linking to [Review history](/reviews).
- «what did review of PR 42 in billing-api find» — one run's findings
  (severity, file, line, title); give a run id, or repository and PR number
  for the latest run.
- «open critical issues» — tracked [Issues](/issues), worst first, with counts
  per status.
- «how does billing-api validate a token» — a written answer from the code Q&A
  with the files it read. It costs one model call, booked as Q&A spend, and
  honours the workspace budget and your research access.
- «where is TokenValidator defined», «who calls it», «who owns
  src/api/auth.py», «summarise the architecture of billing-api» — symbol
  search, usages, ownership and the cached architecture summary, free of
  model calls ([Search](/search)).

Writes (a plan card first, then "Run it"):
- Review a pull request: one by number, or every open pull request of a
  repository (at most 25). Needs the `review` grant on the repository, like
  the Review button. Findings are posted on the pull request unless you say
  not to; the run shows up in [Review history](/reviews).
- Re-index repositories (the code graph search and reviews read): the ones
  you name, or all. Needs `review` on each; one already queued is skipped.
- Set the status of review issues (open, fixed, dismissed, resolved): needs
  the `member` role or higher, the same as the Issues page.

Everything is also available to MCP clients: `review_pr`, `list_reviews`,
`get_review_run`, `index_repo`, `list_issues`, `update_issue`, `ask_code` and
`search_code`, behind the same checks.
""",
    ),
)
