"""The web tab's review of an uploaded 1C file: configuration choice, facts, report.

One call turns a stored upload, a configuration the caller may read (or none) and
a language into the report's blocks, kept for 24 h for their owner. What the
caller may read of a configuration is exactly what an MCP call of theirs may:
the workspace's access decision per repository, secret paths never.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from src.docs.blocks import blocks_to_data
from src.onec import store
from src.onec.checks import check
from src.onec.compare import compare
from src.onec.config import Config, DirSource, GitSource, Source
from src.onec.external import try_inspect
from src.onec.facts import build_facts
from src.onec.review_doc import DEFAULT_LANG, LANGS, build_document, export_name, footer_label
from src.onec.review_llm import ask_prose, parse_choice

logger = logging.getLogger(__name__)

MAX_CONFIGS = 60


class ReviewError(Exception):
    """A refusal the caller can be told: ``status`` is the HTTP code, ``detail`` the text."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class ConfigChoice:
    slug: str
    name: str
    ref: str
    age: str
    upload: bool


# (slug, sha) → is it a configuration dump; the answer cannot change for a revision
_DUMP_CACHE: OrderedDict[tuple[str, str], bool] = OrderedDict()
_DUMP_LOCK = threading.Lock()


def _decisions(user_id: str, is_admin: bool, workspace_id: str) -> dict:
    from src.access.effective import Principal, effective_access

    return effective_access(Principal(user_id=user_id, is_admin=is_admin), workspace_id)


def _source(slug: str, sha: str, dec) -> Source | None:  # noqa: ANN001
    from src.config import InvalidRepoSlug, get_settings
    from src.mcp_server.dev_profile import git_io
    from src.mcp_server.dev_profile.paths import is_secret_path
    from src.repos.upload import upload_revision

    def readable(rel: str) -> bool:
        return bool(dec.code_visible) and not is_secret_path(rel) and dec.path_visible(rel)

    def listable(rel: str) -> bool:
        return not is_secret_path(rel) and dec.path_visible(rel)

    if upload_revision(slug):
        try:
            tree = get_settings().repo_path(slug)
        except InvalidRepoSlug:
            return None
        return DirSource(tree, readable, listable) if tree.is_dir() else None
    clone = git_io.clone_path(slug)
    return GitSource(clone, sha, readable, listable) if clone is not None and sha else None


def _fresh(slugs: list[str]) -> dict:
    from src.mcp_server.dev_profile.freshness import read_freshness

    return read_freshness(list(dict.fromkeys(slugs)))


def _is_dump(slug: str, sha: str, dec) -> bool:  # noqa: ANN001
    key = (slug, sha)
    with _DUMP_LOCK:
        if key in _DUMP_CACHE:
            return _DUMP_CACHE[key]
    src = _source(slug, sha, dec)
    ok = bool(src is not None and Config(src).ok)
    with _DUMP_LOCK:
        _DUMP_CACHE[key] = ok
        while len(_DUMP_CACHE) > 512:
            _DUMP_CACHE.popitem(last=False)
    return ok


def list_configs(user_id: str, is_admin: bool, workspace_id: str, query: str = "",
                 limit: int = MAX_CONFIGS) -> list[ConfigChoice]:
    """The repositories the caller can read that are 1C configuration dumps."""
    from src.access.effective import registered_repos
    from src.repos.upload import read_upload_meta, upload_revision

    decisions = _decisions(user_id, is_admin, workspace_id)
    registry = registered_repos(workspace_id)
    q = (query or "").strip().lower()
    slugs = [s for s, d in sorted(decisions.items())
             if d.code_visible and (not q or q in s.lower()
                                    or any(q in n.lower() for n in registry.get(s, ())))]
    fresh = _fresh(slugs)
    out: list[ConfigChoice] = []
    for slug in slugs:
        info = fresh.get(slug)
        if info is None or not _is_dump(slug, info.sha, decisions[slug]):
            continue
        meta = (read_upload_meta(slug) or {}) if upload_revision(slug) else {}
        names = registry.get(slug, ())
        out.append(ConfigChoice(
            slug=slug, name=str(meta.get("name") or (names[-1] if names else slug)),
            ref=f"{info.branch}@{info.sha[:8]}", age=info.age, upload=bool(meta or upload_revision(slug))))
        if len(out) >= limit:
            break
    return out


def open_config(user_id: str, is_admin: bool, workspace_id: str,
                slug: str) -> tuple[Config, dict]:
    """The configuration ``slug`` and facts about it. Raises ReviewError."""
    decisions = _decisions(user_id, is_admin, workspace_id)
    dec = decisions.get(slug)
    if dec is None or not dec.code_visible:
        raise ReviewError(403, "you cannot read this repository")
    info = _fresh([slug]).get(slug)
    if info is None:
        raise ReviewError(404, "this repository has no indexed revision yet")
    src = _source(slug, info.sha, dec)
    cfg = Config(src, label=f"{slug}@{info.sha[:8]}") if src is not None else None
    if cfg is None or not cfg.ok:
        raise ReviewError(422, "this repository is not a 1C configuration dump")
    return cfg, {"label": slug, "ref": f"{info.branch}@{info.sha[:8]}", "age": info.age}


def run_review(*, user_id: str, is_admin: bool, workspace_id: str, handle: str,
               config_slug: str, model: str, lang: str) -> dict:
    """Build and keep the report. Raises ReviewError."""
    saved = store.get(handle, user_id, workspace_id)
    if saved is None:
        raise ReviewError(404, "no such upload of yours (unknown, expired after 24 h, or "
                               "uploaded by someone else)")
    data = saved.read()
    ext, why = try_inspect(data, saved.file_name)
    if ext is None:
        raise ReviewError(422, f"the file could not be read: {why}")
    lang = lang if lang in LANGS else DEFAULT_LANG
    cfg = info = comparison = None
    if (config_slug or "").strip():
        cfg, info = open_config(user_id, is_admin, workspace_id, config_slug.strip())
        comparison = compare(ext, cfg)
    checks = check(ext, cfg)
    facts = build_facts(ext, data, cfg, comparison, checks, config_info=info,
                        now=datetime.now())
    choice = parse_choice(model)
    prose, label, failure = ask_prose(facts, lang, choice, workspace_id, user_id)
    note = ""
    if prose is not None:
        note = (f"Пояснення сформульовано моделлю {label}; таблиці, числа й уривки коду взято з "
                "перевірки без змін." if lang != "en" else
                f"The explanations were worded by the model {label}; tables, numbers and code "
                "excerpts come from the check unchanged.")
    elif choice.mode != "none":
        note = (f"Пояснення побудовано за шаблоном: модель не відповіла ({failure})."
                if lang != "en" else
                f"The explanations come from the template: the model did not answer ({failure}).")
    blocks = build_document(facts, lang=lang, prose=prose, model_note=note)
    payload = {
        "file_name": saved.file_name, "lang": lang, "level": facts.level,
        "config": config_slug or "", "model": {"mode": choice.mode, "used": prose is not None,
                                               "label": label, "failure": failure},
        "blocks": blocks_to_data(blocks), "footer": footer_label(facts, lang),
        "name": export_name(facts), "created": datetime.now().isoformat(timespec="seconds"),
    }
    payload["id"] = store.save_result(user_id, workspace_id, payload)
    return payload


__all__ = ["ConfigChoice", "ReviewError", "list_configs", "open_config", "run_review"]
