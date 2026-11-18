"""The web tab's review of an uploaded 1C file: configuration choice, facts, report.

One call turns a stored upload, a configuration the caller may read (or none) and
a language into the report's blocks, kept for 24 h for their owner. What the
caller may read of a configuration is exactly what an MCP call of theirs may:
the workspace's access decision per repository, secret paths never.

A repository another workspace shares is offered to the members of the receiving
workspace while the share holds (C3-C7 of :mod:`src.access.shares`: live, the
repository still the owner's, the person not switched off, hidden paths hidden),
as long as the share's ceiling on people covers every member; a bigger workspace
falls back to the MCP rule, a share named on one of the caller's own live
read-only developer tokens here (C1, C2). A model sees the
shared code only when the owner allowed asking (C8); otherwise the report is
worded from the template. Every report built on a share is written to the
receiving workspace's MCP call log by share id, which is where the owner reads
the share's usage.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

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
    #: The owner workspace's name when another workspace shares this repository.
    shared_from: str = ""


# (slug, sha) → is it a configuration dump; the answer cannot change for a revision
_DUMP_CACHE: OrderedDict[tuple[str, str], bool] = OrderedDict()
_DUMP_LOCK = threading.Lock()


def _live_tokens(user_id: str, workspace_id: str) -> list:
    """The caller's own live ``pat`` tokens in this workspace (``TokenView``)."""
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.mcp_server import token_store

    with Session(_sync_engine()) as s:
        return token_store.list_rows(s, user_id=user_id, workspace_id=workspace_id,
                                     status="active", kinds=["pat"])


def _held_shares(user_id: str, workspace_id: str) -> dict[str, str]:
    """``{share id: token id}`` for the shares named on the caller's own live tokens
    in this workspace, counting only the tokens whose shares apply over MCP too: a
    superadmin-issued read-only ``pat`` of the ``dev`` profile, issued to this
    person for this workspace."""
    out: dict[str, str] = {}
    for view in _live_tokens(user_id, workspace_id):
        if view.user_id != user_id or view.workspace_id != workspace_id:
            continue
        if view.problem() is None and view.shares_apply:
            for share_id in view.shares:
                out.setdefault(share_id, view.id)
    return out


def _member_shares(session, user_id: str, workspace_id: str) -> list[str]:  # noqa: ANN001
    """Ids of the live shares to this workspace that membership alone opens here.

    Being a member of the receiving workspace is enough in this tab, as long as it
    cannot widen what the owner allowed: the share's ceiling on people
    (``max_people``) must cover every member the owner has not switched off. A
    workspace bigger than that keeps the token rule (:func:`_held_shares`), which
    is where the ceiling is counted. A list of blocked people that cannot be read
    opens nothing; per-person blocking itself is applied by ``live_grants``."""
    from sqlalchemy import select

    from src.access import shares
    from src.db.models import RepoShare, WorkspaceMember

    members = set(session.execute(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
    ).scalars().all())
    if user_id not in members:
        return []
    rows = session.execute(
        select(RepoShare).where(
            RepoShare.grantee_workspace_id == workspace_id,
            RepoShare.revoked_at.is_(None),
            RepoShare.expires_at > datetime.now(UTC),
        )
    ).scalars().all()
    out: list[str] = []
    for row in rows:
        blocked = shares._string_list(row.blocked_user_ids)  # noqa: SLF001 — same reading as the module
        if blocked is None:
            continue
        if len(members - set(blocked)) <= int(row.max_people or 0):
            out.append(row.id)
    return out


