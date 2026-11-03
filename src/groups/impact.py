"""Who consumes a route or a symbol of one repository, across its workspace.

Two kinds of link, both read from the facts the indexer records per repository
(`src.indexing.graph.xrepo`), never from a name that merely looks alike:

  http    a route exposed by repository A  <->  an HTTP call in repository B
          with a compatible method and path. STRONG when the call's host maps
          to A (a configured base URL, a compose service of either repository,
          an image or build context that names A, a host named like A, or a URL
          variable named like A). WEAK on the path alone, or when the call
          carries a prefix the route does not (a mounted router).
  import  a package published by A  <->  an import of it in B, then the calls of
          the imported names in B. STRONG when the changed name is called; WEAK
          when it is only imported, or when only the class it belongs to is.

Weak links are reported and must never raise a severity: the caller decides
(`Consumer.strong`).

TENANCY. The repositories consulted are `tenancy.within_workspace(list_repos(),
workspace_id)` - the same filter the deprecation scan uses. Under multi_tenant
another workspace's repositories are neither read nor named.

A repository indexed before facts existed has no file; it is reported in
`Impact.unknown` so the answer can say "N repositories not checked", which is
not the same sentence as "no consumers".
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from types import SimpleNamespace

from src.groups.cross_repo import _extract_build_directory, _extract_repo_name, _image_base_name
from src.indexing.graph.xrepo import FactSet, load_facts, methods_compatible, paths_match
from src.indexing.graph.xrepo.manifests import normalize_py_name
from src.indexing.graph.xrepo.model import normalize_path

logger = logging.getLogger(__name__)

STRONG = "strong"
WEAK = "weak"

_URL_NOISE = {"url", "uri", "host", "base", "endpoint", "api", "addr", "address", "origin",
              "server", "svc", "root", "path"}
_ROLE_SUFFIX = ("-service", "-svc", "-api", "-srv", "-server", "-backend", "-be")


@dataclass(frozen=True)
class Consumer:
    repo_slug: str
    file: str
    line: int
    kind: str  # "http" | "import"
    confidence: str  # STRONG | WEAK
    detail: str  # `POST /invoices/{}/pay`, `acme_billing.pay_invoice`
    caller: str = ""
    why: str = ""  # how the link was decided, for the reader

    @property
    def strong(self) -> bool:
        return self.confidence == STRONG


@dataclass
class Impact:
    consumers: list[Consumer] = field(default_factory=list)
    #: Repositories of the workspace that were consulted.
    checked: list[str] = field(default_factory=list)
    #: Consulted repositories with no recorded facts (indexed before this
    #: feature, or never): their silence is not an answer.
    unknown: list[str] = field(default_factory=list)
    #: The changed repository has no recorded facts (see `WorkspaceView`).
    owner_missing: bool = False

    @property
    def strong(self) -> list[Consumer]:
        return [c for c in self.consumers if c.strong]


# ─── the workspace ───────────────────────────────────────────────


def workspace_facts(
    workspace_id: str | None, exclude_slug: str = "",
    *, load: Callable[[str], FactSet | None] | None = None,
) -> tuple[dict[str, FactSet], list[str]]:
    """(facts by slug, slugs without facts) of every OTHER repository the
    workspace may read."""
    from src.mcp_server import tools as legacy
    from src.mcp_server.tenancy import within_workspace

    try:
        # Slugs only: a summary opens the repository's graph, seconds each.
        repos = [SimpleNamespace(slug=s) for s in legacy.list_repo_slugs()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("impact_repos_unreadable err=%s", exc)
        return {}, []
    repos = within_workspace(repos, workspace_id)
    if repos and exclude_slug and all(r.slug != exclude_slug for r in repos):
        # Nothing was excluded: the caller handed an address this listing is
        # not keyed on. Better to find no consumers than to name the
        # repository's own code as somebody else's.
        logger.warning("impact_own_repo_not_excluded repo=%r", exclude_slug)
    if load is None:
        from src.config import get_settings

        settings = get_settings()

        def load(slug: str) -> FactSet | None:  # noqa: ANN202
            return load_facts(settings.repo_graph_path(slug))

    found: dict[str, FactSet] = {}
    unknown: list[str] = []
    for r in repos:
        if r.slug == exclude_slug:
            continue
        facts = load(r.slug)
        if facts is None:
            unknown.append(r.slug)
        else:
            found[r.slug] = facts
    return found, unknown


# ─── which host is which repository ──────────────────────────────


def repo_names(slug: str) -> set[str]:
    """Names a repository answers to as a hostname: its slug without the
    provider prefix and every multi-word tail of it (`acme-billing-service` ->
    `billing-service`). Single words are not tails: `service` is nobody."""
    bare = re.sub(r"^(github|gitlab|bitbucket|generic)_", "", slug.lower())
    names = {bare, _extract_repo_name(slug).lower()}
    words = bare.split("-")
    for i in range(1, len(words) - 1):
        names.add("-".join(words[i:]))
    return {n for n in names if n}


def _stems(names: Iterable[str]) -> set[str]:
    out = set()
    for n in names:
        out.add(n)
        for suf in _ROLE_SUFFIX:
            if n.endswith(suf) and len(n) > len(suf):
                out.add(n[: -len(suf)])
    return out


def _hint_stem(hint: str) -> str:
    """`BILLING_SERVICE_URL` -> `billing-service`; `baseUrl` -> `base`->''."""
    words = [w for w in re.split(r"[_\-\s]+|(?<=[a-z])(?=[A-Z])", hint) if w]
    words = [w.lower() for w in words]
    while words and words[-1] in _URL_NOISE:
        words.pop()
    return "-".join(words)


def configured_host_map() -> dict[str, str]:
    try:
        from src.config import get_settings

        raw = getattr(get_settings(), "impact_host_map", "") or ""
        data = json.loads(raw) if raw.strip() else {}
        return {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("impact_host_map_unreadable err=%s", exc)
        return {}


class _Hosts:
    """Decides whether a call made by `caller` (its facts) is addressed to
    `owner`, and whether it is addressed to some OTHER repository."""

    def __init__(self, owner_slug: str, owner: FactSet | None,
                 all_facts: dict[str, FactSet], host_map: dict[str, str]) -> None:
        self.owner_slug = owner_slug
        self.owner_names = repo_names(owner_slug)
        self.owner_stems = _stems(self.owner_names)
        self.owner_services = {s.name.lower() for s in (owner.services if owner else [])}
        self.host_map = host_map
        self.others = {s: _stems(repo_names(s)) for s in all_facts if s != owner_slug}

    def _service_target(self, caller: FactSet, host: str) -> set[str]:
        """Repo names a compose service of the caller's own stack points at."""
        out: set[str] = set()
        for svc in caller.services:
            if svc.name.lower() != host:
                continue
            if svc.image:
                out.add(_image_base_name(svc.image).lower())
            if svc.build:
                d = _extract_build_directory(svc.build)
                if d:
                    out.add(d.lower())
        return out

    def classify(self, caller: FactSet, host: str, hint: str) -> tuple[str, str]:
        """-> ("owner", why) | ("other", why) | ("unknown", "")."""
        if host:
            mapped = self.host_map.get(host)
            if mapped:
                return ("owner", f"configured base URL {host}") if mapped == self.owner_slug \
                    else ("other", "")
            if host in self.owner_services:
                return "owner", f"compose service {host} of the repository"
            targets = self._service_target(caller, host)
            if targets & self.owner_stems:
                return "owner", f"compose service {host} runs this repository's image/build"
            first = host.split(".")[0]
            if first in self.owner_stems or host in self.owner_stems:
                return "owner", f"host {host} is named like the repository"
            if targets or any(first in st or host in st for st in self.others.values()):
                return "other", ""
        elif hint:
            stem = _hint_stem(hint)
            if stem and stem in self.owner_stems:
                return "owner", f"URL variable {hint} is named like the repository"
            if stem and any(stem in st for st in self.others.values()):
                return "other", ""
        return "unknown", ""


