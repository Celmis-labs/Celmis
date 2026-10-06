"""Pull-request review: agents, policies, prompts, issues, analytics.

Written from src/review (orchestrator, agents, models, issues, compliance),
src/api/routers/{agents,review_policies,issues,pull_requests,compliance}.py,
src/ee/analytics and the pages under web/app/(app)/{reviews,issues,
pull-requests,analytics,review-settings} (the settings sections are in
web/components/review-settings).
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="agent-prompts",
        title="Agent prompts and guidelines: workspace-wide and per repository, and which wins",
        keywords=(
            "prompt", "system prompt", "agent", "agents", "override",
            "instruction", "global", "workspace-wide", "per repo",
            "per-repo", "each repo", "every repo", "one repo", "repository",
            "defect", "contract", "security", "verifier", "reset", "default",
            "performance", "business logic", "business_logic",
            "base instruction", "базова інструкц", "базовая инструкц",
            "guideline", "guidelines", "replace", "настанов", "рекомендац",
            "доповн", "додати до промпт", "дополн",
            "промпт", "агент", "інструкц", "глобальн", "кожн", "окрем",
            "репозитор", "репо", "перевизнач", "скинут", "типов",
            "инструкц", "глобальн", "кажд", "отдельн", "переопредел",
            "сброс",
        ),
        strong=("prompt", "промпт", "override", "перевизнач", "переопредел",
                "system prompt"),
        body="""
Yes — agent prompts can be changed for the whole workspace AND per
repository, for every agent that has a prompt (defect, contract, security,
performance, business_logic and the verifier). Both live on one page,
[Code review settings](/review-settings) (Code review → "Settings"), section
"Custom prompts". Each agent offers two kinds of customisation:

- "Guidelines (added to the built-in prompt)" — the normal way. Up to 2,000
  characters of what this team wants the agent to look for or how to word
  it. They are ADDED after the agent's own prompt in a block headed
  `Team guidelines for <agent>`, which tells the model they refine focus and
  wording but never override the prompt's scope (changed lines only),
  evidence, severity and output rules. The box shows what the agent already
  checks, so write only what you add.
- "Advanced: replace the built-in prompt" — the whole system prompt is
  swapped for your text (with a warning: the built-in severity calibration,
  scope rules and avoid-list are lost). Guidelines are still added to it.
  Since 2.3.2 an old per-agent prompt that was a short list (no output
  format, no `You are …` opening) was turned into guidelines automatically; long
  prompts stayed replacements.

A. Workspace-wide — scope "Global" (the default), section "Custom prompts"
   (link: `/review-settings?section=prompts`; `&agent=security` opens one):
1. Each agent card has the guidelines box (badge "Custom" or "Not set")
   and, folded, "Advanced: replace the built-in prompt". Press "Save
   settings" at the top. Every repository without its own value uses them.
2. "Reset" clears the guidelines; "Reset to default" restores the built-in
   prompt on save ("Keep custom" undoes it). "Preview" shows the composed
   prompt: the agent's own prompt folded, the added blocks highlighted.
3. A line under the card lists the repositories with their own guidelines
   or their own prompt for that agent — each name links to it.

B. Per repository — pick the repository under "Per repository" on the left
   (search box; the orange number is how many settings it overrides), then
   "Custom prompts" (link: `/review-settings?repo=<repo>&section=prompts`):
1. One card per agent, the verifier included. The guidelines box is badged
   "Custom" or "Inherited" (from the workspace); an empty box inherits.
   "Also keep the workspace guidelines" adds this repository's to the
   workspace's instead of replacing them (repository wins a disagreement).
2. "Advanced: replace the built-in prompt" holds this repository's
   replacement; empty inherits the workspace's prompt, else the built-in.
3. "Repository instructions": extra context for this repository added to
   every agent's prompt (up to 20,000 characters).
4. "Preview" shows the composed prompt exactly as the reviewer sends it
   (saved settings only). Press "Save settings"; it applies from the next
   review of that repository.

