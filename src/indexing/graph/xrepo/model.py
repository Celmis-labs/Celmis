"""The fact records, their JSON file, and the path-template algebra."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Bumped when a field changes meaning. A file of another version is read as
#: absent: better "unknown, reindex" than a wrong match.
FACTS_VERSION = 1

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ANY_METHOD = "*"


@dataclass
class RouteFact:
    """A route this repository exposes. `path` is a normalised template."""

    method: str  # GET..OPTIONS, or "*" when the framework does not say
    path: str
    file: str
    line: int
    handler: str = ""
    framework: str = ""


@dataclass
class CallFact:
    """An outgoing HTTP call. `method` is "" when it could not be read.

    `host` is the literal host (no port) when the URL has one; `host_hint` is
    the name of the variable the URL starts with (`BILLING_URL`) when it does
    not. Both empty means a bare path against some client base.
    """

    method: str
    path: str
    file: str
    line: int
    host: str = ""
    host_hint: str = ""
    client: str = ""
    caller: str = ""


@dataclass
class ImportCall:
    name: str
    line: int


@dataclass
class ImportFact:
    """An import of a package that is not this repository's own code."""

    package: str  # as written in the import: `acme_billing.client`, `@acme/ui`
    names: list[str]
    file: str
    line: int
    ecosystem: str = ""
    calls: list[ImportCall] = field(default_factory=list)


@dataclass
class PublishedPackage:
    ecosystem: str  # pypi | npm | go | maven
    name: str  # distribution name; for maven `group:artifact`
    file: str
    #: Importable names when they differ from `name` (python top-level
    #: packages, a go module path is `name` itself).
    modules: list[str] = field(default_factory=list)


@dataclass
class ServiceFact:
    """A compose service: how a hostname is tied to an image or a build dir."""

    name: str
    image: str = ""
    build: str = ""
    file: str = ""


@dataclass
class ExportFact:
    """`from <module> import <names>` in a package `__init__`: those names are
    reachable from the package itself. `module` is as written (`.client`)."""

    file: str
    module: str
    names: list[str] = field(default_factory=list)  # "*" re-exports everything


@dataclass
class FactSet:
    version: int = FACTS_VERSION
    routes: list[RouteFact] = field(default_factory=list)
    calls: list[CallFact] = field(default_factory=list)
    imports: list[ImportFact] = field(default_factory=list)
    published: list[PublishedPackage] = field(default_factory=list)
    services: list[ServiceFact] = field(default_factory=list)
    exports: list[ExportFact] = field(default_factory=list)

    def drop_files(self, files: set[str]) -> None:
        self.routes = [r for r in self.routes if r.file not in files]
        self.calls = [c for c in self.calls if c.file not in files]
        self.imports = [i for i in self.imports if i.file not in files]
        self.published = [p for p in self.published if p.file not in files]
        self.services = [s for s in self.services if s.file not in files]
        self.exports = [e for e in self.exports if e.file not in files]

    def extend(self, other: FactSet) -> None:
        self.routes.extend(other.routes)
        self.calls.extend(other.calls)
        self.imports.extend(other.imports)
        self.published.extend(other.published)
        self.services.extend(other.services)
        self.exports.extend(other.exports)

    def is_empty(self) -> bool:
        return not (self.routes or self.calls or self.imports or self.published
                    or self.services)


# ─── the file next to the graph ──────────────────────────────────


def facts_path(graph_path: Path) -> Path:
    """`graph.fdblite` -> `graph.fdblite.xrepo.json`: it travels with the
    graph and is replaced together with it."""
    return graph_path.with_name(graph_path.name + ".xrepo.json")


