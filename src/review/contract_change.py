"""What a pull request changed in the CONTRACT of a repository.

A contract is what other code can hold on to: a public function, method or
class, and an HTTP route. A change is breaking when something that worked
against the old definition stops working against the new one:

  * a public symbol or a route is removed (or its file is deleted);
  * a parameter is removed, or - where callers may pass it by name (Python) -
    renamed, or the positional order of the existing ones changes;
  * a parameter that callers did not have to pass becomes required, or a new
    required one appears;
  * a route's HTTP method or path changes.

A body-only change is none of these and is never reported. Return types and
parameter types are not compared.

Old and new definitions are PAIRED by qualified name (`Class.method`) from the
file's text at the pull request's base and head commits when the provider can
read them, and from the diff alone (the lines of each side of every hunk)
otherwise - which sees less: a hunk that cuts a signature in half is not
parsed, and the check says nothing rather than guess.
"""

from __future__ import annotations

import logging
import re
import textwrap
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from tree_sitter_language_pack import get_parser

from src.indexing.graph.xrepo.collect import file_facts
from src.indexing.graph.xrepo.model import RouteFact

logger = logging.getLogger(__name__)

MAX_FILES = 40
MAX_BYTES = 400_000
#: Wall-clock seconds the provider may be asked for file texts; the rest of the
#: files are read from the diff.
READ_BUDGET_SECONDS = 20.0

_LANG_BY_EXT = {
    ".py": "python", ".go": "go", ".java": "java",
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".mts": "typescript", ".cts": "typescript",
}
_SKIP_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|node_modules|vendor|migrations|testdata|fixtures|"
    r"examples?|docs?)/|\.(test|spec)\.[a-z]+$|_test\.go$|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$",
    re.I)

#: Languages where a caller may pass an argument by NAME: only there is a
#: rename a break. Everywhere else a rename changes nothing for the caller.
_BY_NAME = {"python"}


@dataclass(frozen=True)
class Param:
    name: str
    required: bool = True
    kind: str = "pos"  # pos | kw | varpos | varkw


@dataclass
class Definition:
    name: str  # qualified: `pay_invoice`, `Client.pay`
    kind: str  # function | method | class
    params: list[Param]
    line: int
    file: str
    language: str


@dataclass
class ContractChange:
    kind: str  # symbol_removed | param_removed | param_renamed | param_required |
    #            param_reordered | route_removed | route_changed
    symbol: str  # qualified name, or `METHOD /path` for a route
    file: str
    line: int
    detail: str  # one English sentence
    language: str = ""
    route: tuple[str, str] | None = None  # the OLD (method, path), for route kinds
    new_route: tuple[str, str] | None = None

    @property
    def is_route(self) -> bool:
        return self.route is not None

    def lookup_names(self) -> list[str]:
        """The names a caller of this symbol would use."""
        if self.is_route:
            return []
        if self.symbol.endswith(".__init__"):
            return [self.symbol.rsplit(".", 1)[0]]
        return [self.symbol]


# ─── tree-sitter helpers ─────────────────────────────────────────


def _t(node) -> str:  # noqa: ANN001
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _ln(node) -> int:  # noqa: ANN001
    return node.start_point[0] + 1


def _parse(language: str, source: str):  # noqa: ANN202
    grammar = {"tsx": "tsx", "typescript": "typescript", "javascript": "javascript"}.get(language, language)
    return get_parser(grammar).parse(source.encode("utf-8", "replace")).root_node


# ─── definitions ─────────────────────────────────────────────────


def definitions(language: str, rel: str, source: str) -> list[Definition]:
    try:
        root = _parse(language, source)
        extract = {"python": _py_defs, "go": _go_defs, "java": _java_defs}.get(language, _js_defs)
        return extract(root, rel, language)
    except Exception as exc:  # noqa: BLE001 — an unparsable file is "no definitions"
        logger.debug("contract_defs_failed file=%s err=%s", rel, exc)
        return []


