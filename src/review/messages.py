"""What the bot says on a pull request, in the language the repository reviews in.

Everything Celmis writes to a PR on its own behalf — the lifecycle comments, the
notes for a skipped or failed review, later the greeting, the "review
completed" comment and the command replies — is looked up here by key, never
spelled at the call site:

    t("started.title", "uk")            -> "## 🔄 Celmis перевіряє цей PR…"
    t("status.failed", lang, reason=r)  -> "### ❌ Review failed: …"
    tn("lost_diff", n, lang, files=n)   -> the right plural form

`lang` is the repository's `review_language` (`ReviewBatch.review_language`).
Two catalogs exist, `en` and `uk`; any other language, and any key missing from
a catalog, falls back to English, so a new language never breaks a comment.
The tests keep the two catalogs in step: same keys, same placeholders.

The model's own output (findings, the overview) follows `review_language`
through the prompt instead; this module is only for the fixed text around it.
"""

from __future__ import annotations

import logging
import string
from typing import Final

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE: Final = "en"

EN: Final[dict[str, str]] = {
    # The placeholder posted the moment a review starts.
    "started.title": "## 🔄 Celmis is reviewing this PR…",
    "started.commit": "Commit",
    "started.agents": "Agents",
    "started.files": "Files to review",
    "started.started": "Started",
    "started.footer": "_The results will replace this comment when the review finishes._",
    "started.none": "_none_",
    # A review that ended without a verdict.
    "status.title": "## 🤖 Code Review for PR #{number}",
    "status.skipped": "### ⏭️ Skipped: {reason}",
    "status.failed": "### ❌ Review failed: {reason}",
    "status.commit": "Commit",
    "status.tail_skipped": "_Nothing was reviewed for this commit._",
    "status.tail_failed": (
        "_No review was delivered for this commit. Re-run it once the cause is fixed._"
    ),
    # The brief note for a review that never started.
    "feedback.note": (
        "⏭️ **Celmis did not review this pull request** (commit `{sha}`): {reason}."
    ),
    # The provider lists changed files but sent no diff: not a skip, a failure.
    "lost_diff.one": (
        "the git provider lists {files} changed file but returned no diff, so "
        "nothing could be reviewed — push a new commit or run the review again"
    ),
    "lost_diff.other": (
        "the git provider lists {files} changed files but returned no diff, so "
        "nothing could be reviewed — push a new commit or run the review again"
    ),
    "lost_diff.unknown": (
        "the git provider returned no diff and could not say how many files "
        "changed, so nothing could be reviewed — push a new commit or run the "
        "review again"
    ),
    # The first message of a review: a greeting on the PR's first review, a
    # short "new changes" line on every later push.
    "started.first_title": "## 👋 Hi! I'm Celmis. Starting the review of commit `{sha}` ({files})…",
    "started.update_title": "## 🔄 New changes — updating the review of commit `{sha}` ({files})…",
    "started.files_count.one": "{count} file",
    "started.files_count.other": "{count} files",
    # The closing comment ("Code Review Completed").
    "completed.title": "## Code Review Completed! 🔥",
    "completed.verdict.approve": "**APPROVED** — no blocking findings",
    "completed.verdict.comment": "**COMMENT** — findings to consider",
    "completed.verdict.request_changes": "**CHANGES REQUESTED** — blocking findings",
    "completed.verdict.skipped": "**SKIPPED** — nothing was reviewed",
    "completed.found": "**Found: {n}** ({breakdown})",
    "completed.clean": "_No issues detected._",
    "completed.by_category": "**By category:** {items}",
    "completed.top": "**Top findings:**",
    "completed.more": "_…and {n} more in the inline comments._",
    "completed.in_description": "_The change summary is in the pull request description._",
    "completed.summary": "### Summary",
    "completed.scope": "Files: **{files}** · Lines: **+{added} / -{removed}** · Commit: `{sha}`",
    "completed.details": "Scope and performance",
    "scope.files": "- Files changed: **{n}**",
    "scope.lines": "- Lines: **+{added} / -{removed}**",
    "scope.callers": "- Cross-repo callers: **{n}** (blast radius via materialized edges)",
    "scope.skipped": "- Skipped: {n} {files} (lock/binary/generated/too large)",
    "scope.ignored": "- Ignored by this repository's ignore globs: {n} {files}",
    "scope.not_run": "- Not run: `{agent}` — {why}",
    "perf.time": "Analysis time: **{seconds}s**",
    "perf.agents": "agents: {agents}",
    "perf.none": "none",
    "perf.tokens": "tokens: {tin}/{tout}",
    "completed.powered": "Powered by Code Analyzer · {provenance}",
    "completed.posting_shown": "up to **{n}** shown inline",
    "completed.posting_below": (
        "{n} below the comment threshold ({label}) — recorded in Celmis, not posted"),
    "completed.posting_over": "{n} over the {cap}-comment limit",
    "completed.settings": "[Review settings for this repository]({url})",
    "completed.run": "[This review in Celmis]({url})",
    "guide.title": "### Commands",
    "guide.start_review": "`{handle} start-review` — review the changes since the last review (the whole pull request the first time)",
    "guide.review_force": "`{handle} review --force` — review the whole pull request again, ignoring what was already reviewed",
    "guide.remember": "`{handle} remember: <rule>` — teach the reviewer a rule for this repository",
    "guide.ask": "Mention `{handle}` in a comment thread to ask a question about the change.",
    "guide.help": "`{handle} help` — list the commands",
    "guide.business_logic": "`{handle} -v business-logic <ticket>` — check the change against the ticket's business rules",
    "command.ack": "Working on it — `{command}` from @{actor}.",
    "command.ack_review": "Review requested by @{actor}. I will post the result here.",
    "command.done_queued": "Review queued by @{actor}.",
    "command.done": "Done — see the review summary on this pull request.",
    "command.done_skipped": "The review did not run: {reason}",
    "command.failed": "The command failed. Nothing was changed; try again in a minute.",
    "command.already_queued": "A review of this pull request is already queued or running.",
    "command.resumed": "Reviews are resumed for this pull request.",
    "command.pr_closed": "This pull request is {state}; there is nothing to review.",
    "command.denied": "@{actor}, you are not allowed to command the reviewer on this repository.",
    "command.rate_limited": "Too many commands in a short time. Try again in a while.",
    "command.disabled": "Commands are switched off for this repository.",
    "command.unavailable": "`{command}` is not available on this installation.",
    "command.remember.empty": "Tell me what to remember: `{handle} remember: <rule>`.",
    "command.remember.dir_needed": (
        "`--dir` needs a directory: `--dir=src/api`, or write the comment on a line of a file."),
    "command.remember.scope.workspace": "for every repository of the workspace",
    "command.remember.scope.repo": "for this repository",
    "command.remember.scope.directory": "for one directory of this repository",
    "command.remember.created": "✅ Remembered {scope}: {text}",
    "command.remember.updated": "✅ Updated what I remember {scope}: {text}",
    "command.remember.pending": (
        "📝 Saved {scope} as a suggestion: {text}\nIt takes effect once a maintainer "
        "approves it on the Memories page."),
    "command.remember.skipped": "Nothing was saved: {reason}",
    "command.unknown": "I did not recognise that command.",
    "chat.thinking": "Looking into it, @{actor}…",
    "chat.budget": "I cannot answer right now: this workspace has used its monthly AI budget.",
    "chat.failed": "I could not come up with an answer just now. Try asking again in a minute.",
    "chat.no_model": "No AI model is set up for this workspace, so I cannot answer questions yet.",
    "chat.empty": "Ask me something about this change: `{handle} <your question>`.",
    "command.help_title": "### How to talk to the reviewer",
    "command.help_footer": "Anything else after `{handle}` is treated as a question about the change.",
    "severity.critical": "Critical",
    "severity.error": "Error",
    "severity.warning": "Warning",
    "severity.info": "Info",
    "threshold.critical": "critical only",
    "threshold.error": "critical + error",
    "threshold.warning": "warning and above",
    "threshold.fallback": "the threshold",
    "category.Bug": "Bug",
    "category.Contract": "Contract",
    "category.Security": "Security",
    "category.Performance": "Performance",
    "category.Business logic": "Business logic",
    "category.Compliance": "Compliance",
    "category.Breaking change": "Breaking change",
    "category.Structure": "Structure",
    "category.Dependencies": "Dependencies",
    "category.Other": "Other",
    # The walkthrough table.
    "walk.title": "### Changes walkthrough",
    "walk.file": "File",
    "walk.change": "Change summary",
    "walk.more.one": "_+{count} more file_",
    "walk.more.other": "_+{count} more files_",
    # The findings block of the pull request description.
    "description.found": "### Findings",
    "description.counts": "**{n}** in total: {breakdown}",
    "description.clean": "No issues found.",
    "description.inline": "Up to {n} left as comments in the code.",
    "description.stamp": "Celmis summary · commit `{sha}` · {when}",
    "description.update": "### Update — {when} (commit `{sha}`)",
    # Findings the git provider refused to anchor in the diff.
    "unanchored.title": "### Findings without a place in the diff",
    "unanchored.intro": (
        "_The git provider would not attach these to a line of the diff, so they "
        "are listed here instead._"),
    # Gates a PR can stop at before any model is asked (S4).
    "gate.title": "the pull request title contains the ignored keyword \"{keyword}\"",
    "gate.cadence_manual": (
        "automatic reviews are off for this repository (review cadence: manual) — "
        "comment `{handle} review` to review this PR"),
    "gate.cadence_paused": (
        "automatic reviews are paused on this PR ({pushes} pushes in "
        "{minutes} min) — comment `{handle} start-review` to resume"),
    "gate.cadence_paused_manual": (
        "automatic reviews are paused on this PR — comment `{handle} start-review` "
        "to resume"),
    "pause.notice": (
        "⏸️ **Automatic reviews are paused on this PR** ({pushes} pushes in "
        "{minutes} min). Comment `{handle} start-review` to resume — it reviews "
        "everything since {since} — or `{handle} review` for a one-off review."),
    "pause.notice_manual": (
        "⏸️ **Automatic reviews are paused on this PR.** Comment "
        "`{handle} start-review` to resume — it reviews everything since "
        "{since} — or `{handle} review` for a one-off review."),
    "pause.notice_cadence": (
        "⏸️ **Automatic reviews are off for this repository** (review cadence: "
        "manual). Comment `{handle} review` to review this PR."),
    "pause.since_commit": "`{sha}`",
    "pause.since_start": "the start of the PR",
    # Incremental review: only the commits since the last reviewed one (S5).
    "incremental.commits.one": "{count} new commit",
    "incremental.commits.other": "{count} new commits",
    "incremental.banner": (
        "🔁 **Incremental review** — {commits} since `{sha}`; {files} read."),
    "incremental.threads": (
        "Earlier comments: **{open}** still open, **{resolved}** resolved because "
        "the code they point at changed."),
    "incremental.skip.no_new_commits": "there are no new commits since `{sha}`",
    "incremental.skip.only_merge_commits": (
        "the new commits are merge commits only, nothing of this pull request's own"),
    "incremental.skip.no_reviewable_new_changes": (
        "the new commits change only files this review does not read (ignored, "
        "generated or binary)"),
    # Backlog issues a later change removed from the target branch
    # (src/review/issue_resolver.py): one line in the completed comment.
    "earlier_issues.one": "✅ Resolved {count} earlier issue from merged pull requests:",
    "earlier_issues.other": "✅ Resolved {count} earlier issues from merged pull requests:",
    "earlier_issues.by_pr": " — fixed in PR #{pr}",
    "earlier_issues.by_sha": " — fixed in `{sha}`",
    "earlier_issues.more": "- …and {count} more",
    # Findings the team already judged (the learned filter, mode "on").
    "learned.one": "🧠 {count} finding was left out because your team dismissed a similar one before.",
    "learned.other": "🧠 {count} findings were left out because your team dismissed similar ones before.",
    # The requirements check: the task's acceptance criteria as a checklist in
    # the completed comment (src/review/task_context/checklist.py).
    "requirements.title": "### Requirements check — {task}",
    "requirements.verdict.met": "met",
    "requirements.verdict.no_gap": "no gap reported",
    "requirements.verdict.partial": "partly met",
    "requirements.verdict.missing": "not implemented",
    "requirements.verdict.contradicts": "contradicted",
    "requirements.verdict.unclear": "could not be judged",
    "requirements.more": "- …and {count} more criteria",
    "requirements.footer": "_Jira {issue}, updated {when}_",
    "requirements.footer_plain": "_Jira {issue}_",
    "requirements.findings_note": (
        "_Only the gaps this review reported are marked; the other criteria were "
        "not checked one by one._"),
    "requirements.task_line": "Task: {task}",
    # `@celmis -v business-logic <task>`: a one-off check, answered in the thread.
    "command.ack_business_logic": "🔎 Checking this pull request against the task…",
    "business_logic.title": "## Business-logic check — {task}",
    "business_logic.title_plain": "## Business-logic check",
    "business_logic.clean": "No gap found between this change and the task.",
    "business_logic.gaps": "### Gaps found",
    "business_logic.more": "- …and {count} more",
    "business_logic.not_run": "The check did not run: {reason}.",
    "business_logic.reason.no_connection": "no Jira connection is saved for this workspace (Connections page)",
    "business_logic.reason.no_task": "no Jira key or link to the connected Jira site could be read from what was given",
    "business_logic.reason.pages_off": "reading a page by its link is switched off for this repository (the task_urls_enabled setting)",
    "business_logic.reason.task_off": "reading the Jira task is switched off for this repository",
    "business_logic.reason.project_not_allowed": "the task's project is not on this repository's list of Jira projects (task_project_keys)",
    "business_logic.reason.pages_restricted": "this repository limits Jira tasks to a list of projects (task_project_keys), and a page belongs to none of them",
    "business_logic.failed": "The check could not be finished: {reason}.",
    "business_logic.footer": (
        "_One-off check of commit `{sha}`. Nothing was added to the code and the "
        "review summary is unchanged._"),
    "business_logic.partial_note": "_Not every task could be read: {note}_",
}

