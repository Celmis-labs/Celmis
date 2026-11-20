"""Import a knowledge base into a repository's documentation, and keep it current.

Cards land in the repository's vault under ``kb/`` — next to the notes the
generator writes, never on top of them — and are embedded into the same
search index, so semantic search, Q&A and MCP find them like any note. The
front matter is kept twice: as written (``fm``) and as Celmis reads it
(``kb``: id, status, 1C objects in canonical form, links, staleness).

An import is a snapshot of one source: a card that is no longer in it is
removed, a card whose text and checks did not change is not rewritten (and not
embedded again). Where the cards came from is remembered in the vault's
``.celmis_kb.json``; a knowledge base imported from a connected repository is
imported again after every index of that repository, and the cards of every
knowledge base bound to a 1C configuration are checked again after every
index of the configuration.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter

from src.kb import changes
from src.kb.cards import Card, CardProblem, parse_card
from src.kb.objects import ChangeIndex, canonical_all, graph_lookup, staleness

logger = logging.getLogger(__name__)

KB_DIR = "kb"
BINDING = ".celmis_kb.json"
MD_SUFFIXES = (".md", ".markdown")
MAX_DEPTH = 12
MAX_PATH_CHARS = 240
#: Entries of an archive looked at (not kept) before it is refused as a bomb.
MAX_ENTRIES = 50_000


class ImportRefused(ValueError):
    """The whole upload is refused; nothing was changed."""


@dataclass
class ImportReport:
    repo: str
    code_repo: str = ""
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # {path, reason}
    warnings: list[dict] = field(default_factory=list)  # {path, message}
    missing_objects: list[dict] = field(default_factory=list)  # {path, objects}
    stale: list[str] = field(default_factory=list)
    embedded: int = 0
    embed_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "code_repo": self.code_repo,
            "created": self.created, "updated": self.updated,
            "unchanged": self.unchanged, "deleted": self.deleted,
            "skipped": self.skipped, "warnings": self.warnings,
            "missing_objects": self.missing_objects, "stale": self.stale,
            "embedded": self.embedded, "embed_failures": self.embed_failures[:5],
        }


# ─── reading an upload ────────────────────────────────────────────────


def _limits() -> tuple[int, int, int]:
    from src.config import get_settings

    s = get_settings()
    return int(s.kb_max_files), int(s.kb_max_file_bytes), int(s.kb_max_upload_bytes)


def safe_rel(name: str) -> str | None:
    """A knowledge-base path for an upload or checkout name, or None to skip it.

    Forward slashes, no absolute part, no ``..``, no hidden folder, no secret
    file's name, only markdown, bounded depth and length. ``.markdown`` is
    stored as ``.md``.
    """
    n = (name or "").replace("\\", "/").strip()
    if not n or n.startswith("/") or ":" in n.split("/", 1)[0]:
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") or p == "__MACOSX" for p in parts):
        return None
    if len(parts) > MAX_DEPTH:
        return None
    low = parts[-1].lower()
    if not low.endswith(MD_SUFFIXES):
        return None
    if low.endswith(".markdown"):
        parts[-1] = parts[-1][: -len(".markdown")] + ".md"
    rel = "/".join(parts)
    if len(rel) > MAX_PATH_CHARS:
        return None
    from src.security.secret_files import is_secret_path

    # The floor every reader of the vault applies (`secrets/x.md`, `.env.md`):
    # a note no reader would open must not be written, nor embedded for search.
    return None if is_secret_path(f"{KB_DIR}/{rel}") or is_secret_path(rel) else rel


def _decode(rel: str, data: bytes, report: ImportReport) -> str | None:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.skipped.append({"path": rel, "reason": "not UTF-8 text"})
        return None


def read_upload(files: list[tuple[str, bytes]], report: ImportReport) -> list[tuple[str, str]]:
    """Markdown files from uploaded ``(name, bytes)``; a ``.zip`` is opened.

    Raises :class:`ImportRefused` when the upload is over a limit as a whole.
    """
    max_files, max_file, max_total = _limits()
    if sum(len(d) for _, d in files) > max_total:
        raise ImportRefused(f"the upload is larger than {max_total // 1_000_000} MB")
    out: list[tuple[str, str]] = []
    total = 0

    def keep(name: str, data: bytes) -> None:
        nonlocal total
        rel = safe_rel(name)
        if rel is None:
            if name.lower().endswith(MD_SUFFIXES):
                report.skipped.append({"path": name[:MAX_PATH_CHARS], "reason": "path not allowed"})
            return
        if len(data) > max_file:
            report.skipped.append({"path": rel, "reason": f"larger than {max_file // 1000} KB"})
            return
        total += len(data)
        if total > max_total:
            raise ImportRefused(f"the files unpack to more than {max_total // 1_000_000} MB")
        text = _decode(rel, data, report)
        if text is not None:
            out.append((rel, text))
        if len(out) > max_files:
            raise ImportRefused(f"more than {max_files} markdown files in one import")

    for name, data in files:
        if name.lower().endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                raise ImportRefused(f"{name} is not a readable zip archive") from None
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ImportRefused("the archive has too many entries")
            for info in infos:
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    report.skipped.append({"path": info.filename[:MAX_PATH_CHARS], "reason": "symbolic link"})
                    continue
                if info.file_size > max_file:
                    if info.filename.lower().endswith(MD_SUFFIXES):
                        report.skipped.append({"path": info.filename[:MAX_PATH_CHARS],
                                               "reason": f"larger than {max_file // 1000} KB"})
                    continue
                if safe_rel(info.filename) is None:
                    keep(info.filename, b"")
                    continue
                keep(info.filename, zf.read(info))
        else:
            keep(name, data)
    return out


def read_checkout(root: Path, prefix: str, report: ImportReport) -> list[tuple[str, str]]:
    """Markdown files under ``root/prefix`` of a repository checkout (no links)."""
    max_files, max_file, _ = _limits()
    pre = (prefix or "").strip().strip("/")
    if pre and safe_rel(pre + "/x.md") is None:
        raise ImportRefused("the folder is not a plain relative path")
    base = root / pre if pre else root
    out: list[tuple[str, str]] = []
    if not base.is_dir():
        raise ImportRefused(f"no folder {pre or '/'} in the repository")
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = safe_rel(full.relative_to(base).as_posix())
            if rel is None:
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > max_file:
                report.skipped.append({"path": rel, "reason": f"larger than {max_file // 1000} KB"})
                continue
            text = _decode(rel, full.read_bytes(), report)
            if text is not None:
                out.append((rel, text))
            if len(out) > max_files:
                raise ImportRefused(f"more than {max_files} markdown files in one import")
    return out


# ─── the binding ──────────────────────────────────────────────────────


def vault_dir(slug: str) -> Path:
    from src.config import get_settings

    return get_settings().repo_vault_path(slug)


def load_binding(slug: str) -> dict | None:
    try:
        data = json.loads((vault_dir(slug) / BINDING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_binding(slug: str, binding: dict) -> None:
    d = vault_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (BINDING + ".tmp")
    tmp.write_text(json.dumps(binding, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / BINDING)


def bindings_to(code_repo: str) -> list[str]:
    """Slugs whose knowledge base checks its cards against ``code_repo``."""
    from src.config import get_settings

    root = get_settings().vault_dir / "projects"
    out: list[str] = []
    try:
        for path in root.glob(f"*/{BINDING}"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            slug = str(data.get("repo") or "")
            if slug and data.get("code_repo") == code_repo:
                out.append(slug)
    except OSError:
        return []
    return sorted(out)


# ─── import ───────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _existing(directory: Path) -> dict[str, tuple[dict, str]]:
    """Vault path → (front matter, body) of every imported card on disk."""
    out: dict[str, tuple[dict, str]] = {}
    root = directory / KB_DIR
    if not root.is_dir():
        return out
    from src.vault.reader import vault_note_files

    for full in vault_note_files(root):
        try:
            post = frontmatter.load(full)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(post.metadata, dict) and isinstance(post.metadata.get("kb"), dict):
            out[f"{KB_DIR}/{full.relative_to(root).as_posix()}"] = (post.metadata, post.content)
    return out


def _kb_block(card: Card, *, code_repo: str, lookup, index: ChangeIndex | None,  # noqa: ANN001
              report: ImportReport, vault_path: str) -> dict[str, Any]:
    objects, not_objects = canonical_all(card.objects)
    missing: list[str] = []
    if lookup is not None:
        missing = [o for o in objects if lookup(o) is None]
    for w in card.warnings:
        report.warnings.append({"path": vault_path, "message": w})
    if not_objects:
        report.warnings.append({"path": vault_path, "message":
                                "not 1C object names (expected Type.Name): " + ", ".join(not_objects[:10])})
    if missing:
        report.missing_objects.append({"path": vault_path, "objects": missing})
    st = staleness(index, objects, card.source_sha)
    if st.state == "stale":
        report.stale.append(vault_path)
    kb: dict[str, Any] = {
        "origin": "import", "id": card.id, "status": card.status,
        "src_path": card.src_path, "sha": card.sha,
    }
    for key in ("domain", "author", "lang", "source"):
        value = getattr(card, key)
        if value:
            kb[key] = value
    if card.source_sha:
        kb["source_sha"] = card.source_sha
    for key in ("tags", "related", "replaces"):
        if getattr(card, key):
            kb[key] = list(getattr(card, key))
    if objects:
        kb["objects"] = objects
    if not_objects:
        kb["not_objects"] = not_objects
    if lookup is not None and objects:
        kb["objects_missing"] = missing
    if code_repo:
        kb["code_repo"] = code_repo
    kb["staleness"] = st.as_dict()
    kb["effective_status"] = "needs-review" if st.state == "stale" else card.status
    return kb


def _comparable(kb: dict) -> dict:
    """``kb`` without what changes on every check: when it ran, and the head
    commit a still-fresh card was compared with."""
    out = {k: v for k, v in kb.items() if k != "checked_at"}
    st = dict(out.get("staleness") or {})
    st.pop("head", None)
    out["staleness"] = st
    return out


def _same(old_kb: dict, new_kb: dict) -> bool:
    return _comparable(old_kb) == _comparable(new_kb)


def _delete_note(slug: str, workspace_id: str | None, vault_path: str, report: ImportReport | None,
                 *, by: str) -> None:
    directory = vault_dir(slug)
    try:
        (directory / vault_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("kb_delete_failed path=%s err=%s", vault_path, type(exc).__name__)
        return
    try:
        from src.retrieval.tier1_vault import VaultRetriever

        VaultRetriever(workspace_id=workspace_id).delete_by_note_path(vault_path, repo=slug)
    except Exception as exc:  # noqa: BLE001 — the file is gone; a vector left behind is re-checked later
        logger.warning("kb_vector_delete_failed path=%s err=%s", vault_path, type(exc).__name__)
    changes.record(directory, vault_path, "deleted", by=by)
    if report is not None:
        report.deleted.append(vault_path)


def import_cards(
    slug: str,
    files: list[tuple[str, str]],
    *,
    workspace_id: str | None,
    code_repo: str = "",
    replace: bool = True,
    binding: dict | None = None,
    by: str = "import",
    report: ImportReport | None = None,
) -> ImportReport:
    """Write ``files`` (knowledge-base path, text) as cards of ``slug``.

    ``code_repo`` is the 1C configuration the cards' objects are checked
    against ("" = none). With ``replace`` the import is a snapshot: imported
    cards it does not carry are removed.
    """
    from src.vault.writer import NoteMetadata, VaultWriter

    report = report or ImportReport(repo=slug)
    report.code_repo = code_repo
    directory = vault_dir(slug)
    existing = _existing(directory)

    cards: list[tuple[str, Card]] = []
    ids: dict[str, str] = {}
    for rel, text in files:
        vault_path = f"{KB_DIR}/{rel}"
        try:
            card = parse_card(rel, text)
        except CardProblem as exc:
            report.skipped.append({"path": vault_path, "reason": str(exc)})
            continue
        if card.id.lower() in ids:
            report.skipped.append({"path": vault_path,
                                   "reason": f"id {card.id!r} is already used by {ids[card.id.lower()]}"})
            continue
        ids[card.id.lower()] = vault_path
        cards.append((vault_path, card))

    lookup, close = graph_lookup(code_repo) if code_repo else (None, lambda: None)
    index = None
    if code_repo:
        from src.config import get_settings

        index = ChangeIndex(get_settings().repo_path(code_repo))
    writer = VaultWriter(workspace_id=workspace_id)
    try:
        with writer.batched_qdrant():
            for vault_path, card in cards:
                kb = _kb_block(card, code_repo=code_repo, lookup=lookup, index=index,
                               report=report, vault_path=vault_path)
                old = existing.get(vault_path)
                if old is not None and isinstance(old[0].get("kb"), dict) \
                        and _same(old[0]["kb"], kb) and old[0].get("type") == card.type:
                    report.unchanged += 1
                    continue
                kb["checked_at"] = _now()
                meta = NoteMetadata(type=card.type, repo=slug,
                                    extra={"title": card.title, "fm": card.fm, "kb": kb})
                writer.write(repo=slug, relative_path=vault_path, content=card.body, metadata=meta)
                (report.updated if old is not None else report.created).append(vault_path)
    finally:
        close()
    report.embedded = writer.qdrant_upserted
    report.embed_failures = list(writer.qdrant_failures)

    if replace:
        keep = {p for p, _ in cards} | {s["path"] for s in report.skipped}
        for vault_path in sorted(set(existing) - keep):
            _delete_note(slug, workspace_id, vault_path, report, by=by)

    b = dict(binding or load_binding(slug) or {})
    b.update({"repo": slug, "code_repo": code_repo, "imported_at": _now(),
              "cards": len(cards), "by": by})
    b.setdefault("source", "upload")
    save_binding(slug, b)
    logger.info("kb_import repo=%s code=%s created=%d updated=%d unchanged=%d deleted=%d skipped=%d",
                slug, code_repo or "-", len(report.created), len(report.updated),
                report.unchanged, len(report.deleted), len(report.skipped))
    return report


def import_from_checkout(slug: str, *, workspace_id: str | None, prefix: str = "",
                         code_repo: str = "", follow: bool = True, by: str = "import") -> ImportReport:
    """Import the markdown of ``slug``'s own checkout (a connected repository)."""
    from src.config import get_settings

    report = ImportReport(repo=slug)
    files = read_checkout(get_settings().repo_path(slug), prefix, report)
    binding = {"source": "repo", "prefix": (prefix or "").strip().strip("/"), "follow": bool(follow)}
    return import_cards(slug, files, workspace_id=workspace_id, code_repo=code_repo,
                        replace=True, binding=binding, by=by, report=report)


