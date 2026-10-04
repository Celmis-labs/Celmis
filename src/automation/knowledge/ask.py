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
its findings, explain the product; and — shown as a plan you approve with
"Run it" — generate documentation, start a dependency audit, or switch
automatic review on or off (and pin a branch) for many repositories at once.
Nothing runs until you press the second button. Single-repository work stays
on the pages' own buttons. Its model is "Celmis agent" on
[LLM Setup](/settings/llm); its spend appears as Celmis agent on
[Usage & cost](/admin/usage).
""",
    ),
)