UK: Final[dict[str, str]] = {
    "started.title": "## 🔄 Celmis перевіряє цей PR…",
    "started.commit": "Коміт",
    "started.agents": "Агенти",
    "started.files": "Файлів на перевірку",
    "started.started": "Початок",
    "started.footer": "_Результати замінять цей коментар, коли рев'ю завершиться._",
    "started.none": "_немає_",
    "status.title": "## 🤖 Code Review для PR #{number}",
    "status.skipped": "### ⏭️ Пропущено: {reason}",
    "status.failed": "### ❌ Рев'ю не вдалося: {reason}",
    "status.commit": "Коміт",
    "status.tail_skipped": "_Для цього коміту нічого не перевірялося._",
    "status.tail_failed": (
        "_Рев'ю для цього коміту не доставлено. Запустіть його знову, коли причину усунуть._"
    ),
    "feedback.note": (
        "⏭️ **Celmis не перевіряв цей pull request** (коміт `{sha}`): {reason}."
    ),
    "lost_diff.one": (
        "git-провайдер повідомляє про {files} змінений файл, але не віддав diff, тож "
        "нічого було перевіряти — запушіть новий коміт або запустіть рев'ю ще раз"
    ),
    "lost_diff.few": (
        "git-провайдер повідомляє про {files} змінені файли, але не віддав diff, тож "
        "нічого було перевіряти — запушіть новий коміт або запустіть рев'ю ще раз"
    ),
    "lost_diff.many": (
        "git-провайдер повідомляє про {files} змінених файлів, але не віддав diff, тож "
        "нічого було перевіряти — запушіть новий коміт або запустіть рев'ю ще раз"
    ),
    "lost_diff.other": (
        "git-провайдер повідомляє про {files} змінених файлів, але не віддав diff, тож "
        "нічого було перевіряти — запушіть новий коміт або запустіть рев'ю ще раз"
    ),
    "lost_diff.unknown": (
        "git-провайдер не віддав diff і не зміг сказати, скільки файлів змінено, тож "
        "нічого було перевіряти — запушіть новий коміт або запустіть рев'ю ще раз"
    ),
    "started.first_title": "## 👋 Привіт! Я Celmis. Починаю рев'ю коміту `{sha}` ({files})…",
    "started.update_title": "## 🔄 Нові зміни — оновлюю рев'ю коміту `{sha}` ({files})…",
    "started.files_count.one": "{count} файл",
    "started.files_count.few": "{count} файли",
    "started.files_count.many": "{count} файлів",
    "started.files_count.other": "{count} файлів",
    "completed.title": "## Code Review завершено! 🔥",
    "completed.verdict.approve": "**СХВАЛЕНО** — блокуючих зауважень немає",
    "completed.verdict.comment": "**КОМЕНТАР** — є зауваження, які варто розглянути",
    "completed.verdict.request_changes": "**ПОТРІБНІ ЗМІНИ** — є блокуючі зауваження",
    "completed.verdict.skipped": "**ПРОПУЩЕНО** — нічого не перевірялося",
    "completed.found": "**Знайдено: {n}** ({breakdown})",
    "completed.clean": "_Зауважень не знайдено._",
    "completed.by_category": "**За категоріями:** {items}",
    "completed.top": "**Найважливіші зауваження:**",
    "completed.more": "_…і ще {n} у коментарях до коду._",
    "completed.in_description": "_Підсумок змін — в описі pull request._",
    "completed.summary": "### Підсумок",
    "completed.scope": "Файлів: **{files}** · Рядків: **+{added} / -{removed}** · Коміт: `{sha}`",
    "completed.details": "Обсяг і продуктивність",
    "scope.files": "- Змінено файлів: **{n}**",
    "scope.lines": "- Рядків: **+{added} / -{removed}**",
    "scope.callers": "- Викликів з інших репозиторіїв: **{n}** (радіус впливу за матеріалізованими зв'язками)",
    "scope.skipped": "- Пропущено: {n} {files} (lock/бінарні/згенеровані/завеликі)",
    "scope.ignored": "- Ігноровано glob-шаблонами цього репозиторію: {n} {files}",
    "scope.not_run": "- Не запускався: `{agent}` — {why}",
    "perf.time": "Час аналізу: **{seconds}s**",
    "perf.agents": "агенти: {agents}",
    "perf.none": "немає",
    "perf.tokens": "токени: {tin}/{tout}",
    "completed.powered": "Працює на Code Analyzer · {provenance}",
    "completed.posting_shown": "у коді показано до **{n}**",
    "completed.posting_below": (
        "{n} нижче порога коментарів ({label}) — записано в Celmis, не опубліковано"),
    "completed.posting_over": "{n} понад ліміт у {cap} коментарів",
    "completed.settings": "[Налаштування рев'ю для цього репозиторію]({url})",
    "completed.run": "[Це рев'ю в Celmis]({url})",
    "guide.title": "### Команди",
    "guide.start_review": "`{handle} start-review` — перевірити зміни після останнього рев'ю (першого разу весь pull request)",
    "guide.review_force": "`{handle} review --force` — перевірити весь pull request ще раз, ігноруючи вже переглянуте",
    "guide.remember": "`{handle} remember: <правило>` — навчити рев'юера правилу для цього репозиторію",
    "guide.ask": "Згадайте `{handle}` у гілці коментарів, щоб поставити питання про зміну.",
    "guide.help": "`{handle} help` — показати список команд",
    "guide.business_logic": "`{handle} -v business-logic <тікет>` — звірити зміну з бізнес-правилами тікета",
    "command.ack": "Беруся до роботи — `{command}` від @{actor}.",
    "command.ack_review": "Рев'ю запитав @{actor}. Результат з'явиться тут.",
    "command.done_queued": "Рев'ю поставлено в чергу: @{actor}.",
    "command.done": "Готово — підсумок рев'ю дивіться в цьому pull request.",
    "command.done_skipped": "Рев'ю не запускалось: {reason}",
    "command.failed": "Команда не виконалась. Нічого не змінено; спробуйте за хвилину.",
    "command.already_queued": "Рев'ю цього pull request уже в черзі або виконується.",
    "command.resumed": "Рев'ю для цього pull request відновлено.",
    "command.pr_closed": "Цей pull request має стан {state}; рев'ювати нічого.",
    "command.denied": "@{actor}, вам не дозволено керувати рев'юером у цьому репозиторії.",
    "command.rate_limited": "Забагато команд за короткий час. Спробуйте трохи згодом.",
    "command.disabled": "Команди вимкнено для цього репозиторію.",
    "command.unavailable": "Команда `{command}` недоступна в цьому встановленні.",
    "command.remember.empty": "Напишіть, що запам'ятати: `{handle} remember: <правило>`.",
    "command.remember.dir_needed": (
        "`--dir` потрібна тека: `--dir=src/api`, або напишіть коментар до рядка файлу."),
    "command.remember.scope.workspace": "для всіх репозиторіїв робочого простору",
    "command.remember.scope.repo": "для цього репозиторію",
    "command.remember.scope.directory": "для однієї теки цього репозиторію",
    "command.remember.created": "✅ Запам'ятав {scope}: {text}",
    "command.remember.updated": "✅ Оновив те, що пам'ятаю {scope}: {text}",
    "command.remember.pending": (
        "📝 Збережено {scope} як пропозицію: {text}\nНабуде чинності, коли супровідник "
        "підтвердить її на сторінці Memories."),
    "command.remember.skipped": "Нічого не збережено: {reason}",
    "command.unknown": "Я не впізнав цю команду.",
    "chat.thinking": "Дивлюся, @{actor}…",
    "chat.budget": "Зараз не можу відповісти: робочий простір вичерпав місячний бюджет ШІ.",
    "chat.failed": "Не вдалося підготувати відповідь. Спробуйте запитати ще раз за хвилину.",
    "chat.no_model": "Для цього робочого простору не налаштовано модель ШІ, тому я ще не можу відповідати на питання.",
    "chat.empty": "Запитайте щось про цю зміну: `{handle} <ваше питання>`.",
    "command.help_title": "### Як говорити з рев'юером",
    "command.help_footer": "Усе інше після `{handle}` сприймається як питання про зміну.",
    "severity.critical": "Критичні",
    "severity.error": "Помилки",
    "severity.warning": "Попередження",
    "severity.info": "Підказки",
    "threshold.critical": "лише критичні",
    "threshold.error": "критичні та помилки",
    "threshold.warning": "попередження й вище",
    "threshold.fallback": "поріг",
    "category.Bug": "Помилка",
    "category.Contract": "Контракт",
    "category.Security": "Безпека",
    "category.Performance": "Продуктивність",
    "category.Business logic": "Бізнес-логіка",
    "category.Compliance": "Відповідність",
    "category.Breaking change": "Ламка зміна",
    "category.Structure": "Структура",
    "category.Dependencies": "Залежності",
    "category.Other": "Інше",
    "walk.title": "### Огляд змін",
    "walk.file": "Файл",
    "walk.change": "Що змінилося",
    "walk.more.one": "_+ ще {count} файл_",
    "walk.more.few": "_+ ще {count} файли_",
    "walk.more.many": "_+ ще {count} файлів_",
    "walk.more.other": "_+ ще {count} файлів_",
    "description.found": "### Знайдено",
    "description.counts": "Усього **{n}**: {breakdown}",
    "description.clean": "Зауважень не знайдено.",
    "description.inline": "До {n} залишено коментарями в коді.",
    "description.stamp": "Підсумок Celmis · коміт `{sha}` · {when}",
    "description.update": "### Оновлення — {when} (коміт `{sha}`)",
    "unanchored.title": "### Зауваження без місця в diff",
    "unanchored.intro": (
        "_Git-провайдер не прив'язав ці зауваження до рядка diff, тому вони "
        "наведені тут._"),
    # Гейти, на яких PR зупиняється ще до звернення до моделі (S4).
    "gate.title": "назва pull request містить ігнороване ключове слово \"{keyword}\"",
    "gate.cadence_manual": (
        "автоматичні рев'ю вимкнено для цього репозиторію (режим: manual) — "
        "напишіть `{handle} review`, щоб перевірити цей PR"),
    "gate.cadence_paused": (
        "автоматичні рев'ю на цьому PR призупинено ({pushes} пушів за "
        "{minutes} хв) — напишіть `{handle} start-review`, щоб відновити"),
    "gate.cadence_paused_manual": (
        "автоматичні рев'ю на цьому PR призупинено — напишіть "
        "`{handle} start-review`, щоб відновити"),
    "pause.notice": (
        "⏸️ **Автоматичні рев'ю на цьому PR призупинено** ({pushes} пушів за "
        "{minutes} хв). Напишіть `{handle} start-review`, щоб відновити — буде "
        "перевірено все, що змінилося з {since} — або `{handle} review` для "
        "разової перевірки."),
    "pause.notice_manual": (
        "⏸️ **Автоматичні рев'ю на цьому PR призупинено.** Напишіть "
        "`{handle} start-review`, щоб відновити — буде перевірено все, що "
        "змінилося з {since} — або `{handle} review` для разової перевірки."),
    "pause.notice_cadence": (
        "⏸️ **Автоматичні рев'ю вимкнено для цього репозиторію** (режим: "
        "manual). Напишіть `{handle} review`, щоб перевірити цей PR."),
    "pause.since_commit": "коміту `{sha}`",
    "pause.since_start": "початку PR",
    # Інкрементальне рев'ю: лише коміти після останнього переглянутого (S5).
    "incremental.commits.one": "{count} новий коміт",
    "incremental.commits.few": "{count} нові коміти",
    "incremental.commits.many": "{count} нових комітів",
    "incremental.commits.other": "{count} нових комітів",
    "incremental.banner": (
        "🔁 **Інкрементальне рев'ю** — {commits} після `{sha}`; прочитано: {files}."),
    "incremental.threads": (
        "Попередні коментарі: **{open}** ще відкриті, **{resolved}** закрито, бо код, "
        "на який вони вказували, змінився."),
    "incremental.skip.no_new_commits": "нових комітів після `{sha}` немає",
    "incremental.skip.only_merge_commits": (
        "нові коміти лише злиття, власних змін цього pull request немає"),
    "incremental.skip.no_reviewable_new_changes": (
        "нові коміти змінюють лише файли, які це рев'ю не читає (ігноровані, "
        "згенеровані чи бінарні)"),
    "earlier_issues.one": "✅ Закрито {count} раніше знайдену проблему з об'єднаних pull request:",
    "earlier_issues.few": "✅ Закрито {count} раніше знайдені проблеми з об'єднаних pull request:",
    "earlier_issues.many": "✅ Закрито {count} раніше знайдених проблем з об'єднаних pull request:",
    "earlier_issues.other": "✅ Закрито {count} раніше знайдених проблем з об'єднаних pull request:",
    "earlier_issues.by_pr": " — виправлено в PR #{pr}",
    "earlier_issues.by_sha": " — виправлено в `{sha}`",
    "earlier_issues.more": "- …і ще {count}",
    "learned.one": "🧠 {count} зауваження пропущено, бо команда раніше відхилила схоже.",
    "learned.few": "🧠 {count} зауваження пропущено, бо команда раніше відхилила схожі.",
    "learned.many": "🧠 {count} зауважень пропущено, бо команда раніше відхилила схожі.",
    "learned.other": "🧠 {count} зауважень пропущено, бо команда раніше відхилила схожі.",
    "requirements.title": "### Перевірка вимог — {task}",
    "requirements.verdict.met": "виконано",
    "requirements.verdict.no_gap": "розбіжностей не знайдено",
    "requirements.verdict.partial": "виконано частково",
    "requirements.verdict.missing": "не реалізовано",
    "requirements.verdict.contradicts": "суперечить задачі",
    "requirements.verdict.unclear": "не вдалося оцінити",
    "requirements.more": "- …ще критеріїв: {count}",
    "requirements.footer": "_Jira {issue}, оновлено {when}_",
    "requirements.footer_plain": "_Завдання Jira {issue}_",
    "requirements.findings_note": (
        "_Позначено лише розбіжності, які знайшов цей рев'ю; решту критеріїв "
        "окремо не перевіряли._"),
    "requirements.task_line": "Задача: {task}",
    "command.ack_business_logic": "🔎 Звіряю цей pull request із задачею…",
    "business_logic.title": "## Перевірка бізнес-логіки — {task}",
    "business_logic.title_plain": "## Перевірка бізнес-логіки",
    "business_logic.clean": "Розбіжностей між цією зміною і задачею не знайдено.",
    "business_logic.gaps": "### Знайдені розбіжності",
    "business_logic.more": "- …і ще {count}",
    "business_logic.not_run": "Перевірка не запускалася: {reason}.",
    "business_logic.reason.no_connection": "для цього workspace не збережено підключення до Jira (сторінка Connections)",
    "business_logic.reason.no_task": "з наданого тексту не вдалося прочитати ключ Jira чи посилання на підключений сайт Jira",
    "business_logic.reason.pages_off": "читання сторінки за посиланням вимкнено для цього репозиторію (налаштування task_urls_enabled)",
    "business_logic.reason.task_off": "читання задачі Jira вимкнено для цього репозиторію",
    "business_logic.reason.project_not_allowed": "проєкту задачі немає в списку проєктів Jira цього репозиторію (task_project_keys)",
    "business_logic.reason.pages_restricted": "цей репозиторій обмежує задачі Jira списком проєктів (task_project_keys), а сторінка не належить жодному з них",
    "business_logic.failed": "Перевірку не вдалося завершити: {reason}.",
    "business_logic.footer": (
        "_Разова перевірка коміту `{sha}`. До коду нічого не додано, підсумок "
        "рев'ю не змінено._"),
    "business_logic.partial_note": "_Не всі задачі вдалося прочитати: {note}_",
}