def _py_params(node, drop_first: bool) -> list[Param]:  # noqa: ANN001
    out: list[Param] = []
    kw_only = False
    for ch in (node.named_children if node is not None else []):
        t = ch.type
        if t == "identifier":
            out.append(Param(_t(ch), True, "kw" if kw_only else "pos"))
        elif t in ("default_parameter", "typed_default_parameter"):
            out.append(Param(_t(ch.child_by_field_name("name")), False, "kw" if kw_only else "pos"))
        elif t == "typed_parameter":
            inner = ch.named_children[0] if ch.named_children else None
            if inner is not None and inner.type == "list_splat_pattern":
                out.append(Param(_t(inner).lstrip("*"), False, "varpos"))
                kw_only = True
            elif inner is not None and inner.type == "dictionary_splat_pattern":
                out.append(Param(_t(inner).lstrip("*"), False, "varkw"))
            else:
                out.append(Param(_t(inner), True, "kw" if kw_only else "pos"))
        elif t == "list_splat_pattern":
            out.append(Param(_t(ch).lstrip("*"), False, "varpos"))
            kw_only = True
        elif t == "dictionary_splat_pattern":
            out.append(Param(_t(ch).lstrip("*"), False, "varkw"))
        elif t == "keyword_separator":
            kw_only = True
    if drop_first and out and out[0].name in ("self", "cls"):
        out = out[1:]
    return out


def _py_defs(root, rel: str, language: str) -> list[Definition]:  # noqa: ANN001
    out: list[Definition] = []

    def visit(node, prefix: str, in_class: bool) -> None:  # noqa: ANN001
        for ch in node.named_children:
            target = ch
            if ch.type == "decorated_definition":
                target = ch.child_by_field_name("definition") or ch
            if target.type == "function_definition":
                name = _t(target.child_by_field_name("name"))
                if name.startswith("_") and name != "__init__":
                    continue
                qual = f"{prefix}{name}"
                out.append(Definition(
                    qual, "method" if in_class else "function",
                    _py_params(target.child_by_field_name("parameters"), in_class),
                    _ln(target), rel, language))
            elif target.type == "class_definition":
                name = _t(target.child_by_field_name("name"))
                if name.startswith("_"):
                    continue
                out.append(Definition(f"{prefix}{name}", "class", [], _ln(target), rel, language))
                body = target.child_by_field_name("body")
                if body is not None:
                    visit(body, f"{prefix}{name}.", True)

    visit(root, "", False)
    return out


_JS_FUNCS = ("function_declaration", "generator_function_declaration")


def _js_params(node) -> list[Param]:  # noqa: ANN001
    out: list[Param] = []
    for ch in (node.named_children if node is not None else []):
        t = ch.type
        if t == "identifier":
            out.append(Param(_t(ch), True))
        elif t in ("required_parameter", "optional_parameter"):
            pat = ch.child_by_field_name("pattern")
            has_default = ch.child_by_field_name("value") is not None
            rest = pat is not None and pat.type == "rest_pattern"
            out.append(Param(_t(pat).lstrip("."), not (t == "optional_parameter" or has_default or rest),
                             "varpos" if rest else "pos"))
        elif t == "assignment_pattern":
            out.append(Param(_t(ch.child_by_field_name("left")), False))
        elif t == "rest_pattern":
            out.append(Param(_t(ch).lstrip("."), False, "varpos"))
        elif t in ("object_pattern", "array_pattern"):
            # destructured: callers pass ONE argument; it counts by position
            out.append(Param(f"${len(out)}", True))
    return out


def _js_defs(root, rel: str, language: str) -> list[Definition]:  # noqa: ANN001
    out: list[Definition] = []

    def declared(node, exported: bool) -> None:  # noqa: ANN001
        if node.type in _JS_FUNCS and exported:
            out.append(Definition(_t(node.child_by_field_name("name")), "function",
                                  _js_params(node.child_by_field_name("parameters")),
                                  _ln(node), rel, language))
        elif node.type in ("class_declaration", "abstract_class_declaration") and exported:
            cname = _t(node.child_by_field_name("name"))
            out.append(Definition(cname, "class", [], _ln(node), rel, language))
            body = node.child_by_field_name("body")
            for m in (body.named_children if body is not None else []):
                if m.type != "method_definition":
                    continue
                mname = _t(m.child_by_field_name("name"))
                text_head = _t(m)[:40]
                if mname.startswith(("_", "#")) or re.match(r"(private|protected)\b", text_head):
                    continue
                out.append(Definition(f"{cname}.{mname}", "method",
                                      _js_params(m.child_by_field_name("parameters")),
                                      _ln(m), rel, language))
        elif node.type == "lexical_declaration" and exported:
            for d in node.named_children:
                val = d.child_by_field_name("value")
                if d.type == "variable_declarator" and val is not None \
                        and val.type in ("arrow_function", "function_expression", "function"):
                    out.append(Definition(_t(d.child_by_field_name("name")), "function",
                                          _js_params(val.child_by_field_name("parameters")
                                                     or (val.named_children[0] if val.named_children
                                                         and val.named_children[0].type == "identifier" else None)),
                                          _ln(d), rel, language))

    for top in root.named_children:
        if top.type == "export_statement":
            for ch in top.named_children:
                declared(ch, True)
    return out