def save_facts(graph_path: Path, facts: FactSet) -> None:
    """Atomic: a reader never sees a half-written file."""
    target = facts_path(graph_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".xrepo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(facts), fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def forget_facts(graph_path: Path) -> None:
    """Remove the sidecar: the repository then reads as "facts unknown". Used
    whenever the graph changed and the new facts could not be written, so the
    old ones are never read as current. Never raises."""
    try:
        facts_path(graph_path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("xrepo_facts_not_removed path=%s err=%s", graph_path, exc)


def load_facts(graph_path: Path) -> FactSet | None:
    """None when the repository was indexed before facts existed (or the file
    is unreadable): the caller must say "unknown", never "nothing"."""
    target = facts_path(graph_path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("xrepo_facts_unreadable path=%s err=%s", target, exc)
        return None
    if not isinstance(raw, dict) or raw.get("version") != FACTS_VERSION:
        return None
    try:
        return FactSet(
            routes=[RouteFact(**r) for r in raw.get("routes", [])],
            calls=[CallFact(**c) for c in raw.get("calls", [])],
            imports=[
                ImportFact(**{**i, "calls": [ImportCall(**c) for c in i.get("calls", [])]})
                for i in raw.get("imports", [])
            ],
            published=[PublishedPackage(**p) for p in raw.get("published", [])],
            services=[ServiceFact(**s) for s in raw.get("services", [])],
            exports=[ExportFact(**e) for e in raw.get("exports", [])],
        )
    except TypeError as exc:
        logger.warning("xrepo_facts_malformed path=%s err=%s", target, exc)
        return None


# ─── path templates ──────────────────────────────────────────────

ONE = "{}"  # any single non-empty segment
REST = "{**}"  # everything from here on

_BRACE = re.compile(r"^\{(.*)\}$")
_ANGLE = re.compile(r"^<(.*)>$")


def _segment(seg: str) -> str:
    if seg in ("*",):
        return ONE
    if seg in ("**", "{**}"):
        return REST
    m = _BRACE.match(seg)
    if m and "{" not in m.group(1):
        inner = m.group(1)
        # {*path} (starlette/chi/Go 1.22 `{p...}`), {path:path}, ${x}
        if inner.startswith("*") or inner.endswith("...") or inner.endswith(":path"):
            return REST
        return ONE
    m = _ANGLE.match(seg)
    if m:
        return REST if m.group(1).startswith("path:") else ONE
    if seg.startswith(":"):
        return REST if seg.endswith(("*", "+")) else ONE
    if seg.startswith("[") and seg.endswith("]"):  # next.js style, if it leaks in
        return REST if seg.startswith(("[...", "[[...")) else ONE
    # `file-{id}.json`: keep the literal parts, the hole becomes a regex hole
    if "{" in seg or "<" in seg:
        return re.sub(r"\{[^{}]*\}|<[^<>]*>", "{}", seg)
    return seg


def normalize_path(raw: str) -> str:
    """`/invoices/{id}/pay`, `/invoices/:id/pay`, `/invoices/<int:id>/pay`,
    `/invoices/*/pay` -> `/invoices/{}/pay`. Query and fragment are dropped,
    the trailing slash too."""
    path = (raw or "").strip()
    path = path.split("#", 1)[0].split("?", 1)[0]
    segs = [s for s in path.split("/") if s != ""]
    out: list[str] = []
    for seg in segs:
        norm = _segment(seg)
        out.append(norm)
        if norm == REST:
            break
    return "/" + "/".join(out)


def _seg_matches(route_seg: str, call_seg: str) -> bool:
    if route_seg == call_seg:
        return True
    if route_seg == ONE or call_seg == ONE:
        return True
    if "{}" in route_seg or "{}" in call_seg:
        # embedded hole: compare as a pattern against the other side's literal
        pat, lit = (route_seg, call_seg) if "{}" in route_seg else (call_seg, route_seg)
        rx = "^" + ".+".join(re.escape(p) for p in pat.split("{}")) + "$"
        return re.match(rx, lit) is not None
    return False


def _segs(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def paths_match(route_path: str, call_path: str) -> str | None:
    """How a call path reaches a route path: "exact", "suffix", "holes" or None.

    "holes" is a match by a call path with no literal segment: as weak as
    "suffix", and it never makes a link strong.


    "suffix" means the call carries a prefix the route does not: a router
    mounted under `/v1`, a client with a base path. It is the weaker reading:
    the route's own segments must still contain a literal, otherwise `/{}`
    would match every call there is.
    """
    r, c = _segs(route_path), _segs(call_path)
    # A call path of holes only (`f"{BASE}/{endpoint}"`) says nothing about
    # WHICH route it reaches: it fits any route of that length.
    holey = bool(c) and all(seg in (ONE, REST) for seg in c)

    def walk(rs: list[str], cs: list[str]) -> bool:
        for i, rseg in enumerate(rs):
            if rseg == REST:
                return True  # swallows the remainder, even nothing
            if i >= len(cs) or not _seg_matches(rseg, cs[i]):
                return False
        return len(cs) == len(rs)

    if walk(r, c):
        return "holes" if holey else "exact"
    if r and any(s not in (ONE, REST) for s in r) and r[-1] != REST:
        for cut in range(1, len(c) - len(r) + 1):
            tail = c[cut:]
            # a wildcard on the call side must not be the only thing that
            # lines a literal route segment up: some literal has to agree
            if walk(r, tail) and any(a == b and a not in (ONE, REST) for a, b in zip(r, tail, strict=False)):
                return "suffix"
    # the call path may be the one with a REST (a template that ends in a wildcard)
    if c and c[-1] == REST and walk(c, r):
        return "holes" if holey else "exact"
    return None


def methods_compatible(route_method: str, call_method: str) -> bool:
    if route_method in ("", ANY_METHOD) or call_method in ("", ANY_METHOD):
        return True
    return route_method.upper() == call_method.upper()
