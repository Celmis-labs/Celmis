"""The fixed text of findings a program wrote, said in the review language.

A structural rule, a dependency advisory and a breaking-change notice are
templates: the finder fills in a name and a count and the sentence around them
is always the same. Nothing is a model's wording there, so the language check
(`src.review.language`) leaves them alone, and until this module they stayed
English under an otherwise Ukrainian comment.

What the finder wrote stays what it was — English, stored on the finding, the
text the issue fingerprint is made of. Each template also records itself on
the finding (`Finding.template`, a `TemplateText`), and the providers call
`shown()` when they write the comment: a copy of the finding in which the
English title, body and suggestion are replaced by the translation, filled with
the same arguments. The copy is only ever rendered; nothing else sees it.

Only the catalogues `src.review.messages` can keep complete are covered
(`uk`; English is the finder's own text). A review in any other language shows
the English, as every fixed word of the bot does. A template with no entry, or
one whose arguments do not fill its entry, also shows the English: a missing
translation costs a language, never a comment.
"""

from __future__ import annotations

import dataclasses
import logging
import string
from collections.abc import Callable
from typing import Any, Final

from src.review import messages
from src.review.models import Finding

logger = logging.getLogger(__name__)

#: Template key -> {"title"|"body"|"suggestion": text with {placeholders}}, or a
#: function from the template's arguments to the same. A field left out keeps
#: the finder's own text (a code snippet is not translated).
Entry = dict[str, str] | Callable[[dict[str, Any]], dict[str, str]]


# ─── Structural rules ──────────────────────────────────────────────
#
# The body of a structural finding is the rule's sentence followed by a code
# block with the matched line; the translation replaces the sentence and the
# block stays as it was. The suggestions of the pack are code, not prose.

def _rule(title: str, body: str) -> dict[str, str]:
    return {"title": title, "body": body}


_UK_STRUCTURAL: Final[dict[str, dict[str, str]]] = {
    "structural.py.empty-except": _rule(
        "Порожній except мовчки ковтає помилки",
        "`except` лише з `pass` ховає баги й ускладнює налагодження. Щонайменше "
        "залогуйте виняток або киньте його знову."),
    "structural.py.bare-except": _rule(
        "Голий `except:` ловить усе, зокрема KeyboardInterrupt",
        "Пишіть `except Exception:`, щоб SystemExit і KeyboardInterrupt, як і "
        "очікується, не ковталися."),
    "structural.py.mutable-default-arg": _rule(
        "Змінний аргумент за замовчуванням спільний для всіх викликів",
        "Значення за замовчуванням `[]` / `{{}}` обчислюється один раз під час "
        "оголошення функції. Усі виклики ділять той самий список чи словник, "
        "звідси несподівані витоки стану."),
    "structural.py.print-debug": _rule(
        "`print()` залишився в продакшн-коді",
        "Використовуйте logging замість print: його легше фільтрувати, "
        "перенаправляти й налаштовувати за рівнями."),
    "structural.js.console-log": _rule(
        "`console.log` залишився в коді",
        "Продакшн-код не має логувати через console. Скористайтеся "
        "структурованим логером або приберіть виклик перед мерджем."),
    "structural.js.loose-equality": _rule(
        "`==` / `!=` приводить типи",
        "`0 == ''` і `null == undefined` обидва істинні. Порівнюйте значення "
        "суворо, через `===` / `!==`, без приведення типів."),
    "structural.js.await-in-foreach": _rule(
        "`await` усередині `Array.forEach` ігнорується",
        "forEach не чекає на Promise зі свого колбека. Цикл завершується до "
        "кінця асинхронної роботи, звідси гонки. Скористайтеся for-of або "
        "Promise.all(map(...))."),
    "structural.js.todo-fixme": _rule(
        "TODO/FIXME без посилання на задачу",
        "Відстежуйте роботу в трекері задач, щоб вона не гнила в коді."),
    "structural.go.error-discarded": _rule(
        "Значення помилки навмисно відкинуто через `_`",
        "Відкидання `err` через `_` ховає збої. Якщо помилку ігноровано "
        "свідомо, додайте коментар, чому."),
}


# ─── An undefined class constant ───────────────────────────────────

_UK_WHERE: Final[dict[str, str]] = {
    "repo": "у репозиторії на цьому коміті",
    "base_plus_diff": "в базовому коміті pull request або в його diff",
    "branch_plus_diff": "в отриманій гілці репозиторію або в diff цього pull request",
}
_UK_NOTE: Final[dict[str, str]] = {
    "repo": "",
    "base_plus_diff": (
        "\n\n_Перевірено за базовим комітом pull request плюс цей diff: його "
        "головний коміт локально був недоступний._"),
    "branch_plus_diff": (
        "\n\n_Перевірено за отриманою гілкою (НОВІШОЮ за pull request) плюс цей "
        "diff: головний коміт pull request локально був недоступний, як і його "
        "базовий коміт._"),
}


