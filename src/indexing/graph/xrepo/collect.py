"""Run the right fact extractor for a parsed file, and assemble a repository's
whole fact set after a full or incremental index."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from src.indexing.graph.xrepo import go_facts, java_facts, js_facts, python_facts
from src.indexing.graph.xrepo.manifests import own_go_modules, published_packages
from src.indexing.graph.xrepo.model import FactSet, ServiceFact

logger = logging.getLogger(__name__)

_BY_LANGUAGE = {
    "python": python_facts.extract,
    "typescript": js_facts.extract, "tsx": js_facts.extract, "javascript": js_facts.extract,
    "go": go_facts.extract,
    "java": java_facts.extract,
}


def file_facts(language: str, root, source: bytes, rel: str) -> FactSet | None:  # noqa: ANN001
    """Facts of one parsed file; None for a language with no extractor. A bug
    in an extractor must not fail the index, so it answers empty instead."""
    fn = _BY_LANGUAGE.get(language)
    if fn is None:
        return None
    try:
        return fn(root, source, rel)
    except Exception as exc:  # noqa: BLE001
        logger.warning("xrepo_facts_failed file=%s err=%s", rel, exc)
        return FactSet()


def service_facts(symbols, edges) -> list[ServiceFact]:  # noqa: ANN001
    """Compose services with the image / build directory they run, from the
    (unresolved) edges the compose adapter emitted."""
    by_id: dict[str, ServiceFact] = {}
    for s in symbols:
        if getattr(s, "kind", "") == "service":
            by_id[s.id] = ServiceFact(name=s.name, file=s.file)
    for e in edges:
        svc = by_id.get(e.from_id)
        if svc is None or not e.raw_target:
            continue
        if e.kind == "RUNS_IMAGE":
            svc.image = e.raw_target
        elif e.kind == "BUILT_FROM":
            svc.build = e.raw_target
    return list(by_id.values())


def finish(facts: FactSet, repo_root: Path) -> FactSet:
    """Add the manifests and drop imports of the repository's own code."""
    facts.published = published_packages(repo_root)
    own_go = own_go_modules(facts.published)
    facts.imports = [i for i in facts.imports if not _is_local(i, repo_root, own_go)]
    return facts


def _is_local(imp, repo_root: Path, own_go: list[str]) -> bool:  # noqa: ANN001
    if imp.ecosystem == "go":
        return any(imp.package == m or imp.package.startswith(m + "/") for m in own_go)
    if imp.ecosystem == "pypi":
        top = imp.package.split(".")[0]
        if top in sys.stdlib_module_names:
            return True
        return any((repo_root / base / top).is_dir() or (repo_root / base / f"{top}.py").exists()
                   for base in ("", "src", "app", "lib"))
    return False
