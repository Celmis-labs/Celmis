"""A team's own list of business processes, kept next to a repository's documentation.

The generator finds features by itself (src/generation/auto_detect.py), but a
team documenting a legacy configuration already knows which processes it has:
"order reservation", "month-end close". It gives Celmis that list — a name, a
description and the objects a process starts from (``Документ.ЗаказКлиента``,
or, for code that is not 1C, a symbol or a path) — and Celmis writes one
description note per process, ``processes/<id>.md``, from the code those
objects reach.

The list is one small JSON file in the repository's vault
(``.celmis_processes.json``), replaced as a whole by a save; the notes are
ordinary vault notes shaped like a draft card (``kb.origin = "generate"``), so
the catalog, the links, the MCP ``docs`` tool and the staleness check treat
them like any card — and never as a human-verified one.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter

from src.kb import changes
from src.kb.objects import ChangeIndex, Staleness, canonical_all

logger = logging.getLogger(__name__)

LIST_FILE = ".celmis_processes.json"
NOTE_DIR = "processes"
MAX_PROCESSES = 200
MAX_NAME = 200
MAX_DESCRIPTION = 4000
MAX_OBJECTS = 50
MAX_OBJECT_CHARS = 300
MAX_TAGS = 20
MAX_UPLOAD_BYTES = 1_000_000

_ID_RE = re.compile(r"^[\w][\w.-]{0,119}$", re.UNICODE)

#: Front matter keys NoteMetadata models itself; everything else rides in ``extra``.
_STRUCT_KEYS = ("type", "repo", "analyzed_at", "commit", "module", "feature", "path", "symbols",
                "entry_points", "keywords", "external_deps", "used_by", "cross_refs",
                "security_findings", "provenance")


class ProcessSpecError(ValueError):
    """The list is refused as a whole; ``problems`` says what is wrong, per process."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:5]) + (" …" if len(problems) > 5 else ""))
        self.problems = problems


@dataclass
class ProcessSpec:
    id: str
    name: str
    description: str = ""
    #: As the team wrote them: 1C objects in any spelling, code symbols, paths.
    objects: list[str] = field(default_factory=list)
    domain: str = ""
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name, "description": self.description,
                             "objects": list(self.objects)}
        if self.domain:
            d["domain"] = self.domain
        if self.tags:
            d["tags"] = list(self.tags)
        return d