def _go_defs(root, rel: str, language: str) -> list[Definition]:  # noqa: ANN001
    out: list[Definition] = []
    for top in root.named_children:
        if top.type not in ("function_declaration", "method_declaration"):
            continue
        name = _t(top.child_by_field_name("name"))
        if not name[:1].isupper():
            continue
        qual, kind = name, "function"
        if top.type == "method_declaration":
            recv = _t(top.child_by_field_name("receiver"))
            m = re.search(r"\*?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\)$", recv)
            if not m or not m.group(1)[:1].isupper():
                continue
            qual, kind = f"{m.group(1)}.{name}", "method"
        params: list[Param] = []
        plist = top.child_by_field_name("parameters")
        for p in (plist.named_children if plist is not None else []):
            if p.type == "parameter_declaration":
                names = [_t(n) for n in p.named_children if n.type == "identifier"]
                if p.child_by_field_name("type") is not None and names and \
                        _t(p.child_by_field_name("type")) == names[-1] and len(names) > 0 and len(p.named_children) == 1:
                    names = []  # a lone type: unnamed parameter
                for n in names or [f"_{len(params)}"]:
                    params.append(Param(n, True))
            elif p.type == "variadic_parameter_declaration":
                nm = next((_t(n) for n in p.named_children if n.type == "identifier"), f"_{len(params)}")
                params.append(Param(nm, False, "varpos"))
        out.append(Definition(qual, kind, params, _ln(top), rel, language))
    return out


def _java_defs(root, rel: str, language: str) -> list[Definition]:  # noqa: ANN001
    out: list[Definition] = []

    def visit(cls, prefix: str) -> None:  # noqa: ANN001
        mods = _t(next((c for c in cls.children if c.type == "modifiers"), None))
        is_iface = cls.type == "interface_declaration"
        if "public" not in mods and not is_iface:
            return
        cname = _t(cls.child_by_field_name("name"))
        qual = f"{prefix}{cname}"
        out.append(Definition(qual, "class", [], _ln(cls), rel, language))
        body = cls.child_by_field_name("body")
        for m in (body.named_children if body is not None else []):
            if m.type in ("class_declaration", "interface_declaration", "record_declaration"):
                visit(m, qual + ".")
            elif m.type in ("method_declaration", "constructor_declaration"):
                mmods = _t(next((c for c in m.children if c.type == "modifiers"), None))
                if "public" not in mmods and not is_iface:
                    continue
                name = cname if m.type == "constructor_declaration" else _t(m.child_by_field_name("name"))
                params = []
                plist = m.child_by_field_name("parameters")
                for p in (plist.named_children if plist is not None else []):
                    nm = _t(p.child_by_field_name("name")) or f"_{len(params)}"
                    params.append(Param(nm, p.type != "spread_parameter",
                                        "varpos" if p.type == "spread_parameter" else "pos"))
                out.append(Definition(f"{qual}.{name}" if m.type == "method_declaration" else f"{qual}.<init>",
                                      "method", params, _ln(m), rel, language))

    for top in root.named_children:
        if top.type in ("class_declaration", "interface_declaration", "record_declaration"):
            visit(top, "")
    return out


# ─── comparing parameters ────────────────────────────────────────


def _sig(p: Param) -> str:
    return {"varpos": "*", "varkw": "**"}.get(p.kind, "") + p.name