def recheck(slug: str, *, workspace_id: str | None, by: str = "check") -> ImportReport:
    """Check the imported cards of ``slug`` again (objects, staleness) and
    rewrite the ones whose checks changed."""
    from src.vault.writer import NoteMetadata, VaultWriter

    binding = load_binding(slug) or {}
    code_repo = str(binding.get("code_repo") or "")
    report = ImportReport(repo=slug, code_repo=code_repo)
    directory = vault_dir(slug)
    existing = _existing(directory)
    if not existing:
        return report
    lookup, close = graph_lookup(code_repo) if code_repo else (None, lambda: None)
    from src.config import get_settings

    index = ChangeIndex(get_settings().repo_path(code_repo)) if code_repo else None
    writer = VaultWriter(workspace_id=workspace_id)
    try:
        with writer.batched_qdrant():
            for vault_path, (meta, body) in sorted(existing.items()):
                old_kb = meta["kb"]
                src = str(old_kb.get("src_path") or vault_path[len(KB_DIR) + 1:])
                fm = meta.get("fm") if isinstance(meta.get("fm"), dict) else {}
                try:
                    card = parse_card(src, _rebuild(fm, body))
                except CardProblem as exc:
                    report.skipped.append({"path": vault_path, "reason": str(exc)})
                    continue
                kb = _kb_block(card, code_repo=code_repo, lookup=lookup, index=index,
                               report=report, vault_path=vault_path)
                kb["sha"] = old_kb.get("sha", kb["sha"])
                if _same(old_kb, kb):
                    report.unchanged += 1
                    continue
                kb["checked_at"] = _now()
                was = (old_kb.get("staleness") or {}).get("state")
                now = kb["staleness"]["state"]
                writer.write(repo=slug, relative_path=vault_path, content=body,
                             metadata=NoteMetadata(type=str(meta.get("type") or card.type), repo=slug,
                                                   extra={"title": meta.get("title") or card.title,
                                                          "fm": fm, "kb": kb}),
                             redact_content=False)
                if was != now and now in ("stale", "fresh"):
                    changes.record(directory, vault_path, now, by=by,
                                   commit=kb["staleness"].get("head", ""))
                report.updated.append(vault_path)
    finally:
        close()
    report.embedded = writer.qdrant_upserted
    report.embed_failures = list(writer.qdrant_failures)
    binding["checked_at"] = _now()
    if binding.get("repo"):
        save_binding(slug, binding)
    return report