def slug_of(name: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", (name or "").strip().lower(), flags=re.UNICODE).strip("-.")
    return s[:120]


def note_path(process_id: str) -> str:
    return f"{NOTE_DIR}/{process_id}.md"


def split_objects(spec: ProcessSpec) -> tuple[list[str], list[str]]:
    """(1C objects in canonical form, everything else: symbols and paths)."""
    return canonical_all(spec.objects)


def _as_list(value: Any) -> list[str]:  # noqa: ANN401
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[;\n|,]", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def normalise(raw: Any) -> list[ProcessSpec]:  # noqa: ANN401
    """Specs from decoded JSON/YAML/CSV rows; :class:`ProcessSpecError` when any is wrong."""
    if isinstance(raw, dict):
        raw = raw.get("processes")
    if not isinstance(raw, list):
        raise ProcessSpecError(["expected a list of processes"])
    if len(raw) > MAX_PROCESSES:
        raise ProcessSpecError([f"at most {MAX_PROCESSES} processes; got {len(raw)}"])
    from src.security.secret_files import is_secret_path

    problems: list[str] = []
    out: list[ProcessSpec] = []
    seen: dict[str, int] = {}
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            problems.append(f"#{i}: not an object")
            continue
        name = str(row.get("name") or "").strip()
        label = f"#{i} {name[:40]!r}" if name else f"#{i}"
        if not name:
            problems.append(f"{label}: name is required")
            continue
        if len(name) > MAX_NAME:
            problems.append(f"{label}: name is longer than {MAX_NAME} characters")
            continue
        desc = str(row.get("description") or "").strip()
        if len(desc) > MAX_DESCRIPTION:
            problems.append(f"{label}: description is longer than {MAX_DESCRIPTION} characters")
            continue
        objects: list[str] = []
        for o in _as_list(row.get("objects")):
            if o not in objects:
                objects.append(o)
        if len(objects) > MAX_OBJECTS:
            problems.append(f"{label}: more than {MAX_OBJECTS} objects")
            continue
        if any(len(o) > MAX_OBJECT_CHARS or "\x00" in o for o in objects):
            problems.append(f"{label}: an object is longer than {MAX_OBJECT_CHARS} characters")
            continue
        pid = str(row.get("id") or "").strip() or slug_of(name)
        if not _ID_RE.match(pid) or ".." in pid or pid.endswith(".") or is_secret_path(note_path(pid)):
            problems.append(f"{label}: id {pid!r} is not usable (letters, digits, '-', '_', '.'; at most 120)")
            continue
        if pid.lower() in seen:
            problems.append(f"{label}: id {pid!r} is already used by #{seen[pid.lower()]}")
            continue
        seen[pid.lower()] = i
        out.append(ProcessSpec(id=pid, name=name, description=desc, objects=objects,
                               domain=str(row.get("domain") or "").strip()[:200],
                               tags=_as_list(row.get("tags"))[:MAX_TAGS]))
    if problems:
        raise ProcessSpecError(problems)
    return out


def parse_upload(filename: str, data: bytes) -> list[dict]:
    """Rows of an uploaded ``.json``, ``.yaml``/``.yml`` or ``.csv`` list."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ProcessSpecError([f"the file is larger than {MAX_UPLOAD_BYTES // 1000} KB"])
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProcessSpecError(["the file is not UTF-8 text"]) from None
    low = (filename or "").lower()
    try:
        if low.endswith(".csv"):
            sample = text[:2000]
            delim = ";" if sample.count(";") > sample.count(",") else ","
            rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
            return [{(k or "").strip().lower(): (v or "") for k, v in r.items()} for r in rows]
        if low.endswith((".yaml", ".yml")):
            import yaml

            loaded = yaml.safe_load(text)
        elif low.endswith(".json"):
            loaded = json.loads(text)
        else:
            raise ProcessSpecError(["use a .json, .yaml, .yml or .csv file"])
    except ProcessSpecError:
        raise
    except Exception as exc:  # noqa: BLE001 — a malformed file is the caller's to fix
        raise ProcessSpecError([f"cannot read the file: {type(exc).__name__}"]) from None
    if isinstance(loaded, dict):
        loaded = loaded.get("processes")
    if not isinstance(loaded, list):
        raise ProcessSpecError(["expected a list of processes (or {processes: [...]})"])
    return loaded


# ─── the stored list ──────────────────────────────────────────────────


def _vault(slug: str) -> Path:
    from src.config import get_settings

    return get_settings().repo_vault_path(slug)


def _read_file(slug: str) -> dict:
    try:
        data = json.loads((_vault(slug) / LIST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(slug: str, data: dict) -> None:
    d = _vault(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (LIST_FILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / LIST_FILE)


def load(slug: str) -> list[ProcessSpec]:
    out: list[ProcessSpec] = []
    for row in _read_file(slug).get("processes") or []:
        try:
            out.extend(normalise([row]))
        except ProcessSpecError:
            logger.warning("process_spec_unreadable repo=%s", slug)
    return out


def save(slug: str, specs: list[ProcessSpec], *, by: str = "") -> None:
    data = _read_file(slug)
    data["processes"] = [s.as_dict() for s in specs]
    data["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if by:
        data["updated_by"] = by
    _write_file(slug, data)


def last_run(slug: str) -> dict:
    run = _read_file(slug).get("last_run")
    return run if isinstance(run, dict) else {}


def save_run(slug: str, run: dict) -> None:
    data = _read_file(slug)
    data["last_run"] = run
    _write_file(slug, data)


# ─── the notes ────────────────────────────────────────────────────────


def is_generated(meta: dict) -> bool:
    kb = meta.get("kb")
    return isinstance(kb, dict) and kb.get("origin") == "generate" and meta.get("type") == "process"


def generated_notes(slug: str) -> dict[str, tuple[dict, str]]:
    """Vault path → (front matter, body) of the process notes Celmis wrote."""
    out: dict[str, tuple[dict, str]] = {}
    root = _vault(slug) / NOTE_DIR
    if not root.is_dir():
        return out
    from src.vault.reader import vault_note_files

    for full in vault_note_files(root):
        try:
            post = frontmatter.load(full)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(post.metadata, dict) and is_generated(post.metadata):
            out[f"{NOTE_DIR}/{full.relative_to(root).as_posix()}"] = (post.metadata, post.content)
    return out


def remove_note(slug: str, process_id: str, *, workspace_id: str | None, by: str) -> bool:
    """Remove a process's generated note (never a card that happens to share the path)."""
    path = note_path(process_id)
    if path not in generated_notes(slug):
        return False
    from src.kb import store

    store._delete_note(slug, workspace_id, path, None, by=by)
    return True


# ─── staleness ────────────────────────────────────────────────────────


def staleness_of(index: ChangeIndex | None, objects: list[str], files: list[str], sha: str) -> Staleness:
    """Is a process note written at ``sha`` out of date: did one of the objects
    it starts from or involves, or one of the files it read, change since?"""
    if not objects and not files:
        return Staleness("unknown", "the note names no objects or files")
    if not sha:
        return Staleness("unknown", "the note has no source commit")
    if index is None:
        return Staleness("unknown", "the repository has no clone to compare with")
    changed, why = index.changed_since(sha)
    if changed is None:
        return Staleness("unknown", why, head=index.head())
    paths, why2 = index.changed_paths_since(sha)
    hit = sorted(o for o in objects if o in changed)
    if paths:
        hit += sorted(f for f in files if f in paths and f not in hit)
    if hit:
        return Staleness("stale", "code the process uses changed after the source commit", hit,
                         index.head())
    if paths is None:
        return Staleness("unknown", why2, head=index.head())
    return Staleness("fresh", head=index.head())


def refresh_kb(kb: dict, st: Staleness) -> dict:
    out = dict(kb)
    out["staleness"] = st.as_dict()
    out["effective_status"] = "needs-review" if st.state == "stale" else str(kb.get("status") or "draft")
    return out


def recheck(slug: str, *, workspace_id: str | None, by: str = "sync") -> list[str]:
    """Check the generated process notes of ``slug`` against its own clone again;
    the paths of the notes whose answer changed."""
    notes = generated_notes(slug)
    if not notes:
        return []
    from src.config import get_settings
    from src.vault.writer import NoteMetadata, VaultWriter

    index = ChangeIndex(get_settings().repo_path(slug))
    directory = _vault(slug)
    writer = VaultWriter(workspace_id=workspace_id)
    moved: list[str] = []
    with writer.batched_qdrant():
        for path, (meta, body) in sorted(notes.items()):
            kb = meta["kb"]
            objects = list(kb.get("objects") or []) + list(kb.get("involved_objects") or [])
            st = staleness_of(index, objects, list(kb.get("files") or []), str(kb.get("source_sha") or ""))
            new_kb = refresh_kb(kb, st)
            old = dict(kb.get("staleness") or {})
            old.pop("head", None)
            now = dict(new_kb["staleness"])
            now.pop("head", None)
            if old == now:
                continue
            new_kb["checked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            extra = {k: v for k, v in meta.items() if k not in _STRUCT_KEYS}
            extra["kb"] = new_kb
            known = {k: meta[k] for k in _STRUCT_KEYS if k in meta}
            writer.write(repo=slug, relative_path=path, content=body, redact_content=False,
                         metadata=NoteMetadata(extra=extra, **known))
            if old.get("state") != now["state"] and now["state"] in ("stale", "fresh"):
                changes.record(directory, path, now["state"], by=by, commit=st.head)
            moved.append(path)
    return moved


def after_index(slug: str) -> None:
    """Called once ``slug`` was indexed. Never raises."""
    try:
        from src.api.auto_review import workspace_for_repo_slug

        recheck(slug, workspace_id=workspace_for_repo_slug(slug), by="sync")
    except Exception as exc:  # noqa: BLE001
        logger.warning("process_recheck_failed repo=%s err=%s", slug, type(exc).__name__)
