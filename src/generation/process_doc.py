"""One description note per business process of a team's own list.

A process (src/kb/processes.py) is a name, a description and the objects it
starts from. The code behind it is not one module: an order-reservation
process starts at a document, is carried out by its posting procedure, writes a
register and reads a catalog through a common module. So the context handed to
the model is built from the graph around the starting objects, not from a
module:

  * the object's own modules (object, manager, forms) — the entry points;
  * the methods that WRITE the object, then those that READ it (data edges);
  * the callers and callees of those methods, one hop, bounded;
  * for a symbol or a path that is not a 1C object, the symbol, the file or the
    directory, and one hop around them.

The code is read within a token budget in that priority order, so a very large
module never pushes out the writers. The note lands in the code repository's
vault as ``processes/<id>.md``, shaped like a draft card (``kb.origin =
"generate"``): the catalog, the links, the MCP docs tool and the staleness
check treat it like any card, and a verified card that replaces it wins.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings
from src.indexing.graph.languages.onec import owner_object
from src.kb import processes
from src.kb.objects import ChangeIndex
from src.kb.processes import ProcessSpec
from src.llm.prompts import PROCESS_PROMPT, PROCESS_SYSTEM
from src.llm.prompts.language import with_language
from src.retrieval.tier3_code import CodeBundle, CodeReader
from src.vault.provenance import build as _provenance
from src.vault.writer import NoteMetadata, VaultWriter

logger = logging.getLogger(__name__)

#: Methods listed per role in the metadata the model reads (the code is cut by the budget).
MAX_WRITERS = 25
MAX_READERS = 25
MAX_FILES = 60
MAX_SYMBOLS_IN_NOTE = 40
#: Seeds the one-hop call expansion starts from, and what it may bring back.
MAX_EXPAND_SEEDS = 12
MAX_EXPANDED = 40
#: Tokens below which reading another snippet is not worth a call.
MIN_BUDGET = 200

_OBJECT_MODULE = re.compile(r"\.(ObjectModule|ManagerModule|RecordSetModule|ValueManagerModule)\.")
_WIKI = re.compile(r"\[\[([^\]|#\n]{1,200})")

Location = tuple[str, int, int | None]


def _get(x: Any, key: str, default: Any = None) -> Any:  # noqa: ANN401
    return x.get(key, default) if isinstance(x, dict) else getattr(x, key, default)


@dataclass
class ProcessContext:
    """What the graph says about one process, before any code is read."""

    found: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    graph_available: bool = True
    #: Locations in reading order (highest priority first).
    locations: list[Location] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    writers: list[dict] = field(default_factory=list)
    readers: list[dict] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # "file:name", for the reverse index

    def add(self, file: str, start: int, end: int | None = None) -> None:
        if not file:
            return
        self.locations.append((file, int(start or 1), end))
        if file not in self.files:
            self.files.append(file)


def _plain_relative(token: str) -> bool:
    t = token.replace("\\", "/")
    return bool(t) and not t.startswith("/") and ".." not in t.split("/") and "\x00" not in t \
        and ":" not in t.split("/", 1)[0]


def gather(store: Any, repo_path: Path, spec: ProcessSpec) -> ProcessContext:  # noqa: ANN401
    """The graph context of ``spec``. ``store`` is a graph store, or None when the
    repository has no graph yet (the objects are then neither found nor missing:
    nothing could be checked)."""
    ctx = ProcessContext()
    canon, others = processes.split_objects(spec)
    if store is None:
        ctx.graph_available = False
        for obj in canon:
            _dir_fallback(repo_path, obj.replace(".", "/", 1), ctx)
        for tok in others:
            if _plain_relative(tok) and (repo_path / tok).exists():
                ctx.add(tok.replace("\\", "/"), 1)
        return ctx

    object_ids: list[str] = []
    entry_modules: list[Any] = []
    other_symbols: list[Any] = []
    seeds: list[str] = []

    for obj in canon:
        try:
            rows = [r for r in store.find_named(obj, limit=20) if r.get("name") == obj]
        except Exception as exc:  # noqa: BLE001
            logger.warning("process_find_failed obj=%s err=%s", obj, type(exc).__name__)
            rows = []
        if not rows:
            ctx.missing.append(obj)
            continue
        ctx.found.append(obj)
        node = rows[0]
        object_ids.append(node["id"])
        owner_file = str(node.get("file") or "")
        directory = owner_file[:-4] if owner_file.endswith(".xml") else obj.replace(".", "/", 1)
        try:
            syms = list(store.symbols_in_path(directory, limit=80))
        except Exception:  # noqa: BLE001
            syms = []
        for s in syms:
            (entry_modules if _OBJECT_MODULE.search(str(_get(s, "name", ""))) else other_symbols).append(s)

    for tok in others:
        found_any = False
        if _plain_relative(tok) and ("/" in tok or (repo_path / tok).exists()):
            rel = tok.replace("\\", "/").strip("/")
            try:
                syms = list(store.symbols_in_file(rel)) or list(store.symbols_in_path(rel))
            except Exception:  # noqa: BLE001
                syms = []
            if syms:
                entry_modules.extend(syms)
                found_any = True
            elif (repo_path / rel).is_file():
                ctx.add(rel, 1)
                found_any = True
        else:
            try:
                syms = list(store.find_by_name(tok, limit=5)) or list(store.find_named(tok, limit=5))
            except Exception:  # noqa: BLE001
                syms = []
            if syms:
                entry_modules.extend(syms)
                found_any = True
        (ctx.found if found_any else ctx.missing).append(tok)

    # 1. the entry points: object modules and the named symbols.
    for s in entry_modules:
        _add_symbol(ctx, s)
        seeds.append(str(_get(s, "id", "")))

    # 2. who writes and who reads the objects.
    users: list[dict] = []
    if object_ids:
        try:
            users = list(store.data_users(object_ids, limit=300))
        except Exception as exc:  # noqa: BLE001
            logger.warning("process_data_users_failed err=%s", type(exc).__name__)
    ranges: dict[str, dict[str, tuple[int, int | None]]] = {}

    def method_range(row: dict) -> tuple[int, int | None]:
        f = str(row.get("from_file") or "")
        if f not in ranges:
            try:
                ranges[f] = {str(_get(s, "id")): (int(_get(s, "start_line") or 1), _get(s, "end_line"))
                             for s in store.symbols_in_file(f)}
            except Exception:  # noqa: BLE001
                ranges[f] = {}
        got = ranges[f].get(str(row.get("from_id")))
        if got:
            return got
        line = int(row.get("line") or 1)
        return max(1, line - 10), line + 25

    for role, bucket, cap in (("WRITES", ctx.writers, MAX_WRITERS), ("READS", ctx.readers, MAX_READERS)):
        by_method: dict[str, dict] = {}
        for row in users:
            if row.get("kind") != role:
                continue
            m = by_method.setdefault(str(row.get("from_id")), {
                "method": row.get("from_name"), "file": row.get("from_file"), "id": row.get("from_id"),
                "objects": [], "lines": [], "row": row})
            if row.get("to_name") and row["to_name"] not in m["objects"]:
                m["objects"].append(row["to_name"])
            if row.get("line") and row["line"] not in m["lines"]:
                m["lines"].append(row["line"])
        for m in list(by_method.values())[:cap]:
            start, end = method_range(m.pop("row"))
            m["lines"] = sorted(m["lines"])[:8]
            bucket.append(m)
            ctx.add(str(m["file"]), start, end)
            if role == "WRITES":
                seeds.append(str(m["id"]))
            sym = f"{m['file']}:{str(m['method']).rsplit('.', 1)[-1]}"
            if "/" in str(m["file"]) and sym not in ctx.symbols:
                ctx.symbols.append(sym)

    # 3. the rest of the object's own modules (forms, commands).
    for s in other_symbols[:40]:
        _add_symbol(ctx, s)

    # 4. one hop of calls around the entry points and the writers.
    seeds = [s for s in dict.fromkeys(seeds) if s][:MAX_EXPAND_SEEDS]
    if seeds:
        try:
            exp = store.bfs_expand(seed_ids=seeds, depth=1, max_nodes=MAX_EXPANDED, edge_kinds=["CALLS"])
            for s in list(exp.reached)[:MAX_EXPANDED]:
                _add_symbol(ctx, s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("process_expand_failed err=%s", type(exc).__name__)
    return ctx


def _add_symbol(ctx: ProcessContext, s: Any) -> None:  # noqa: ANN401
    f = str(_get(s, "file", "") or "")
    start = int(_get(s, "start_line", 1) or 1)
    ctx.add(f, start, _get(s, "end_line"))
    sym = f"{f}:{str(_get(s, 'name', '')).rsplit('.', 1)[-1]}"
    if "/" in f and sym not in ctx.symbols and len(ctx.symbols) < 200:
        ctx.symbols.append(sym)


def _dir_fallback(repo_path: Path, rel_dir: str, ctx: ProcessContext) -> None:
    """With no graph: the BSL/source files under an object's folder, whole."""
    from src.security.secret_files import plain_file

    base = repo_path / rel_dir
    try:
        if base.is_symlink() or not base.is_dir():
            return
        paths = sorted(base.rglob("*"))
    except OSError:
        return
    for p in paths:
        if len(ctx.files) >= 12:
            return
        try:
            if not p.is_file() or p.suffix not in (".bsl", ".os"):
                continue
        except OSError:
            continue
        rel = str(p.relative_to(repo_path))
        if plain_file(repo_path, rel) is not None:
            ctx.add(rel, 1)


