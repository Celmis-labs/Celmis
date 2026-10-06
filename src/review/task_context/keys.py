"""Which Jira tasks does a pull request name?

A key is `PROJECT-123`. Three things make finding one harder than a regex:

* Teams write it into a branch name next to words: `PROJ-6066_порезка`,
  `feature/PROJ-6066-порезка-профилей`. `\b` does not see the boundary in the
  first one (`_` is a word character), so the boundaries here are lookarounds
  over letters and digits only.
* A Ukrainian keyboard layout types Cyrillic letters that LOOK Latin
  (`ВР2D-1` for `BP2D-1`). The look-alikes are folded to Latin inside a
  candidate — but only a candidate whose every Cyrillic letter has a Latin
  twin, so a Russian or Ukrainian word that happens to precede `-12` is not
  turned into a key.
* `UTF-8`, `SHA-256`, `ISO-8601` look exactly like keys. The project must be a
  real one: one the admin allow-listed (`task_project_keys`), or one the Jira
  site confirms (the caller passes the connection's project list). Without
  either, the short list of well-known non-projects still applies.

Sources in priority order: title, branch, description, commit messages. At most
`MAX_TASKS` keys are returned, de-duplicated, in that order. A browse link is
only believed when it is on the connection's own Jira host: a link to somebody
else's tracker names somebody else's key.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from typing import Any

from src.review.task_context.models import Source, TaskRef

#: A pull request names at most this many tasks that are read.
MAX_TASKS = 3

#: Letters a project key may be written with: Latin, and the Cyrillic ones the
#: fold below may turn into Latin.
_LETTERS = "A-Za-zА-Яа-яІіЇїЄєҐґ"
_KEY = re.compile(
    rf"(?<![{_LETTERS}0-9])([{_LETTERS}][{_LETTERS}0-9]{{1,9}})-(\d{{1,7}})(?!\d)"
)
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)

#: Cyrillic capitals that look like a Latin letter, and the letter.
_HOMOGLYPHS = {
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "К": "K", "М": "M",
    "О": "O", "Р": "P", "Т": "T", "Х": "X", "І": "I",
}

#: Prefixes that look like a ticket project and are a standard or an
#: encoding — never an issue key, whatever the project list says.
NOT_A_PROJECT = frozenset({
    "AES", "CVE", "CWE", "GHSA", "HTTP", "ISO", "MD", "RFC", "RSA", "SHA",
    "TLS", "UTF", "WCAG",
})


def fold_key(project: str) -> str | None:
    """`project` upper-cased with Cyrillic look-alikes made Latin, or None
    when a Cyrillic letter without a Latin twin is left."""
    out = []
    for ch in project.upper():
        ch = _HOMOGLYPHS.get(ch, ch)
        if not (ch.isascii() and ch.isalnum()):
            return None
        out.append(ch)
    folded = "".join(out)
    return folded if folded[:1].isalpha() else None


def _strip_foreign_urls(text: str, jira_host: str | None) -> str:
    """The text with every URL blanked except links on the Jira host: the
    key inside `https://github.com/org/repo/issues/AB-12` is not ours."""
    from urllib.parse import urlsplit

    def keep(match: re.Match[str]) -> str:
        try:
            host = (urlsplit(match.group(0)).hostname or "").lower()
        except ValueError:      # a netloc that NFKC-folds into `/`, `@`, `:` …: not our link
            return " "
        return match.group(0) if jira_host and host == jira_host else " "

    return _URL.sub(keep, text)


def keys_in(text: str, *, jira_host: str | None = None) -> list[str]:
    """Every task key in `text`, upper-cased and folded, in order, once."""
    found: list[str] = []
    for match in _KEY.finditer(_strip_foreign_urls(text or "", jira_host)):
        project = fold_key(match.group(1))
        if project is None or project in NOT_A_PROJECT:
            continue
        key = f"{project}-{int(match.group(2))}"
        if key not in found:
            found.append(key)
    return found


def extract_task_refs(
    pr: Any,
    commit_messages: Iterable[str] | None = None,
    allowed_projects: Collection[str] | None = None,
    *,
    jira_host: str | None = None,
    limit: int = MAX_TASKS,
) -> list[TaskRef]:
    """The tasks `pr` names, best source first.

    `allowed_projects` (upper-case keys) filters when given and non-empty;
    `commit_messages` are read last. `jira_host` lets browse links on the
    connection's own site count.
    """
    allowed = {p.upper() for p in (allowed_projects or ()) if p}
    sources: list[tuple[Source, str]] = [
        ("title", getattr(pr, "title", "") or ""),
        ("branch", getattr(pr, "head_ref", "") or ""),
        ("description", getattr(pr, "description", "") or ""),
    ]
    sources.extend(("commit", m or "") for m in (commit_messages or ()))
    refs: list[TaskRef] = []
    seen: set[str] = set()
    for source, text in sources:
        for key in keys_in(text, jira_host=jira_host):
            if key in seen:
                continue
            if allowed and key.split("-", 1)[0] not in allowed:
                continue
            seen.add(key)
            refs.append(TaskRef(key=key, source=source))
            if len(refs) >= limit:
                return refs
    return refs


def parse_key(raw: str, *, jira_host: str | None = None) -> str | None:
    """One key from something a person typed (`PROJ-6066`, a browse link on
    the connection's host, `?selectedIssue=PROJ-6066`), or None."""
    found = keys_in(str(raw or ""), jira_host=jira_host)
    return found[0] if found else None