def compare_params(old: list[Param], new: list[Param], language: str) -> list[tuple[str, str]]:
    """[(kind, sentence)] for what a caller of `old` can no longer do."""
    issues: list[tuple[str, str]] = []
    by_name = language in _BY_NAME
    new_names = {p.name for p in new}
    old_names = {p.name for p in old}

    if by_name:
        removed = [p for p in old if p.name not in new_names]
        added = [p for p in new if p.name not in old_names]
        # A rename: the same position, the same kind, a name that is new.
        renamed: list[tuple[Param, Param]] = []
        for p in list(removed):
            idx = old.index(p)
            cand = new[idx] if idx < len(new) else None
            if cand is not None and cand in added and cand.kind == p.kind and p.kind in ("pos", "kw"):
                renamed.append((p, cand))
                removed.remove(p)
                added.remove(cand)
        for a, b in renamed:
            issues.append(("param_renamed",
                           f"parameter `{a.name}` was renamed to `{b.name}`; callers passing it by keyword break"))
        for p in removed:
            issues.append(("param_removed", f"parameter `{_sig(p)}` was removed"))
        for p in added:
            if p.required:
                issues.append(("param_required", f"new required parameter `{p.name}`"))
        for p in new:
            if p.name in old_names and p.required:
                before = next(o for o in old if o.name == p.name)
                if not before.required:
                    issues.append(("param_required", f"parameter `{p.name}` is now required"))
        common_old = [p.name for p in old if p.kind == "pos" and p.name in new_names]
        common_new = [p.name for p in new if p.kind == "pos" and p.name in old_names]
        if common_old != common_new:
            issues.append(("param_reordered", "the order of the positional parameters changed"))
        else:
            new_pos = [p.name for p in new if p.kind == "pos"]
            for p in added:
                if p.kind == "pos" and new_pos.index(p.name) < len(new_pos) - 1 \
                        and any(n in old_names for n in new_pos[new_pos.index(p.name) + 1:]):
                    issues.append(("param_reordered",
                                   f"parameter `{p.name}` was inserted before existing positional "
                                   "parameters; positional callers now bind to the wrong ones"))
                    break
        for p in new:
            before = next((o for o in old if o.name == p.name), None)
            if before is not None and before.kind == "pos" and p.kind == "kw":
                issues = [i for i in issues if not i[1].startswith("the order of")]
                issues.append(("param_reordered",
                               f"parameter `{p.name}` is now keyword-only; positional callers break"))
        return issues

    # Positional languages: what matters is how many arguments a caller must pass.
    old_req = sum(1 for p in old if p.required)
    new_req = sum(1 for p in new if p.required)
    if len(new) < len(old) and not any(p.kind == "varpos" for p in new):
        gone = ", ".join(f"`{_sig(p)}`" for p in old[len(new):])
        issues.append(("param_removed", f"parameter {gone} was removed"))
    elif new_req > old_req:
        extra = ", ".join(f"`{p.name}`" for p in new if p.required)[:120]
        issues.append(("param_required", f"the call now needs {new_req} argument(s) instead of {old_req} ({extra})"))
    return issues


# ─── pairing old with new ────────────────────────────────────────


def _closest(old: Definition, candidates: list[Definition]) -> Definition:
    return min(candidates, key=lambda d: (abs(len(d.params) - len(old.params)),
                                          sum(a != b for a, b in zip(d.params, old.params, strict=False))))


def compare_definitions(
    old_defs: list[Definition], new_defs: list[Definition], moved: dict[str, list[Definition]],
) -> list[ContractChange]:
    out: list[ContractChange] = []
    new_by: dict[str, list[Definition]] = {}
    for d in new_defs:
        new_by.setdefault(d.name, []).append(d)
    seen_old: set[str] = set()
    for old in old_defs:
        if old.kind == "class":
            if old.name not in new_by and old.name not in moved:
                out.append(ContractChange(
                    "symbol_removed", old.name, old.file, old.line,
                    f"public class `{old.name}` was removed", old.language))
            continue
        cands = new_by.get(old.name) or moved.get(old.name) or []
        if not cands:
            parent = old.name.rsplit(".", 1)[0] if "." in old.name else ""
            # a method of a class that is itself removed is reported once, as the class
            if parent and parent not in new_by and parent not in moved \
                    and any(d.name == parent for d in old_defs):
                continue
            out.append(ContractChange(
                "symbol_removed", old.name, old.file, old.line,
                f"public {old.kind} `{old.name}` was removed", old.language))
            continue
        same_arity_overloads = [d for d in cands if d.params == old.params]
        if same_arity_overloads:
            continue
        key = old.name
        if key in seen_old and len(cands) == 1:
            continue
        seen_old.add(key)
        new = _closest(old, cands)
        for kind, sentence in compare_params(old.params, new.params, old.language):
            out.append(ContractChange(kind, old.name, new.file, new.line,
                                      f"`{old.name}`: {sentence}", old.language))
    return out