def _undefined_constant(args: dict[str, Any]) -> dict[str, str]:
    variant = args.get("variant", "repo")
    near, target = args.get("near"), args.get("target", "")
    hint = f" Найближча константа `{target}` — `{near}`." if near else ""
    return {
        "title": f"Невизначена константа класу {args['shown']}",
        "body": (
            f"`{args['shown']}` використано тут, але константи з назвою "
            f"`{args['name']}` немає {_UK_WHERE[variant]} (шукали серед `const`, "
            f"`case` переліків, `define()`; каталоги vendor включено). PHP "
            f"кидає фатальну помилку `Undefined constant`, коли цей рядок "
            f"виконується.{hint}\n\n```\n{args['snippet']}\n```{_UK_NOTE[variant]}"
        ),
        "suggestion": f"Використайте `{near}`." if near else "",
    }


# ─── A change that breaks other repositories ───────────────────────

_UK_BREAKING: Final[dict[str, str]] = {
    "title": "Ризик зламу сумісності: {name} — споживачів: {n}",
    "body": (
        "Зміна сигнатури зачіпає `{kind}` **{name}** ({detail}). Споживачів в "
        "інших репозиторіях: {n}:\n\n{consumers}"),
}


# ─── A dependency with a known vulnerability ───────────────────────

def _advisory(args: dict[str, Any]) -> dict[str, str]:
    note = (f"оголошена нижня межа {args['version']}" if args.get("floor")
            else f"версія {args['version']}")
    alias = f" / {args['alias']}" if args.get("alias") else ""
    parts = [f"`{args['package']}` ({args['ecosystem']}, {note}) зачеплено "
             f"вразливістю **{args['ident']}**{alias}."]
    if args.get("summary"):
        parts.append(str(args["summary"]))
    parts.append(f"Виправлено у **{args['fixed_in']}**." if args.get("fixed_in")
                 else "OSV поки не вказує виправленої версії.")
    if args.get("floor"):
        parts.append("Версія — нижня межа оголошеного діапазону: встановлена "
                     "може бути новішою, перевірте lock-файл.")
    if args.get("url"):
        parts.append(str(args["url"]))
    return {
        "title": f"{args['package']} {args['version']} має відому вразливість {args['ident']}",
        "body": "\n\n".join(parts),
    }


# ─── A compliance rule ─────────────────────────────────────────────

_UK_COMPLIANCE: Final[dict[str, str]] = {"title": "Відповідність: {name}"}


UK: Final[dict[str, Entry]] = {
    **_UK_STRUCTURAL,
    "structural.undefined-class-constant": _undefined_constant,
    "breaking_change.risk": _UK_BREAKING,
    "cve.advisory": _advisory,
    "compliance.rule": _UK_COMPLIANCE,
}

CATALOGS: Final[dict[str, dict[str, Entry]]] = {"uk": UK}


class _Keep(dict):
    """`format_map` source that leaves an unknown `{name}` as it was."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fill(template: str, args: dict[str, Any]) -> str | None:
    try:
        return string.Formatter().vformat(template, (), _Keep(args))
    except (ValueError, IndexError, AttributeError, KeyError):
        return None


def _texts(key: str, args: dict[str, Any], language: str | None) -> dict[str, str] | None:
    """The translated title/body/suggestion of a template, or None."""
    catalog = CATALOGS.get(messages.normalise_language(language))
    entry = catalog.get(key) if catalog else None
    if entry is None:
        return None
    try:
        raw = entry(args) if callable(entry) else entry
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("finding_text_failed key=%s err_type=%s", key, type(exc).__name__)
        return None
    if callable(entry):
        return raw
    # A plain entry is a template: fill its placeholders with the arguments.
    out = {}
    for name, text in raw.items():
        filled = _fill(text, args)
        if filled is None:
            return None
        out[name] = filled
    return out


def shown(finding: Finding, language: str | None = None) -> Finding:
    """`finding` as the pull request is shown it: the template text in
    `language`, everything else as it was. The finding itself is not changed.

    The English is replaced where it is found, not wholesale: a finding merged
    with another agent's keeps what that agent said around the template's
    sentence. A field that no longer reads as the template wrote it (it was
    edited since) is left alone.
    """
    template = getattr(finding, "template", None)
    if template is None:
        return finding
    texts = _texts(template.key, template.args, language)
    if not texts:
        return finding
    changes: dict[str, Any] = {}
    title = texts.get("title")
    if title and template.title and finding.title == template.title:
        changes["title"] = title
    body = texts.get("body")
    if body and template.body and template.body in (finding.body or ""):
        changes["body"] = finding.body.replace(template.body, body, 1)
    suggestion = texts.get("suggestion")
    if suggestion and template.suggestion and finding.suggestion == template.suggestion:
        changes["suggestion"] = suggestion
    return dataclasses.replace(finding, **changes) if changes else finding


__all__ = ["CATALOGS", "UK", "shown"]