def read_within_budget(reader: CodeReader, repo_path: Path, locations: list[Location],
                       budget: int) -> CodeBundle:
    """Read ``locations`` in order. One snippet too big for what is left is
    skipped (the reader itself would stop there); the rest are still tried."""
    bundle = CodeBundle(budget=budget)
    seen: set[tuple[str, int]] = set()
    remaining = budget
    for loc in locations:
        if remaining < MIN_BUDGET:
            bundle.truncated = True
            break
        if (loc[0], loc[1]) in seen:
            continue
        seen.add((loc[0], loc[1]))
        got = reader.read_locations(repo_path, [loc], budget_tokens=remaining, context_lines=2)
        if got.snippets:
            bundle.snippets.extend(got.snippets)
            bundle.total_tokens += got.total_tokens
            remaining -= got.total_tokens
        elif got.truncated:
            bundle.truncated = True
    return bundle


def _module_refs(vault_dir: Path, files: list[str]) -> list[str]:
    """``modules/<name>`` of the module notes whose folder holds one of ``files``."""
    import frontmatter

    from src.vault.reader import vault_note_files

    root = vault_dir / "modules"
    if not root.is_dir():
        return []
    out: list[str] = []
    for full in vault_note_files(root):
        try:
            meta = frontmatter.load(full).metadata
        except Exception:  # noqa: BLE001
            continue
        mp = str((meta or {}).get("path") or "").strip("/")
        if mp and any(f == mp or f.startswith(mp + "/") for f in files):
            out.append(f"modules/{full.relative_to(root).with_suffix('').as_posix()}")
    return sorted(out)[:30]