def compare_routes(old: list[RouteFact], new: list[RouteFact]) -> list[ContractChange]:
    old_keys = {(r.method, r.path) for r in old}
    new_keys = {(r.method, r.path) for r in new}
    removed = [r for r in old if (r.method, r.path) not in new_keys]
    added = [r for r in new if (r.method, r.path) not in old_keys]
    out: list[ContractChange] = []

    def changed(o: RouteFact, n: RouteFact) -> None:
        what = []
        if o.method != n.method:
            what.append(f"method {o.method} -> {n.method}")
        if o.path != n.path:
            what.append(f"path {o.path} -> {n.path}")
        out.append(ContractChange(
            "route_changed", f"{o.method} {o.path}", n.file, n.line,
            f"route `{o.method} {o.path}` changed: {', '.join(what)}", "",
            route=(o.method, o.path), new_route=(n.method, n.path)))

    for o in list(removed):
        mate = next((n for n in added if o.handler and n.handler == o.handler), None)
        if mate is not None:
            changed(o, mate)
            added.remove(mate)
            removed.remove(o)
    if len(removed) == 1 and len(added) == 1:
        changed(removed[0], added[0])
        removed, added = [], []
    for o in removed:
        out.append(ContractChange(
            "route_removed", f"{o.method} {o.path}", o.file, o.line,
            f"route `{o.method} {o.path}` was removed", "", route=(o.method, o.path)))
    return out


# ─── reading the pull request ────────────────────────────────────


@dataclass
class FileSides:
    old_path: str
    new_path: str
    old: str | None  # None: the file did not exist on that side
    new: str | None
    language: str
    from_diff: bool = False


def _language(path: str) -> str | None:
    return _LANG_BY_EXT.get(PurePosixPath(path).suffix.lower())


def _sides_from_hunks(hunks: list) -> tuple[str, str]:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for h in hunks:
        for line in h.content.splitlines()[1:]:  # drop the @@ header
            if line.startswith("\\"):
                continue
            tag, body = line[:1], line[1:]
            if tag == "-":
                old_lines.append(body)
            elif tag == "+":
                new_lines.append(body)
            else:
                old_lines.append(body)
                new_lines.append(body)
        old_lines.append("")
        new_lines.append("")
    return textwrap.dedent("\n".join(old_lines)), textwrap.dedent("\n".join(new_lines))