# ─── http links ──────────────────────────────────────────────────


def route_consumers(
    *, owner_slug: str, owner_facts: FactSet | None, method: str, path: str,
    others: dict[str, FactSet], host_map: dict[str, str] | None = None,
) -> list[Consumer]:
    """Calls in `others` that reach the route `method path` of `owner_slug`."""
    hosts = _Hosts(owner_slug, owner_facts, others, host_map or {})
    want = normalize_path(path)
    out: list[Consumer] = []
    for slug, facts in others.items():
        for call in facts.calls:
            if not methods_compatible(method, call.method):
                continue
            how = paths_match(want, call.path)
            if how is None:
                continue
            side, why = hosts.classify(facts, call.host, call.host_hint)
            if side == "other":
                continue  # addressed to a different repository of the workspace
            strong = side == "owner" and how == "exact"
            if not strong and how == "suffix":
                why = "call path carries a prefix the route does not"
            elif not strong and how == "holes":
                why = "the call path has no literal segment, so it fits any route of that length"
            elif not strong:
                why = why or "path and method only; the host is not known to belong to the repository"
            out.append(Consumer(
                repo_slug=slug, file=call.file, line=call.line, kind="http",
                confidence=STRONG if strong else WEAK,
                detail=f"{call.method or '?'} {call.path}", caller=call.caller, why=why))
    return sorted(out, key=lambda c: (not c.strong, c.repo_slug, c.file, c.line))