def _rebuild(fm: dict, body: str) -> str:
    """The card's text as it was imported (front matter + body), for a re-check."""
    if not fm:
        return body
    return frontmatter.dumps(frontmatter.Post(body, **fm))


def remove_all(slug: str, *, workspace_id: str | None, by: str = "import") -> int:
    """Remove every imported card of ``slug`` and forget where they came from."""
    existing = _existing(vault_dir(slug))
    for vault_path in sorted(existing):
        _delete_note(slug, workspace_id, vault_path, None, by=by)
    with contextlib.suppress(OSError):
        (vault_dir(slug) / BINDING).unlink(missing_ok=True)
    return len(existing)


# ─── after an index ───────────────────────────────────────────────────


def after_index(slug: str) -> None:
    """Called once a repository was indexed. Never raises.

    A knowledge base that follows its own repository is imported again; the
    cards bound to this repository as their configuration are checked again.
    """
    try:
        from src.api.auto_review import workspace_for_repo_slug

        ws = workspace_for_repo_slug(slug)
        binding = load_binding(slug)
        reimported = False
        if binding and binding.get("source") == "repo" and binding.get("follow"):
            code_repo = str(binding.get("code_repo") or "")
            if code_repo and workspace_for_repo_slug(code_repo) != ws:
                code_repo = ""
            import_from_checkout(slug, workspace_id=ws, prefix=str(binding.get("prefix") or ""),
                                 code_repo=code_repo, follow=True, by="sync")
            reimported = True
        for target in bindings_to(slug):
            if target == slug and reimported:
                continue
            target_ws = workspace_for_repo_slug(target)
            if target_ws != ws:
                continue  # a binding never reaches into another workspace's configuration
            recheck(target, workspace_id=target_ws, by="sync")
        # The process notes Celmis wrote from a team's list live in this
        # repository's own vault and are checked against its own clone.
        from src.kb import processes

        processes.after_index(slug)
    except Exception as exc:  # noqa: BLE001 — an index must not fail on its knowledge base
        logger.warning("kb_after_index_failed repo=%s err=%s", slug, type(exc).__name__)