def read_sides(pr, provider, notes: list[str] | None = None,  # noqa: ANN001
               budget: float | None = None) -> list[FileSides]:
    """The old and new text of every changed source file: from the provider at
    the base and head commits where it can, else rebuilt from the hunks.

    A side the provider cannot return although the diff says the file exists
    there (a fork, a 404, a timeout) is NOT "no file": the file is read from
    the diff instead. After `budget` seconds (default READ_BUDGET_SECONDS) the
    remaining files are read from the diff too, and `notes` says so.
    """
    if budget is None:
        budget = READ_BUDGET_SECONDS
    started = time.monotonic()
    out_of_time = False
    by_file: dict[tuple[str, str], list] = {}
    for h in getattr(pr, "anchor_hunks", None) or pr.hunks:
        if h.is_binary:
            continue
        by_file.setdefault((h.old_file_path or h.file_path, h.file_path), []).append(h)
    out: list[FileSides] = []
    reader = getattr(provider, "read_file_at", None) if provider is not None else None
    for (old_path, new_path), hunks in list(by_file.items())[:MAX_FILES]:
        first = hunks[0]
        probe = new_path if not first.is_deleted_file else old_path
        lang = _language(probe)
        if lang is None or _SKIP_PATH.search(old_path) or _SKIP_PATH.search(new_path):
            continue
        old = new = None
        from_diff = True
        if callable(reader) and pr.base_sha and pr.head_sha:
            if time.monotonic() - started > budget:
                if not out_of_time and notes is not None:
                    notes.append("reading the files took too long; the rest were compared "
                                 "from the diff alone, which sees less")
                out_of_time = True
            else:
                try:
                    old = None if first.is_new_file else reader(pr.repo, pr.base_sha, old_path)
                    new = None if first.is_deleted_file else reader(pr.repo, pr.head_sha, new_path)
                    from_diff = False
                    if (old is None and not first.is_new_file) or (new is None and not first.is_deleted_file):
                        from_diff = True  # the file exists on that side, the provider just would not say
                    elif (old is not None and len(old) > MAX_BYTES) or (new is not None and len(new) > MAX_BYTES):
                        continue
                except Exception as exc:  # noqa: BLE001 - fall back to the diff, never fail the check
                    logger.info("contract_read_failed file=%s err=%s", new_path, type(exc).__name__)
                    from_diff = True
        if from_diff:
            o, n = _sides_from_hunks(hunks)
            old = None if first.is_new_file else o
            new = None if first.is_deleted_file else n
        out.append(FileSides(old_path, new_path, old, new, lang, from_diff))
    return out


def _related(a: str, b: str) -> bool:
    """Could a name defined in file `a` be the same one that used to live in
    `b`? Same language and the same top-level directory (or both at the top)."""
    pa, pb = PurePosixPath(a), PurePosixPath(b)
    if pa.suffix != pb.suffix:
        return False
    ta = pa.parts[0] if len(pa.parts) > 1 else ""
    tb = pb.parts[0] if len(pb.parts) > 1 else ""
    return ta == tb


def detect(pr, provider=None, notes: list[str] | None = None) -> list[ContractChange]:  # noqa: ANN001
    """Every contract change of the pull request."""
    sides = read_sides(pr, provider, notes)
    old_by_file: dict[str, list[Definition]] = {}
    new_by_file: dict[str, list[Definition]] = {}
    old_routes: list[RouteFact] = []
    new_routes: list[RouteFact] = []
    for s in sides:
        old_by_file[s.new_path] = definitions(s.language, s.old_path, s.old) if s.old else []
        new_by_file[s.new_path] = definitions(s.language, s.new_path, s.new) if s.new else []
        if s.old:
            old_routes.extend(_route_facts(s.language, s.old_path, s.old))
        if s.new:
            new_routes.extend(_route_facts(s.language, s.new_path, s.new))
    # A name counts as moved only into a file that is new or newly gained it.
    gained: list[Definition] = []
    for s in sides:
        had = {d.name for d in old_by_file[s.new_path]}
        for d in new_by_file[s.new_path]:
            if s.old is None or d.name not in had:
                gained.append(d)
    changes: list[ContractChange] = []
    for s in sides:
        if s.old is None:
            continue
        elsewhere: dict[str, list[Definition]] = {}
        for d in gained:
            if d.file != s.new_path and _related(d.file, s.new_path):
                elsewhere.setdefault(d.name, []).append(d)
        changes.extend(compare_definitions(
            old_by_file[s.new_path], new_by_file[s.new_path], elsewhere))
    # Routes are paired across every file of the pull request: one moved
    # from A to B is not removed.
    changes.extend(compare_routes(old_routes, new_routes))
    return _dedupe(changes)


def _route_facts(language: str, rel: str, text: str) -> list[RouteFact]:
    try:
        facts = file_facts(language, _parse(language, text), text.encode("utf-8", "replace"), rel)
    except Exception as exc:  # noqa: BLE001
        logger.debug("contract_routes_failed file=%s err=%s", rel, exc)
        return []
    return list(facts.routes) if facts is not None else []


def _dedupe(changes: list[ContractChange]) -> list[ContractChange]:
    seen: set[tuple] = set()
    out: list[ContractChange] = []
    for c in changes:
        key = (c.kind, c.symbol, c.file)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