# ─── import links ────────────────────────────────────────────────


def _package_of_file(published, file: str) -> list:  # noqa: ANN001
    """The published packages whose manifest directory contains `file`."""
    out = []
    for p in published:
        base = p.file.rsplit("/", 1)[0] if "/" in p.file else ""
        if not base or file == base or file.startswith(base + "/"):
            out.append(p)
    return out


def _strip_ext(path: str) -> str:
    stem = path.rsplit("/", 1)[-1]
    return path[: len(path) - len(stem)] + stem.rsplit(".", 1)[0] if "." in stem else path


def _base_of(pkg) -> str:  # noqa: ANN001
    return pkg.file.rsplit("/", 1)[0] if "/" in pkg.file else ""


def _rel_to(pkg, file: str) -> str:  # noqa: ANN001
    base = _base_of(pkg)
    return file[len(base) + 1:] if base and file.startswith(base + "/") else file


def _py_module(pkg, file: str) -> str:  # noqa: ANN001
    """`src/acme/legacy.py` -> `acme.legacy`; `acme/__init__.py` -> `acme`."""
    parts = _strip_ext(_rel_to(pkg, file)).split("/")
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(init_module: str, module: str) -> str:
    """`.client` seen in the `__init__` of package `acme` -> `acme.client`."""
    level = len(module) - len(module.lstrip("."))
    rest = module.lstrip(".")
    if level == 0:
        return module
    base = init_module.split(".") if init_module else []
    base = base[: len(base) - (level - 1)] if level > 1 else base
    return ".".join([*base, *([rest] if rest else [])])


def _py_reexports(owner: FactSet | None, pkg, module: str, leaf: str, target: str) -> bool:  # noqa: ANN001
    """Does the package `module` re-export `leaf` from the module `target`?"""
    for e in (owner.exports if owner is not None else []):
        if _py_module(pkg, e.file) != module:
            continue
        if _resolve_relative(module, e.module) == target and (leaf in e.names or "*" in e.names):
            return True
    return False