def _shared(user_id: str, workspace_id: str, own: dict) -> dict:
    """``{slug: decision}`` of the shared repositories the caller may read now:
    the shares on the caller's own tokens and the ones membership opens
    (:func:`_member_shares`), each still checked by ``live_grants`` (C3-C7).
    Anything unreadable grants nothing."""
    from sqlalchemy.orm import Session

    from src.access import shares
    from src.access.resolver import _sync_engine

    try:
        held = set(_held_shares(user_id, workspace_id))
        with Session(_sync_engine()) as s:
            held.update(_member_shares(s, user_id, workspace_id))
            if not held:
                return {}
            grants = shares.live_grants(s, share_ids=sorted(held), workspace_id=workspace_id,
                                        user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — refuse rather than grant
        logger.warning("onec_review_shares_unreadable ws=%s err=%s", workspace_id,
                       type(exc).__name__)
        return {}
    # A slug the caller's own workspace has is the caller's own, whatever a share says.
    return {slug: g.decision() for slug, g in grants.items() if slug not in own}


def _decisions(user_id: str, is_admin: bool, workspace_id: str) -> dict:
    from src.access.effective import Principal, effective_access, registered_repos

    own = effective_access(Principal(user_id=user_id, is_admin=is_admin), workspace_id)
    return {**own, **_shared(user_id, workspace_id, {**registered_repos(workspace_id), **own})}


def _owner_names(decisions: dict) -> dict[str, str]:
    """``{owner workspace id: name}`` for the shared decisions in ``decisions``."""
    ids = sorted({d.data_workspace for d in decisions.values()
                  if getattr(d, "via_share", None) and getattr(d, "data_workspace", None)})
    if not ids:
        return {}
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import Workspace

    try:
        with Session(_sync_engine()) as s:
            return {w.id: w.name for w in s.execute(
                select(Workspace).where(Workspace.id.in_(ids))).scalars().all()}
    except Exception:  # noqa: BLE001 — a missing name is not a reason to hide the repository
        return {}


def _note_share_read(user_id: str, workspace_id: str, share_id: str) -> None:
    """One row in the receiving workspace's MCP call log, by share id and never by
    name: the owner's usage of a share is counted from that log. Never blocks."""
    try:
        from src.mcp_server import audit

        token_id = _held_shares(user_id, workspace_id).get(share_id)
        audit.write_row({
            "ts": datetime.now(UTC), "workspace_id": workspace_id, "user_id": user_id,
            "token_id": token_id, "kind": "web", "client_id": "web", "tool": "onec_check",
            "profile": "web", "repos": [], "packs": [], "shares": [share_id], "status": "ok",
            "result_bytes": 0, "result_items": 1, "duration_ms": 0, "args_hash": "",
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("onec_review_share_log_failed err=%s", type(exc).__name__)


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
    registry = dict(registered_repos(workspace_id))
    owners = _owner_names(decisions)
    for owner in {d.data_workspace for d in decisions.values() if getattr(d, "via_share", None)}:
        for slug, names in registered_repos(owner).items():
            if slug in decisions and slug not in registry:
                registry[slug] = names
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
        dec = decisions[slug]
        shared_from = (owners.get(dec.data_workspace) or "?") if getattr(dec, "via_share", None) else ""
        out.append(ConfigChoice(
            slug=slug, name=str(meta.get("name") or (names[-1] if names else slug)),
            ref=f"{info.branch}@{info.sha[:8]}", age=info.age, upload=bool(meta or upload_revision(slug)),
            shared_from=shared_from))
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
    out = {"label": slug, "ref": f"{info.branch}@{info.sha[:8]}", "age": info.age}
    if getattr(dec, "via_share", None):
        out.update(share=dec.via_share, ask_ok=bool(getattr(dec, "ask_ok", False)))
    return cfg, out


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
    share, ask_ok = "", True
    if (config_slug or "").strip():
        cfg, info = open_config(user_id, is_admin, workspace_id, config_slug.strip())
        share, ask_ok = str(info.pop("share", "") or ""), bool(info.pop("ask_ok", True))
        comparison = compare(ext, cfg)
    checks = check(ext, cfg)
    facts = build_facts(ext, data, cfg, comparison, checks, config_info=info,
                        now=datetime.now())
    choice = parse_choice(model)
    withheld = bool(share) and not ask_ok and choice.mode != "none"
    if withheld:
        # C8: the owner did not allow its code to reach the receiver's model provider.
        choice = parse_choice("none")
    if share:
        _note_share_read(user_id, workspace_id, share)
    prose, label, failure = ask_prose(facts, lang, choice, workspace_id, user_id)
    note = ""
    if withheld:
        note = ("Пояснення побудовано за шаблоном: власник пошареної конфігурації не дозволив "
                "надсилати її код моделі." if lang != "en" else
                "The explanations come from the template: the owner of the shared configuration "
                "did not allow sending its code to a model.")
    elif prose is not None:
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