Which prompt an agent starts from (highest priority first):
1. The repository's override (its "Advanced: replace the built-in prompt" box).
2. The workspace override (Global, same box).
3. The built-in prompt.
Which guidelines it gets: the repository's, else the workspace's (both when
"Also keep the workspace guidelines" is on).
The order sent: the prompt, the team guidelines, the "Base instruction"
(same section, at most 2,000 characters; set at Global or per repository —
it governs how every suggestion is written), the workspace-wide rules from
[LLM Setup](/settings/llm), the "Repository instructions", the rules from the
rules library that apply, a language instruction when the review language is
not English, and the output format last. The Claude Code engine gets every
agent's guidelines too (a replaced prompt does not reach it).

The chat assistant can set guidelines too — `add to the security agent's
guidelines for repo X: …` (key `agent_prompt_guidelines`); it never
replaces a prompt.

Who: the editor, admin or owner role in the workspace (or a global admin)
edits prompts and guidelines; at Global an editor may change them while the
other workspace defaults stay with owners and admins. Repository settings
additionally need review permission on that repository when team grants are
configured. Careful: a replaced prompt can silently produce zero findings —
prefer guidelines, use "Preview", and "Reset to default" to recover.
""",
    ),
    Section(
        id="review-policies",
        title="Code review settings per repository: every section and field",
        keywords=(
            "policy", "policies", "branch", "target branch", "ignore",
            "glob", "severity", "threshold", "comment", "disable", "enable",
            "department", "folder rule", "template", "model", "limit",
            "mcp source", "sentry", "turn off", "settings", "override",
            "exclude", "pattern",
            "політик", "гілк", "ігнор", "поріг", "коментар", "вимкн", "увімкн",
            "відділ", "правил", "шаблон", "налаштуван",
            "политик", "ветк", "игнор", "порог", "комментар", "выключ",
            "включ", "правил", "настройк",
        ),
        strong=("policy", "policies", "політик", "политик", "review settings",
                "target branch"),
        body="""