def _import_link(pkg, imp, file: str, name: str,  # noqa: ANN001
                 owner: FactSet | None) -> str | None:
    """How firmly the import `imp` reaches the module that defines `name` in
    `file`: STRONG when the imported module path IS that file (or a package
    `__init__` that re-exports it, where the owner's facts say so), WEAK when
    it is the same package but not provably the same module, None when it is
    not that package at all."""
    spec = imp.package
    leaf = name.split(".", 1)[0]
    if pkg.ecosystem == "pypi":
        top = normalize_py_name(spec.split(".")[0])
        if top not in {normalize_py_name(m) for m in [pkg.name, *pkg.modules]}:
            return None
        target = _py_module(pkg, file)
        if spec == target:
            return STRONG
        if any(f"{spec}.{n}" == target for n in imp.names):
            return STRONG  # from acme import legacy
        if target.startswith(spec + ".") and _py_reexports(owner, pkg, spec, leaf, target):
            return STRONG
        return WEAK
    if pkg.ecosystem == "npm":
        if not (spec == pkg.name or spec.startswith(pkg.name + "/")):
            return None
        sub = spec[len(pkg.name):].strip("/")
        rel = _strip_ext(_rel_to(pkg, file))
        for prefix in ("src/", "lib/", "dist/", "build/"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
            if sub.startswith(prefix):
                sub = sub[len(prefix):]
        if not sub:
            return STRONG if rel in ("index", "main") else WEAK
        return STRONG if sub == rel or sub == rel.removesuffix("/index") else WEAK
    if pkg.ecosystem == "go":
        if not (spec == pkg.name or spec.startswith(pkg.name + "/")):
            return None
        directory = _rel_to(pkg, file).rsplit("/", 1)[0] if "/" in _rel_to(pkg, file) else ""
        want = pkg.name + (f"/{directory}" if directory else "")
        return STRONG if spec == want else WEAK
    if pkg.ecosystem == "maven":
        group = pkg.name.split(":", 1)[0]
        if not spec.startswith(group + "."):
            return None
        marker = "/java/"
        rel = _strip_ext(file)
        dotted = rel.split(marker, 1)[1].replace("/", ".") if marker in rel else ""
        if dotted and (spec == dotted or spec.rsplit(".", 1)[0] == dotted.rsplit(".", 1)[0]
                       and spec.endswith(".*")):
            return STRONG
        return WEAK
    return None


def symbol_consumers(
    *, owner_facts: FactSet | None, file: str, names: list[str], others: dict[str, FactSet],
) -> list[Consumer]:
    """Imports of the package that publishes `file`, and the calls of `names`.

    `names` are `func` or `Class.method`. A call is a STRONG link only when
    the importing file calls the name AND the module it imported is the one
    that defines it (see `_import_link`); an import of the same package by
    another module is WEAK, and so is one that merely names `Class`.
    """
    if owner_facts is None:
        return []
    packages = _package_of_file(owner_facts.published, file)
    if not packages:
        return []
    out: list[Consumer] = []
    for slug, facts in others.items():
        for imp in facts.imports:
            for name in names:
                link = None
                for p in packages:
                    got = _import_link(p, imp, file, name, owner_facts)
                    if got == STRONG or (got == WEAK and link is None):
                        link = got
                if link is None:
                    continue
                hits = [c for c in imp.calls if c.name == name]
                for c in hits:
                    out.append(Consumer(
                        repo_slug=slug, file=imp.file, line=c.line, kind="import",
                        confidence=link, detail=f"{imp.package}.{name}",
                        why=(f"imports {imp.package} and calls {name}" if link == STRONG else
                             f"imports {imp.package}, which is not shown to be the module "
                             f"that defines {name}")))
                if hits:
                    continue
                head = name.split(".", 1)[0]
                if name in imp.names or "*" in imp.names:
                    out.append(Consumer(
                        repo_slug=slug, file=imp.file, line=imp.line, kind="import",
                        confidence=WEAK, detail=f"{imp.package}.{name}",
                        why=f"imports {name} from {imp.package}; no call of it was found"))
                elif "." in name and head in imp.names:
                    out.append(Consumer(
                        repo_slug=slug, file=imp.file, line=imp.line, kind="import",
                        confidence=WEAK, detail=f"{imp.package}.{head}",
                        why=f"imports {head}, which owns {name}; the call goes through an instance"))
    return sorted(set(out), key=lambda c: (not c.strong, c.repo_slug, c.file, c.line))


# ─── entry points ────────────────────────────────────────────────


@dataclass
class WorkspaceView:
    """Everything one review needs from the workspace, loaded ONCE."""

    others: dict[str, FactSet] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    owner_facts: FactSet | None = None
    #: The changed repository itself has no recorded facts (indexed before
    #: cross-repository facts existed): what it publishes is not known.
    owner_missing: bool = False


def workspace_view(
    workspace_id: str | None, owner_slug: str, *,
    load: Callable[[str], FactSet | None] | None = None,
    owner_facts: FactSet | None = None,
) -> WorkspaceView:
    others, unknown = workspace_facts(workspace_id, owner_slug, load=load)
    if owner_facts is None and load is None:
        owner_facts = _own_facts(owner_slug)
    return WorkspaceView(others, sorted(unknown), owner_facts, owner_facts is None)


def _impact(view: WorkspaceView, consumers: list[Consumer]) -> Impact:
    return Impact(consumers=consumers, checked=sorted(view.others), unknown=list(view.unknown),
                  owner_missing=view.owner_missing)


def consumers_of_route(
    *, workspace_id: str | None, owner_slug: str, method: str, path: str,
    owner_facts: FactSet | None = None,
    load: Callable[[str], FactSet | None] | None = None,
    view: WorkspaceView | None = None,
) -> Impact:
    if view is None:
        view = workspace_view(workspace_id, owner_slug, load=load, owner_facts=owner_facts)
    return _impact(view, route_consumers(
        owner_slug=owner_slug, owner_facts=view.owner_facts, method=method, path=path,
        others=view.others, host_map=configured_host_map()))


def consumers_of_symbols(
    *, workspace_id: str | None, owner_slug: str, file: str, names: list[str],
    owner_facts: FactSet | None = None,
    load: Callable[[str], FactSet | None] | None = None,
    view: WorkspaceView | None = None,
) -> Impact:
    if view is None:
        view = workspace_view(workspace_id, owner_slug, load=load, owner_facts=owner_facts)
    return _impact(view, symbol_consumers(
        owner_facts=view.owner_facts, file=file, names=names, others=view.others))


def _own_facts(slug: str) -> FactSet | None:
    try:
        from src.config import get_settings

        return load_facts(get_settings().repo_graph_path(slug))
    except Exception as exc:  # noqa: BLE001
        logger.debug("impact_own_facts_unreadable repo=%s err=%s", slug, exc)
        return None
