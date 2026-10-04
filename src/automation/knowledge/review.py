"""Pull-request review: agents, policies, prompts, issues, analytics.

Written from src/review (orchestrator, agents, models, issues, compliance),
src/api/routers/{agents,review_policies,issues,pull_requests,compliance}.py,
src/ee/analytics and the pages under web/app/(app)/{reviews,issues,
pull-requests,analytics,admin/review-policies,admin/agents}.
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
repository. Two places:

A. Workspace-wide — [AI Agents](/admin/agents) (Code review → "AI Agents"):
1. Open the agent (defect, contract, security or verifier) with "Edit prompt".
2. Edit the text in the "System prompt" card and press "Save override".
   Every review in this workspace then uses it ("custom prompt active").
3. "Reset to default" discards the workspace edit and returns to the built-in
   text.

B. Per repository — [Review policies](/admin/review-policies):
1. Find the repository (filter "Repository") and open its settings (the
   settings icon), which opens `/admin/review-policies/<repo>`.
2. Tab "Agents" → card "Agent system prompts (this repo)": a field per agent
   (defect, contract, security). Blank means inherit ("Leave blank to inherit"). "Preview" shows the effective prompt exactly as the reviewer
   will send it (save first to preview unsaved edits).
3. Press "Save". It applies from the next review of that repository.
Also per repository, tab "Prompt & rules": "Prompt template" (rules every
agent receives as a mandatory checklist for this repository, up to 20,000
characters) and "Folder rules" (a glob such as `src/api/**/*.py` plus rules
added only when a changed file matches).

Which prompt an agent uses (the base, highest priority first):
1. The repository's override (policy → Agents → "Agent system prompts (this repo)").
2. The workspace override ([AI Agents](/admin/agents)).
3. The built-in prompt.
Then, always appended on top of whichever base won: the workspace-wide rules
from [LLM Setup](/settings/llm), this repository's "Prompt template" and
matching "Folder rules", and a language instruction when the review language
is not English. So a repository override replaces the workspace prompt for
that repository only; rules add to the prompt rather than replace it.

Who: editing either needs the editor, admin or owner role in the workspace
(or a global admin); policies additionally need review permission on that
repository when team grants are configured. Others see "Read-only: editing prompts and review policies needs the editor, admin or owner role in this workspace." Careful: a broken prompt can silently produce zero findings —
use "Preview", and "Reset to default" to recover.
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

[Review policies](/admin/review-policies) lists repositories as "With a saved policy ({count})" and "On defaults ({count})", with an "Enable review" switch per row (switching
it off saves a policy). The settings icon opens the repository's policy with
five tabs; press "Save" at the bottom ("Reset to default" deletes the policy):

1. "General & branches"
   - "AI review enabled" — off skips PR review for this repository entirely.
   - "Department" — a grouping label only; it changes nothing in reviews.
   - "Target branches" — only PRs whose BASE branch is checked are reviewed;
     none checked = every branch. Names match exactly (no globs:
     `release/*` does not match `release/1.2`); add one not listed with "Add a branch by name (if it is not in the list above)".
   - "Review output": "Post comments for" = "All findings", "Warning and above (hides nits)", "Critical + error" or "Only critical" — findings below it
     are not posted but are still counted in the summary, kept in history and
     tracked on Issues. "Ignore paths (one glob per line)" skips paths such
     as `vendor/**` on top of the built-in skip list (no negation, at most 200
     patterns).
2. "Prompt & rules" — "Prompt template" and "Folder rules" (see the prompts
   section).
3. "Models & limits" — "Per-agent LLM (model, output ceiling, reasoning)" for
   defect, contract, security and verifier; blank inherits the workspace
   default from [LLM Setup](/settings/llm). Precedence: this policy >
   workspace agent settings > review profile > install default.
4. "MCP sources" — "MCP context sources" whose output is added to the review
   context (as untrusted input): "Name", "URL", "Auth type", "Credentials store key", trigger regexes, allowed tools; "+ Sentry preset".
5. "Agents" — "Agents in the review" switches (defect, contract, security,
   structural, and the verifier). A disabled agent is never run and costs
   nothing; with all off the review is skipped. Plus the per-repository
   agent prompts.

Who: editor, admin or owner of the workspace (or a global admin); everyone
can read.
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
  default and switched on per repository in the policy's "Agents" tab.
- compliance — after the agents, one LLM call per matching rule from
  [Compliance](/admin/compliance).
defect, contract and security are critical: if one fails, the verdict cannot
be APPROVE. A workspace can instead run one headless Claude Code review.

Severities: critical, error, warning, info. Verdict: a failed blocking
compliance check, ≥1 critical or ≥3 errors → REQUEST_CHANGES; any warning or
error → COMMENT; otherwise APPROVE; nothing reviewed → SKIPPED. Skipped when
review is off for the repository, the base branch is not a target branch,
the PR is a draft, the diff is over 500 KB, or every file is ignored.

On the PR: inline comments for findings at or above the policy's "Post comments for" level (at most 20, worst first), plus ONE summary comment
updated in place on every push (verdict, counts by severity, scope, time and
tokens).

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
