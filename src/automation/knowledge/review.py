"""Pull-request review: agents, policies, prompts, issues, analytics.

Written from src/review (orchestrator, agents, models, issues, compliance),
src/api/routers/{agents,review_policies,issues,pull_requests,compliance}.py,
src/ee/analytics and the pages under web/app/(app)/{reviews,issues,
pull-requests,analytics,admin/review-policies,admin/review-defaults,
admin/agents}.
"""

from __future__ import annotations

from src.automation.knowledge._base import Section

SECTIONS = (
    Section(
        id="agent-prompts",
        title="Agent prompts: workspace-wide and per repository, and which wins",
        keywords=(
            "prompt", "system prompt", "agent", "agents", "override",
            "instruction", "global", "workspace-wide", "per repo",
            "per-repo", "each repo", "every repo", "one repo", "repository",
            "defect", "contract", "security", "verifier", "reset", "default",
            "промпт", "агент", "інструкц", "глобальн", "кожн", "окрем",
            "репозитор", "репо", "перевизнач", "скинут", "типов",
            "инструкц", "глобальн", "кажд", "отдельн", "переопредел",
            "сброс",
        ),
        strong=("prompt", "промпт", "override", "перевизнач", "переопредел",
                "system prompt"),
        body="""
Yes — agent prompts can be changed for the whole workspace AND per
repository, for every agent that has a prompt (defect, contract, security and
the verifier). Two places:

A. Workspace-wide — [AI Agents](/admin/agents) (Code review → "AI Agents"):
1. Open the agent with "Edit prompt".
2. Edit the text in the "System prompt" card and press "Save override".
   Every review in this workspace uses it ("custom prompt active") — except
   in repositories that set their own prompt for that agent.
3. "Reset to default" discards the workspace edit and returns to the built-in
   text.
Each agent card shows "Overridden in repositories: {count}", and the agent's
page lists them under "Repository overrides" — their reviews ignore the
workspace prompt.

B. Per repository — [Review policies](/admin/review-policies):
1. Find the repository (filter "Repository") and open its settings (the
   settings icon), which opens `/admin/review-policies/<repo>`; a link can
   open the tab directly with `?tab=agents`.
2. Tab "Agents & prompts" → card "Agent system prompts (this repo)": one box
   per agent, the verifier included. A badge says whether it is "overridden here" or inherits ("inherits workspace prompt" / "inherits built-in prompt"); an empty box inherits. "Start from inherited" copies the
   inherited text in to edit; "Reset to inherited" clears the override;
   "Preview" shows the composed prompt exactly as the reviewer will send it
   (save first to preview unsaved edits).
3. Press "Save". It applies from the next review of that repository.
Also per repository, tab "Rules": "Prompt template" (rules every agent
receives as a mandatory checklist, up to 20,000 characters) and "Custom rules" — each with a "Title", "Files (glob)" such as `src/api/**/*.py`,
"Report violations as" (a severity, or "Agent decides"), "Applies to" (all
agents, or "Only the selected agents") and "Rule instructions". A rule is
added only when a changed file matches, and only to the agents it is for.

Which prompt an agent uses (the base, highest priority first):
1. The repository's override (policy → "Agents & prompts" → "Agent system prompts (this repo)").
2. The workspace override ([AI Agents](/admin/agents)).
3. The built-in prompt.
The page says the same: "Precedence: this repository's prompt → workspace prompt (AI Agents) → built-in default." Then, always appended on top of
whichever base won: the workspace-wide rules from [LLM Setup](/settings/llm),
this repository's "Prompt template" and the matching custom rules for that
agent, and a language instruction when the review language is not English.
So a repository override replaces the workspace prompt for that repository
only; rules add to the prompt rather than replace it.

Who: editing either needs the editor, admin or owner role in the workspace
(or a global admin); policies additionally need review permission on that
repository when team grants are configured. Others see "Read-only: editing prompts and review policies needs the editor, admin or owner role in this workspace." Careful: a broken prompt can silently produce zero findings —
use "Preview", and "Reset to inherited" or "Reset to default" to recover.
""",
    ),
    Section(
        id="review-policies",
        title="Review policies: every setting per repository",
        keywords=(
            "policy", "policies", "branch", "target branch", "ignore",
            "glob", "severity", "threshold", "comment", "disable", "enable",
            "department", "folder rule", "template", "model", "limit",
            "mcp source", "sentry", "turn off",
            "політик", "гілк", "ігнор", "поріг", "коментар", "вимкн", "увімкн",
            "відділ", "правил", "шаблон",
            "политик", "ветк", "игнор", "порог", "комментар", "выключ",
            "включ", "правил",
        ),
        strong=("policy", "policies", "політик", "политик"),
        body="""
A policy is the review configuration of ONE repository; there is no shared
named policy. A repository without a saved policy runs on defaults: review
on, no extra rules, every target branch. Changes apply from the next review.
Every field is optional; an empty one inherits the workspace or install
default (badges "inherited from workspace", "install default").

[Review policies](/admin/review-policies) lists repositories as "With a saved policy ({count})" and "On defaults ({count})", with an "Enable review" switch per row (switching it off saves a policy). The settings icon opens the
repository's policy. Its tabs (a link can open one with `?tab=general`,
`agents`, `rules`, `comments`, `ignore`, `models` or `mcp`); press "Save" at
the bottom ("Reset to default" deletes the whole policy):

1. "General" — "AI review enabled" (off skips PR review for this repository
   entirely); "Department" (a grouping label only); "Target branches" (only
   PRs whose BASE branch is checked are reviewed; none checked = every
   branch; names match exactly, no globs — `release/*` does not match
   `release/1.2`; add one with "Add a branch by name (if it is not in the list above)").
2. "Agents & prompts" — "Agents in the review" switches (defect, contract,
   security, structural; untouched, they follow the workspace
   [Review defaults](/admin/review-defaults); each row links to the agent's
   model and limits; a disabled agent is never run and costs nothing;
   with all off the review is skipped), the verifier's own switch (off by
   default: a second model pass that drops low-confidence findings and merges
   duplicates, one extra call per review), and "Agent system prompts (this repo)" for every agent, the verifier included.
3. "Rules" — "Prompt template", "Custom rules" (title, files glob, severity,
   which agents; up to 20) and "Suppressed rule ids" (findings with these
   rule ids are dropped before anything is posted; the install default list
   applies until "Override for this repository" is used).
4. "Comments & summary" — card "Inline comments": "Post comments for" ("All findings", "Warning and above (hides nits)", "Critical + error", "Only critical"; findings below it are not posted but are still counted in the
   summary, kept in history and tracked on Issues), "Max inline comments per review" (1–100; empty = install default, 20), "Review language" (language
   of finding titles, bodies and the summary; default = the workspace's).
   Card "Summary & status comments": "Post a PR summary", "Summary instructions" and "Post a “review started” comment" (see the PR comments
   section).
5. "Ignore paths" — "Ignore paths (one glob per line)": files never read by
   this repository's review, on top of the built-in skip list (lockfiles,
   build output, binaries); no negation, at most 200 patterns.
6. "Models & limits" — "Per-agent LLM (model, output ceiling, reasoning)" for
   defect, contract, security and verifier; blank inherits the workspace
   default from [LLM Setup](/settings/llm).
7. "MCP sources" — "MCP context sources" queried for evidence (output added
   as untrusted input): "Name", "URL", "Auth type", "Credentials store key", trigger regexes, allowed tools; "+ Sentry preset".

Who: editor, admin or owner of the workspace (or a global admin); everyone
can read.
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
[Review defaults](/admin/review-defaults) (Code review → "Review defaults")
holds the settings every repository of the workspace uses unless its own
policy sets them: which agents take part ("Agents in the review", the
verifier switch), each agent's model, output ceiling and reasoning,
"Post comments for", "Max inline comments per review", "Review language",
"Post a PR summary" with "Summary instructions", "Post a “review started” comment", ignore paths, target branches and suppressed rule ids. Tabs:
"Agents & models", "Comments & summary", "Ignore paths & branches" (a link can
open one with `?tab=agents`, `?tab=comments` or `?tab=ignore`).

Precedence, for every one of these fields: the repository's own policy → the
workspace review defaults → the install default. A field a repository set
itself is labelled "overridden here" on its policy page and keeps its value
when the workspace default changes; an unset one is labelled "inherited from workspace" (or "install default") and follows this page. "Reset to inherited"
on the policy page hands a field back to the workspace default; "Reset to install default" here hands it back to the installation. Each section says how
many repositories override it.

The per-agent model and limits here are the same workspace settings as the
review agents card on [LLM Setup](/settings/llm); a repository overrides them
on its policy, tab "Models & limits". Agent prompts stay on
[AI Agents](/admin/agents).

Who: everyone in the workspace can read the page; only an owner or admin
(or a global admin) can change it — others see "Read-only: changing the workspace review defaults needs the owner or admin role in this workspace."
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
- structural — deterministic ast-grep rules, no model, no tokens.
- cve — the PR's own dependency changes checked against OSV.
- verifier — a post-processor: a deterministic filter (dedup, confidence
  floor, severity sort) always runs; its LLM false-positive veto is off by
  default and switched on per repository in the policy's "Agents & prompts"
  tab.
- compliance — after the agents, one LLM call per matching rule from
  [Compliance](/admin/compliance).
defect, contract and security are critical: if one fails, the verdict cannot
be APPROVE. A workspace can instead run one headless Claude Code review.

Severities: critical, error, warning, info. Verdict: a failed blocking
compliance check, ≥1 critical or ≥3 errors → REQUEST_CHANGES; any warning or
error → COMMENT; otherwise APPROVE; nothing reviewed → SKIPPED. Skipped when
review is off for the repository, the base branch is not a target branch,
the PR is a draft, the diff is over 500 KB, or every file is ignored.

On the PR: one status comment that says the review started and becomes the
summary when it ends (see the PR comments section), plus inline comments for
findings at or above the policy's "Post comments for" level (worst first, at
most "Max inline comments per review", default 20).

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
   default; per repository it is "Post a “review started” comment".
2. When the review ends, that SAME comment is rewritten into the summary — no
   second thread, updated in place on every new push. With "Post a PR summary" on (the default) it has: Summary (2–5 sentences on what the PR
   changes), Changes walkthrough (a table, one line per changed file, up to 30
   files), Findings (by severity and source, the top ones linked to the code)
   and the verdict, with scope and performance folded below. Summary and
   walkthrough come from one extra cheap model call; if it fails they are
   just left out. "Summary instructions" steers that text for one repository
   (e.g. what to lead with). With "Post a PR summary" off the comment is the
   compact form: the verdict and the findings count only.
3. Inline comments: findings at or above "Post comments for", worst first, at
   most "Max inline comments per review" (install default 20). Lower ones are
   still counted in the summary and tracked on [Issues](/issues).
4. A review skipped (draft, too large, nothing to review) or failing after it
   started rewrites the same comment with the reason — never a new comment.

To change any of this for ONE repository (editor, admin or owner):
1. [Review policies](/admin/review-policies) → the repository's settings
   icon → tab "Comments & summary" (link: `?tab=comments`).
2. Switch "Post a PR summary" or "Post a “review started” comment", fill "Summary instructions", set "Post comments for", "Max inline comments per review" (1–100) or "Review language" (language of finding titles,
   bodies and the summary; empty = the workspace's review language on
   [LLM Setup](/settings/llm)).
3. Press "Save" — it applies from the next review.
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