CATALOGS: Final[dict[str, dict[str, str]]] = {"en": EN, "uk": UK}


def normalise_language(lang: str | None) -> str:
    """A catalog code: `uk-UA` -> `uk`; anything without a catalog -> `en`."""
    base = str(lang or "").strip().replace("_", "-").split("-")[0].lower()
    return base if base in CATALOGS else DEFAULT_LANGUAGE


def resolve_language(override: str | None, workspace_id: str | None = None) -> str:
    """The language a repository's bot text is written in.

    The repository's own `review_language`, else the workspace's (the same
    order the review prompts use), else English. Never raises: a missing
    workspace config is English, not a failed review.
    """
    if override and str(override).strip():
        return normalise_language(override)
    if workspace_id:
        try:
            from src.api.routers.llm import _load_workspace_config

            return normalise_language(
                _load_workspace_config(workspace_id).get("review_language"))
        except Exception:  # noqa: BLE001 — a language lookup never fails a review
            return DEFAULT_LANGUAGE
    return DEFAULT_LANGUAGE


class _Keep(dict):
    """`format_map` source that leaves an unknown `{name}` as it was."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def t(key: str, lang: str | None = None, **kw: object) -> str:
    """The text for `key` in `lang` (English when the catalog lacks it)."""
    code = normalise_language(lang)
    template = CATALOGS[code].get(key)
    if template is None:
        template = EN.get(key)
    if template is None:
        logger.warning("messages_unknown_key key=%s", key)
        return key
    if not kw:
        return template
    try:
        return string.Formatter().vformat(template, (), _Keep(kw))
    except (ValueError, IndexError, AttributeError):
        return template


def plural_form(n: int, lang: str | None = None) -> str:
    """CLDR plural category used by the catalogs: one / few / many / other."""
    code = normalise_language(lang)
    n = abs(int(n))
    if code == "uk":
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    return "one" if n == 1 else "other"


def tn(key: str, n: int, lang: str | None = None, **kw: object) -> str:
    """`t("<key>.<form>")` for the plural form of `n` in `lang`."""
    form = plural_form(n, lang)
    code = normalise_language(lang)
    name = f"{key}.{form}"
    if name not in CATALOGS[code]:
        name = f"{key}.other"
    return t(name, lang, **kw)
