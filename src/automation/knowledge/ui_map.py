"""Field-by-field reference of the settings screens, written from the UI catalogue.

The other knowledge modules explain the product in prose. This one is the
reference the `show_in_ui` action points into: for each settings section, every
control with what it does, its built-in default and whether it is an advanced
option. It is GENERATED from `src.automation.ui_catalogue.FIELDS`, so the text
the agent quotes when it says "the severity threshold defaults to info" is the
same text the spotlight popover shows, and a control that is added to the
catalogue is known to the agent in the same commit — a test keeps the
catalogue in step with the screens.

Sections are small and keyed on the words people use for the controls (in
English, Ukrainian and Russian), so that a question about "ignore files" reads
the filters reference and not the whole settings page.
"""

from __future__ import annotations

from src.automation import ui_catalogue as cat
from src.automation.knowledge._base import Section

#: destination id → (section id, keywords, strong keywords)
_KEYS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "review_settings.general": ("ui-general", (
        "cadence", "draft", "drafts", "target branch", "branches", "approve",
        "request changes", "language", "auto resolve", "incremental",
        "кадентн", "чернетк", "гілк", "мов", "черновик", "ветк", "язык",
    ), ("cadence", "draft")),
    "review_settings.categories": ("ui-categories", (
        "verifier", "верифікатор", "верификатор", "reasoning", "міркуван",
        "рассужден", "opt-in", "enabled agents", "disable agent",
        "business logic agent", "requirements check", "tracker",
    ), ("verifier", "верифікатор", "верификатор", "reasoning")),
    "review_settings.filters": ("ui-filters", (
        "severity", "threshold", "ignore", "ignored", "glob", "globs",
        "lock file", "max inline", "suppressed rules", "серйозн", "поріг",
        "ігнор", "игнор", "порог", "серьезн", "важлив",
    ), ("severity", "threshold", "glob", "ігнор", "игнор")),
    "review_settings.prompts": ("ui-prompts", (
        "advanced settings", "advanced option", "advanced prompt",
        "розширен", "расширен", "preview prompt", "base instruction",
        "guidelines", "replace prompt", "custom prompt", "extend",
        "кастомн", "власн", "свои промпт", "свій промпт",
    ), ("advanced settings", "розширені", "расширенные")),
    "review_settings.summary": ("ui-summary", (
        "summary", "overview", "completed comment", "commands guide",
        "підсумок", "опис pr", "саммари", "итог",
    ), ("summary", "підсумок")),
    "review_settings.messages": ("ui-messages", (
        "started comment", "message template", "placeholder", "повідомлен",
        "сообщен",
    ), ()),
    "review_settings.commands": ("ui-commands", (
        "command", "commands", "chat in pull request", "who may command",
        "команд", "@celmis",
    ), ("command permission",)),
    "review_settings.learning": ("ui-learning", (
        "memories", "memory", "learning", "suppression", "trusted",
        "пам'ят", "памят", "навчан", "обучен",
    ), ("suppression",)),
    "review_settings.advanced": ("ui-advanced-repo", (
        "mcp source", "evidence source", "folder rules", "mcp",
    ), ("mcp source", "evidence source")),
    "llm": ("ui-llm", (
        "base model", "provider key", "api key", "embeddings model",
        "apply to all agents", "reasoning level",
    ), ("provider key",)),
}


def _line(f: cat.UiField) -> str:
    tail = []
    if f.default:
        tail.append(f"built-in: {f.default}")
    if f.advanced:
        tail.append("advanced option, rarely needed")
    where = f"{f.key}:<agent>" if f.per_agent else f.key
    return f"- {where} — {f.what}" + (f" ({'; '.join(tail)})" if tail else "")


def _sections() -> tuple[Section, ...]:
    out = []
    for dest_id, (sid, kws, strong) in _KEYS.items():
        dest = cat.DESTINATIONS[dest_id]
        rows = "\n".join(_line(f) for f in cat.FIELDS if f.destination == dest_id)
        scope = ("per repository only" if dest.repo_only else
                 "Global (workspace defaults) and per repository"
                 if dest.scoped else "workspace-wide")
        roles = ", ".join(sorted(cat.required_roles(dest, "x" if dest.scoped else None)))
        body = (
            f"{dest.title}: {dest.what}\n\n"
            f"Scope: {scope}. Layering for every setting: repository value, "
            "else workspace default, else installation default, else built-in; "
            "an empty value inherits. Changing it needs one of: "
            f"{roles} (at the workspace level: "
            f"{', '.join(sorted(cat.required_roles(dest, None)))}).\n\n"
            "Controls (the agent can open and highlight each one):\n" + rows)
        out.append(Section(
            id=sid, title=f"Screen reference: {dest.title}",
            keywords=kws, strong=strong, body=body))
    return tuple(out)


SECTIONS = _sections()