[Code review settings](/review-settings) (Code review → "Settings") holds
every review setting in two scopes: "Global" (the workspace defaults) and
"Per repository" (one repository's overrides). The left panel lists
repositories with a search box and, per repository, an orange count of the
settings it overrides; a repository unfolds into its sections. Without
overrides a repository simply inherits Global: review on, every branch.
Changes apply from the next review.

Every field shows where its value comes from: "Overridden" (with a reset
icon and a line saying what the reset gives back),
"Inherited from Global" or "Built-in default". The header holds
"Save settings" (enabled once something changed; Ctrl/Cmd+S), "Discard" and,
for a repository with overrides, "Reset all overrides" (deletes all of the
repository's settings, its prompts and legacy folder rules included).
A link opens one place: `/review-settings?repo=<repo>&section=<section>`
with section `general`, `categories`, `filters`, `prompts`, `summary`,
`rules`, `messages`, `commands`, `learning` or `advanced`. The sections:

1. "General" — "Review this repository" (off skips every PR) and
   "Department" (a grouping label); "Target branches" — names and globs,
   a leading `!` excludes: `develop, !master, !main`; `release/*` matches
   `release/1.2`; an exclusion wins; only exclusions = every other branch;
   empty = every branch. "Check a branch" says whether a PR into a given
   branch would be reviewed. Then "Review draft pull requests", "Review
   cadence" (every push, pause when pushes pile up, only on request), the two
   auto-pause numbers, "Ignored title keywords",
   "Approve when nothing is found", "Request changes on critical findings",
   "Commit status", "Committable suggestions", "“Review started” comment"
   and "Review language".
2. "Review categories" — one card per agent (Bug = defect, Contract,
   Security, Performance, Business logic ("Opt-in"), Structure,
   Dependencies = cve) with an On/Off switch; Compliance and Breaking change
   are "Always on"; the "Verifier" has its own switch. Each card unfolds
   "Model & limits" (model, output ceiling, reasoning, temperature; blank
   inherits [LLM Setup](/settings/llm)). "Inherit every agent" drops this
   repository's agent choices.
3. "Review filters" — "Minimum severity" (a four-step scale: Info, Warning,
   Error, Critical; lower findings are not posted but still counted in the
   summary and tracked on Issues), "Inline comments per review" (1–100,
   default 20), "Apply these filters to rule findings", "Ignored paths" (one
   glob per line; no negation, at most 200) and "Suppressed rules" (rule ids
   dropped before posting).
4. "Custom prompts" — see the agent prompts section.
5. "PR summary" — see the PR comments section.
6. "Rules" — counts of active and pending rules and
   "Open the rules library" ([Review rules](/admin/review-rules) for this
   repository).
7. "Custom messages" — the texts of the started comment and of the
   finished review header.
8. "Commands" — what `@celmis` does in a pull request comment:
   "Answer comment commands" (start-review, review, help and the rest),
   "Answer questions" (a comment that addresses the bot with a question) and
   "Who may give commands" (people with repository access, participants only,
   or anyone). A repository webhook installed before comment commands existed
   shows "needs repair" on the repositories page, and "Repair all webhooks"
   fixes them in one press.
9. "Learning" — whether reviews are told the team's memories ("Tell
   reviews the team's memories"), whether a new memory waits for approval
   ("Require approval for new memories") and "Trusted commenters" (logins or
   e-mails, one per line, whose memories are active at once). "Open the
   memories" goes to the [Memories](/memories) page (owners, admins and
   editors only; a person sees a repository's memories only when they may read
   that repository): short facts about the
   codebase, for the whole workspace, one repository or one directory of it,
   that every review is told when it touches the files they concern; a memory
   from somebody who is not trusted is "Pending" there until an editor
   approves it, and only active ones reach a review.
10. "Advanced" — "MCP evidence sources" (name, URL, auth, credentials key,
   trigger regexes, allowed tools; queried for evidence, output treated as
   untrusted) and "Legacy folder rules" (read-only; recreate them in the
   rules library).

Who: editor, admin or owner of the workspace (or a global admin) edits a
repository; everyone can read.
""",
    ),
    Section(
        id="review-defaults",
        title="Workspace review defaults: one setting for every repository",
        keywords=(
            "review defaults", "workspace default", "default for all",
            "all repositories", "all repos", "every repository", "every repo",
            "whole workspace", "workspace-wide", "globally", "global",
            "inherit", "inherited",
            "типов", "для всіх", "усіх репозитор", "всіх репозитор",
            "глобальн", "успадк",
            "по умолчанию", "для всех", "всех репозитор", "наслед",
        ),
        strong=("review defaults", "workspace default", "типові налаштування",
                "for all repositories", "для всіх репозиторіїв",
                "для всех репозиториев"),
        body="""
The scope "Global" of [Code review settings](/review-settings) (Code
review → "Settings", top of the left panel, "Workspace defaults") holds what
every repository of the workspace uses unless it overrides it — the same
sections and fields as a repository: target branches, drafts, approval,
request-changes, commit status, committable suggestions, the started
comment, review language; which agents take part and each agent's model,
output ceiling and reasoning; "Minimum severity",
"Inline comments per review", ignored paths, suppressed rules; the base instruction and the
workspace agent prompts; the PR summary; the message templates. Here a
value is badged "Workspace default" or "Built-in"; the reset icon returns
it to the built-in default.

Precedence, for every one of these fields: the repository's own value → the
Global (workspace) value → the install default. A field a repository set
itself is "Overridden" on its page and keeps its value when Global changes;
an unset one is "Inherited from Global" (or "Built-in default") and follows
Global. Each repository in the left panel shows how many settings it
overrides, and its unfolded sections show where.

The per-agent model and limits at Global are the same workspace settings as
the review agents card on [LLM Setup](/settings/llm). Opt-in agents:
business_logic is off until switched on (stored as `enabled_agents`);
performance is on. Overview of every repository's overrides:
GET /api/review-settings/overview.

Who: everyone in the workspace can read Global; only an owner or admin (or
a global admin) can change it, and an editor may change the agent prompts
in "Custom prompts". The page says which applies to the reader.
""",
    ),
    Section(
        id="review-how",
        title="How a review runs: agents, verdict, comments, manual run",
        keywords=(
            "review", "reviewer", "run review", "trigger", "manual", "pr",
            "pull request", "merge request", "mr", "verdict", "approve",
            "request changes", "finding", "comment", "summary", "agent",
            "defect", "contract", "security", "structural", "cve", "verifier",
            "performance", "business logic", "business_logic", "category",
            "categories", "n+1", "acceptance criteria", "категорі",
            "skipped", "re-run", "rerun", "apply fix", "draft", "claude code",
            "рев'ю", "ревю", "ревью", "перевірк", "запуст", "вручн",
            "вердикт", "знахідк", "коментар", "пул-реквест", "пулреквест",
            "проверк", "вручную", "находк", "комментар",
        ),
        strong=("verdict", "вердикт", "manual", "вручн", "run review"),
        body="""
Agents (run in parallel on the diff; the graph adds callers, including from
other repositories, when the repository is indexed):
- defect — single-file provable defects: wrong results, dead branches, races,
  unawaited async, swallowed exceptions. The main finder.
- contract — cross-file claims: callers the change breaks, serialization
  boundaries, cross-repo drift; quotes both sides or stays silent.
- security — OWASP Top 10 / CWE Top 25: injection, auth bypass, hardcoded
  secrets, SSRF.
- performance — costs that grow with something: N+1 queries and calls in
  loops, quadratic or repeated work, blocking I/O on hot paths, memory that
  grows with input, DOM thrash. On by default.
- business_logic — the change against what the PR says it does (title,
  description, acceptance criteria, issue keys named) and, when the
  workspace connected Jira on the Connections page, against the Jira task
  the PR names (its acceptance criteria are numbered AC1, AC2 … and cited in
  findings): contradictions, missing parts, required edge cases. Off by
  default — switched on in "Review categories", where "Switch it on when a
  Jira task is found" turns it on for pull requests that name a readable
  task; a PR whose own text and Jira task both say nothing is skipped
  quietly (no findings, the reason noted in the summary's scope details).
- structural — deterministic ast-grep rules, no model, no tokens.
- cve — the PR's own dependency changes checked against OSV.
- verifier — a post-processor: a deterministic filter (dedup, confidence
  floor, severity sort) always runs; its LLM false-positive veto is off by
  default and switched on in "Review categories" ("Verifier").
- compliance — after the agents, one LLM call per matching rule from
  [Compliance](/admin/compliance).
defect, contract, security, performance and business_logic are critical:
if one fails, the verdict cannot be APPROVE (a business_logic skip is not a
failure). The summary counts findings by category: Bug (defect), Contract,
Security, Performance, Business logic, Compliance, Structure, Dependencies
(cve). A workspace can instead run one headless Claude Code review.

Severities: critical, error, warning, info. Verdict: a failed blocking
compliance check, ≥1 critical or ≥3 errors → REQUEST_CHANGES; any warning or
error → COMMENT; otherwise APPROVE; nothing reviewed → SKIPPED. Skipped when
review is off for the repository, the base branch is not a target branch,
the PR is a draft (unless the repository or workspace reviews drafts —
`run_on_drafts`), its title contains an ignored keyword
(`ignored_title_keywords`, any case, such as wip), the review cadence says it
waits (`review_cadence`), the diff is over 500 KB, or every file is ignored.
A person asking (a comment command, the Review button, the CLI, MCP) skips the
draft, title and cadence checks; only a forced request also skips the target
branch rule.

Review cadence (`review_cadence`): `automatic` reviews every push (default);
`auto_pause` reviews every push until a PR gets `auto_pause_pushes` (default 3)
pushes inside `auto_pause_window_minutes` (default 15) — that push and the
next ones wait, and the bot posts one note saying how to continue; `manual`
reviews a PR only when somebody asks. A paused PR resumes with the Resume
button on the pull-requests page, which reviews everything pushed meanwhile.

Review scope (`review_scope`): `incremental` (default) reads only the commits
since the last review that was complete and posted, and closes the earlier
comments whose code those commits changed; an unclear case (first review,
rewritten history, a provider that cannot list commits) reviews the whole PR.
`full` reads the whole PR every time. A forced review is always whole. A push
that brings no new code (same commit, merge commits only) is skipped quietly.

On the PR: one status comment that says the review started and becomes the
summary when it ends (see the PR comments section), plus inline comments for
findings at or above the "Minimum severity" (worst first, at most
"Inline comments per review", default 20).

Run one review by hand — [Review history](/reviews), card "Run a review":
1. Pick "Repository" and "Open pull request", or "or paste a link manually"
   ("PR reference", e.g. `github:owner/repo#42` or a PR/MR URL).
2. Keep "Post comments" on to publish to the PR (off = only recorded here).
3. Press "Run review"; it appears in "History" (queued → running →
   complete / partial / failed / skipped). The ⚡ button on a
   [Repositories](/repositories) row offers the same per open PR.
Admins can "Re-run" a past review (it does not post again). Findings have
Accept / Dismiss / Apply (Apply = commit the suggested fix; GitHub only,
needs Contents write).
""",
    ),
    Section(
        id="pr-comments",
        title="What Celmis posts on a pull request: started comment, summary, inline",
        keywords=(
            "summary", "pr summary", "walkthrough", "started", "status comment",
            "placeholder", "inline", "comment", "comments", "max inline",
            "language", "review language", "turn off", "disable",
            "підсумок", "резюме", "саммарі", "коментар", "мова", "вимкн",
            "розпочат", "почал",
            "итог", "комментар", "язык", "выключ", "отключ",
        ),
        strong=("summary", "walkthrough", "started", "підсумок", "резюме",
                "саммарі", "итог", "inline", "language", "почат", "начал",
                "мова", "мову", "мови", "язык"),
        body="""
What Celmis writes on a pull request (GitHub, GitLab and Bitbucket alike):

1. When a review starts, ONE status comment appears saying Celmis is
   reviewing the PR (head commit, agents, number of files, start time). On by
   default; the switch is "“Review started” comment" (section "General"),
   its text is set in "Custom messages" ("“Review started” message").
2. When the review ends, that SAME comment is rewritten into the summary — no
   second thread, updated in place on every new push. With
   "Write a PR summary" on (the default) it has: Summary (2–5 sentences on what the PR
   changes), Changes walkthrough (a table, one line per changed file, up to 30
   files), Findings (by severity and source, the top ones linked to the code)
   and the verdict, with scope and performance folded below. Summary and
   walkthrough come from one extra cheap model call; if it fails they are
   just left out. "Summary instructions" steers that text (e.g. what to lead
   with). With "Write a PR summary" off the comment is the compact form: the
   verdict and the findings count only. "Where it goes" puts the summary in a
   comment or into the PR description; "On new commits" chooses "Leave it",
   "Append" or "Replace"; "When the author wrote a description" chooses
   "Append", "Complement" or "Replace".
3. Inline comments: findings at or above "Minimum severity", worst first, at
   most "Inline comments per review" (install default 20). Lower ones are
   still counted in the summary and tracked on [Issues](/issues).
4. A review skipped (draft, too large, nothing to review) or failing after it
   started rewrites the same comment with the reason — never a new comment.

To change any of this — for every repository at "Global", or for ONE
under "Per repository" (editor, admin or owner):
1. [Code review settings](/review-settings) → pick the scope on the left →
   section "PR summary" (link: `/review-settings?repo=<repo>&section=summary`).
2. Switch "Write a PR summary", choose "Where it goes" and the new-commit
   behaviour, fill "Summary instructions". The comment level and cap are in
   "Review filters" ("Minimum severity", "Inline comments per review",
   1–100); "Review language" and the started comment are in "General"
   (language of finding titles, bodies and the summary).
3. Press "Save settings" — it applies from the next review.
""",
    ),
    Section(
        id="issues-prs",
        title="Issues and Pull requests pages; fixed-in-next-commit",
        keywords=(
            "issue", "issues", "finding", "fixed", "dismiss", "resolved",
            "open", "status", "pull requests", "pull-requests", "pr list",
            "empty", "nothing", "no issues", "category",
            "проблем", "знахідк", "виправлен", "відхил", "статус", "порожн",
            "нічого", "немає", "пул-реквест",
            "находк", "исправлен", "отклон", "пуст", "ничего", "нет ",
        ),
        strong=("issue", "issues", "fixed", "dismiss"),
        body="""
[Issues](/issues): review findings followed across a pull request's pushes.
Tabs "All", "Open" (default), "Fixed", "Dismissed", "Resolved"; filters by
severity, category (Bug, Security, Performance, Maintainability, Style,
Other), repository; sort newest, oldest, most severe, recently seen. A row
shows "seen in {n} reviews"-style counts and, when fixed automatically,
the commit it was fixed in.

Empty? Issues and [Pull requests](/pull-requests) fill only after a pull
request has been REVIEWED at least once — by the webhook, the poller or a
manual run on [Review history](/reviews). No reviews yet means empty pages;
check that auto-review is on for the repository and the webhook is set up,
or run one review by hand.

Fixed in the next commit: when a later review of the SAME pull request (new
head commit) no longer finds an open issue, it is marked fixed automatically
— but only if the agent that raised it ran again, the file was reviewed
again, that file's diff changed, and the flagged line is gone. Anything
uncertain stays open. An auto-fixed issue that comes back reopens. Human
decisions are never overruled: dismissed (also via a finding's Dismiss),
resolved and fixed-by-hand stay. Closing a PR without merging marks its open
issues resolved; reopening the PR reopens them.

Changing an issue's status needs member or above in the workspace (viewers
read only).

[Pull requests](/pull-requests): every reviewed PR — how often it was
reviewed, the last result ("Success", "Partial", "Skipped", "Failed"), open
suggestions, state ("Open", "Merged", "Closed"). Everyone in the workspace
can read both pages.
""",
    ),
    Section(
        id="analytics",
        title="Review analytics (enterprise)",
        keywords=(
            "analytic", "statistic", "stats", "metric", "kpi", "fix rate",
            "trend", "report", "chart", "dashboard", "enterprise",
            "аналітик", "статистик", "метрик", "тренд", "звіт", "графік",
            "аналитик", "отчет", "график",
        ),
        strong=("analytic", "аналітик", "аналитик"),
        body="""
[Analytics](/analytics) (Code review → "Analytics"): "Review analytics" — how
many reviews ran, how long they took, what they cost, and what happened to
what they found. "Time window" 7 / 30 / 90 days. KPIs: "Reviews", "Average review time" (median and p90), "Total cost" (a model with no price counts as
unknown, not free), "Fix rate" (issues fixed ÷ issues found minus dismissed).
"What happened to the issues found": "Fixed in a later commit", "Marked fixed by hand", "Still open, PR merged", "Still open", "Dismissed". Charts
"Reviews per day", "Findings per day", "Findings by severity", "Issues by category"; "Show as table".

Requirements:
- An enterprise licence that includes analytics. Without it the page says
  "Available in Celmis Enterprise"; whoever runs the server adds the licence
  (see SSO and licence; the edition card is on
  [System status](/admin/health)).
- Role owner, admin or editor of the workspace (or a global admin). Members
  and viewers do not see the tab ("Analytics is for workspace leads").
""",
    ),
    Section(
        id="compliance-deprecations",
        title="Compliance checks and deprecations",
        keywords=(
            "compliance", "regulat", "blocking", "rule", "deprecat", "obsolete",
            "consumer", "removal",
            "комплаєнс", "відповідн", "блокуюч", "застарі", "депрекац",
            "комплаенс", "соответств", "блокирующ", "устаревш",
        ),
        strong=("compliance", "deprecat", "комплаєнс", "комплаенс", "депрекац"),
        body="""
[Compliance](/admin/compliance) — "Compliance checks": natural-language rules
checked by a dedicated agent after the main review. Press "New check" and
fill "Name", "Description (optional)", "Scope" (`workspace` or
`repo:owner/name`), "File glob" (e.g. `migrations/**`), "Rule (natural language)", "Severity", "Blocking (forces REQUEST_CHANGES)", "Enabled". One
LLM call per enabled check whose scope and glob match a changed file. A
failed blocking check becomes a critical finding and forces REQUEST_CHANGES
whatever the other agents say; a non-blocking one becomes a warning.
Creating, editing and deleting checks needs a global admin; everyone can
read them.

[Deprecations](/admin/deprecations): mark a symbol as obsolete (still works,
scheduled for removal) and track who still calls it across indexed
repositories. "Deprecate a symbol": "Symbol" (e.g. `module.function` or
`GET /api/users/{id}`), "Repo slug" (the repository that owns it),
"Reason", "Replacement (optional)", "Target removal (ISO date, optional)",
"Add". "Scan" finds consumers from the code graph (unindexed repositories
report none). MCP clients see the list through `list_deprecations`. Adding,
deleting and scanning needs a global admin.
""",
    ),
)