def _norm(token: str) -> str:
    return token.strip().lower()


def _canon_set(spec: ProcessSpec) -> set[str]:
    return {_norm(o) for o in processes.split_objects(spec)[0]}


def related_ids(spec: ProcessSpec, others: list[ProcessSpec], text: str = "") -> list[str]:
    """Other processes that share an object with ``spec`` or that the text links."""
    mine = {_norm(o) for o in spec.objects} | _canon_set(spec)
    ids: list[str] = []
    by_id = {o.id.lower(): o.id for o in others if o.id != spec.id}
    for m in _WIKI.finditer(text or ""):
        got = by_id.get(m.group(1).strip().lower())
        if got and got not in ids:
            ids.append(got)
    for o in others:
        if o.id == spec.id or o.id in ids:
            continue
        theirs = {_norm(x) for x in o.objects} | _canon_set(o)
        if mine & theirs:
            ids.append(o.id)
    return ids


class ProcessDocGenerator:
    """Collects the context of one process and writes its note."""

    def __init__(
        self, settings: Settings | None = None, *, code_reader: CodeReader | None = None,
        vault_writer: VaultWriter | None = None, graph_store: Any = None,  # noqa: ANN401
        language: str | None = None, workspace_id: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.language = language
        self.engine = None
        self.graph_store = graph_store
        self.code_reader = code_reader or CodeReader(self.settings)
        self.vault_writer = vault_writer or VaultWriter(self.settings, workspace_id=workspace_id)

    def generate(self, *, repo: str, repo_path: Path, commit_sha: str, spec: ProcessSpec,
                 others: list[ProcessSpec]) -> Path:
        if self.engine is None:
            raise RuntimeError(f"{type(self).__name__} has no engine — build one with "
                               "src.generation.engines.build_engine()")
        ctx = gather(self.graph_store, repo_path, spec)
        bundle = read_within_budget(self.code_reader, repo_path, ctx.locations,
                                    self.settings.max_code_tokens_per_module)
        if not bundle.snippets:
            logger.warning("process_no_code process=%s — the note will describe what is known "
                           "from the list only", spec.id)

        canon, _plain = processes.split_objects(spec)
        metadata = {
            "process": spec.as_dict(),
            "objects_found": ctx.found,
            "objects_missing": ctx.missing,
            "graph_available": ctx.graph_available,
            "modules_involved": ctx.files[:MAX_FILES],
            "writers": [{k: v for k, v in w.items() if k != "id"} for w in ctx.writers],
            "readers": [{k: v for k, v in r.items() if k != "id"} for r in ctx.readers],
            "other_processes": [{"id": o.id, "name": o.name, "objects": o.objects}
                                for o in others if o.id != spec.id][:200],
        }
        result = self.engine.generate(
            prompt=PROCESS_PROMPT,
            system_instruction=with_language(PROCESS_SYSTEM, self.language),
            code_context=bundle.as_markdown(),
            metadata_context=metadata,
            operation="generate_process_doc",
            repo=repo,
            module=None,
        )
        text = str(getattr(result, "text", "") or "")
        if not text.strip():
            raise RuntimeError("the engine returned an empty document")

        files = ctx.files[:MAX_FILES]
        involved = sorted({o for f in files if (o := owner_object(f)) and o not in canon})[:60]
        source_sha = commit_sha or ""
        st = processes.staleness_of(
            ChangeIndex(repo_path) if (repo_path / ".git").exists() else None,
            canon + involved, files, source_sha)
        kb: dict[str, Any] = {
            "origin": "generate", "id": spec.id, "status": "draft",
            "objects": canon, "objects_missing": [o for o in ctx.missing if o in canon],
            "code_repo": repo,
            "process": {"name": spec.name, "description": spec.description, "objects": spec.objects},
            "files": files, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if ctx.missing:
            kb["targets_missing"] = ctx.missing
        if involved:
            kb["involved_objects"] = involved
        if spec.domain:
            kb["domain"] = spec.domain
        if spec.tags:
            kb["tags"] = list(spec.tags)
        if source_sha:
            kb["source"] = f"{repo}@{source_sha}"
            kb["source_sha"] = source_sha
        related = related_ids(spec, others, text)
        if related:
            kb["related"] = related
        kb = processes.refresh_kb(kb, st)

        main = next((f for f in files if _OBJECT_MODULE.search(f.replace("/", ".")) is not None), None) \
            or (files[0] if files else None)
        meta = NoteMetadata(
            provenance=_provenance(
                engine=getattr(result, "engine", None), model=getattr(result, "model", None),
                language=self.language, tools_used=getattr(result, "tools_used", None),
                commit=commit_sha),
            type="process", repo=repo, commit=commit_sha or None, path=main,
            symbols=ctx.symbols[:MAX_SYMBOLS_IN_NOTE],
            cross_refs=_module_refs(self.settings.repo_vault_path(repo), files),
            keywords=[spec.name.lower(), *canon][:20],
            extra={"title": spec.name, "kb": kb},
        )
        return self.vault_writer.write(repo=repo, relative_path=processes.note_path(spec.id),
                                       content=text, metadata=meta)


# ─── a run: one note per process, failures reported not fatal ─────────


@dataclass
class RunResult:
    results: dict[str, dict] = field(default_factory=dict)

    @property
    def ok(self) -> list[str]:
        return [k for k, v in self.results.items() if v.get("status") == "ok"]

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.results.items() if v.get("status") == "failed"]

    def summary(self) -> str:
        return f"processes ok={len(self.ok)} failed={len(self.failed)}"


def generate_processes(
    slug: str, *, workspace_id: str = "default", user_id: str | None = None,
    ids: list[str] | None = None, engine: str | None = None, language: str | None = None,
    cancel_check: Callable[[], bool] | None = None, job_id: str | None = None,
    engine_obj: Any = None,  # noqa: ANN401
    spec_overrides: dict[str, ProcessSpec] | None = None,
) -> RunResult:
    """Write the note of every process of ``slug``'s list (or of ``ids``).

    A process that fails is reported in the result and in the repository's
    ``last_run``; the others still run. ``spec_overrides`` supplies specs that are
    no longer in the list (a note being regenerated from the spec it carries).
    """
    from src.generation.doc_language import resolve_doc_engine, resolve_doc_language
    from src.sync.incremental import _git_head
    from src.sync.queue import JobCancelled

    settings = get_settings()
    repo_path = settings.repo_path(slug)
    if not repo_path.exists():
        raise RuntimeError("the repository has no clone yet — index it first")
    listed = processes.load(slug)
    by_id = {s.id: s for s in listed}
    for k, v in (spec_overrides or {}).items():
        by_id.setdefault(k, v)
    everyone = list(by_id.values())
    wanted = [by_id[i] for i in ids if i in by_id] if ids is not None else listed
    result = RunResult()
    for i in ids or []:
        if i not in by_id:
            result.results[i] = {"status": "failed", "error": "no process with this id in the list"}

    language_code = resolve_doc_language(language, workspace_id)
    selected = engine_obj
    if selected is None:
        from src.generation.engines import build_engine

        selected = build_engine(resolve_doc_engine(engine, workspace_id), workspace_id, user_id)

    store = None
    graph_path = settings.repo_graph_path(slug)
    if graph_path.exists():
        from src.indexing.graph.graph_store import make_graph_store

        store = make_graph_store(graph_path)
    gen = ProcessDocGenerator(settings, graph_store=store, language=language_code,
                              workspace_id=workspace_id)
    gen.engine = selected
    head = _git_head(repo_path) or ""
    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        with gen.vault_writer.batched_qdrant():
            for spec in wanted:
                if cancel_check is not None and cancel_check():
                    raise JobCancelled()
                try:
                    path = gen.generate(repo=slug, repo_path=repo_path, commit_sha=head, spec=spec,
                                        others=everyone)
                    note = path.relative_to(settings.repo_vault_path(slug)).as_posix()
                    result.results[spec.id] = {"status": "ok", "path": note}
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 — one process must not stop the others
                    logger.warning("process_failed repo=%s process=%s err=%s", slug, spec.id,
                                   type(exc).__name__)
                    result.results[spec.id] = {"status": "failed",
                                               "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                logger.debug("process_graph_close_failed repo=%s", slug)
        processes.save_run(slug, {
            "job_id": job_id, "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "engine": getattr(selected, "name", ""), "language": language_code,
            "results": result.results,
        })
    return result
