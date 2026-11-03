"""Routes, HTTP calls and external imports of a Java file.

Routes: Spring `@GetMapping`, `@PostMapping`, ..., `@RequestMapping(method=)`,
with the class-level `@RequestMapping` prefix. JAX-RS (`@Path`) is not read.

Calls: `RestTemplate` (`getForObject`, `postForEntity`, `exchange(url, HttpMethod.X, ..)`,
`put`, `delete`, ...) and `WebClient` / `RestClient` chains
(`.post().uri("/x")`, `.method(HttpMethod.X).uri(..)`).

Imports record the class imported; its calls are `Class.method(...)` and
`new Class(...)`.
"""

from __future__ import annotations

import re

from src.indexing.graph.xrepo.common import (
    Parts,
    clean_hint,
    line_of,
    lit,
    merge,
    method_word,
    text_of,
    url_from_parts,
    var,
    walk,
)
from src.indexing.graph.xrepo.model import (
    ANY_METHOD,
    CallFact,
    FactSet,
    ImportCall,
    ImportFact,
    RouteFact,
    normalize_path,
)

_MAPPINGS = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
             "PatchMapping": "PATCH", "DeleteMapping": "DELETE"}
_TEMPLATE = {
    "getForObject": "GET", "getForEntity": "GET", "postForObject": "POST",
    "postForEntity": "POST", "postForLocation": "POST", "put": "PUT",
    "delete": "DELETE", "patchForObject": "PATCH", "headForHeaders": "HEAD",
    "exchange": "", "execute": "",
}
_STDLIB_PREFIXES = ("java.", "javax.", "jakarta.", "jdk.", "sun.")


def extract(root, source: bytes, rel: str) -> FactSet:  # noqa: ANN001, ARG001
    facts = FactSet()
    consts: dict[str, Parts] = {}
    for node in walk(root):
        if node.type == "variable_declarator":
            name, val = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and val is not None and val.type == "string_literal":
                consts[text_of(name)] = lit(_str(val))
    for node in walk(root):
        if node.type == "class_declaration":
            _class_routes(node, rel, facts)
        elif node.type == "method_invocation":
            _call(node, rel, facts, consts)
    _imports(root, rel, facts)
    return facts


def _str(node) -> str:  # noqa: ANN001
    return "".join(text_of(c) for c in node.children if c.type == "string_fragment")


def _annotations(node) -> list:  # noqa: ANN001
    mods = next((c for c in node.children if c.type == "modifiers"), None)
    return [c for c in (mods.children if mods is not None else [])
            if c.type in ("annotation", "marker_annotation")]


def _annot_args(annot) -> dict[str, object]:  # noqa: ANN001
    """name -> value node; the single unnamed value is stored as `value`."""
    out: dict[str, object] = {}
    args = next((c for c in annot.children if c.type == "annotation_argument_list"), None)
    for a in (args.named_children if args is not None else []):
        if a.type == "element_value_pair":
            out[text_of(a.child_by_field_name("key"))] = a.child_by_field_name("value")
        else:
            out.setdefault("value", a)
    return out


def _paths(value) -> list[str]:  # noqa: ANN001
    if value is None:
        return [""]
    if value.type == "string_literal":
        return [_str(value)]
    if value.type == "element_value_array_initializer":
        return [_str(c) for c in value.named_children if c.type == "string_literal"] or [""]
    return [""]


def _http_method(value) -> list[str]:  # noqa: ANN001
    if value is None:
        return []
    return [m for m in (method_word(t.split(".")[-1]) for t in re.findall(r"[A-Za-z_.]+", text_of(value))) if m]


def _class_routes(cls, rel: str, facts: FactSet) -> None:  # noqa: ANN001
    prefixes = [""]
    for a in _annotations(cls):
        if text_of(a.child_by_field_name("name")) == "RequestMapping":
            args = _annot_args(a)
            prefixes = _paths(args.get("value") or args.get("path"))
    body = cls.child_by_field_name("body")
    for member in (body.named_children if body is not None else []):
        if member.type != "method_declaration":
            continue
        handler = text_of(member.child_by_field_name("name"))
        for a in _annotations(member):
            aname = text_of(a.child_by_field_name("name"))
            if aname in _MAPPINGS:
                methods = [_MAPPINGS[aname]]
            elif aname == "RequestMapping":
                methods = _http_method(_annot_args(a).get("method")) or [ANY_METHOD]
            else:
                continue
            args = _annot_args(a)
            for pre in prefixes:
                for sub in _paths(args.get("value") or args.get("path")):
                    for m in methods:
                        facts.routes.append(RouteFact(
                            method=m, path=normalize_path(pre + "/" + sub.lstrip("/") if sub else pre or "/"),
                            file=rel, line=line_of(a), handler=handler, framework="spring"))


def _parts(node, consts: dict[str, Parts]) -> Parts | None:  # noqa: ANN001
    if node is None:
        return None
    t = node.type
    if t == "string_literal":
        return lit(_str(node))
    if t == "binary_expression" and text_of(node.child_by_field_name("operator")) == "+":
        left = _parts(node.child_by_field_name("left"), consts)
        right = _parts(node.child_by_field_name("right"), consts)
        return merge(left if left is not None else lit("{}"),
                     right if right is not None else lit("{}"))
    if t == "identifier":
        return consts.get(text_of(node)) or var(text_of(node))
    if t == "field_access":
        return var(text_of(node.child_by_field_name("field")))
    if t == "parenthesized_expression" and node.named_children:
        return _parts(node.named_children[0], consts)
    if t == "method_invocation":
        name = text_of(node.child_by_field_name("name"))
        obj = node.child_by_field_name("object")
        if name == "format" and obj is not None and obj.type == "string_literal":
            fmt = re.sub(r"%[-+# 0-9.]*[a-zA-Z]", "{}", _str(obj))
            return lit(fmt)
        if name == "getProperty" or name == "getenv":
            args = node.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            if first is not None and first.type == "string_literal":
                return var(_str(first))
        return lit("{}")
    return None


def _args(node) -> list:  # noqa: ANN001
    a = node.child_by_field_name("arguments")
    return list(a.named_children) if a is not None else []


def _chain(node) -> list:  # noqa: ANN001
    """Method invocations from the innermost receiver outwards."""
    out = []
    cur = node
    while cur is not None and cur.type == "method_invocation":
        out.append(cur)
        cur = cur.child_by_field_name("object")
    return list(reversed(out))


def _call(node, rel: str, facts: FactSet, consts: dict[str, Parts]) -> None:  # noqa: ANN001
    name = text_of(node.child_by_field_name("name"))
    args = _args(node)
    method, url_node, client = "", None, ""
    if name in _TEMPLATE and args:
        obj = node.child_by_field_name("object")
        if not re.search(r"(template|rest|client)$", text_of(obj).rsplit(".", 1)[-1], re.I):
            return
        client = text_of(obj)
        method, url_node = _TEMPLATE[name], args[0]
        if name == "exchange" and len(args) > 1:
            method = _http_method(args[1])[0] if _http_method(args[1]) else ""
        if name == "put" and len(args) < 2:
            return
    elif name == "uri" and args:
        chain = _chain(node.child_by_field_name("object"))
        names = [text_of(c.child_by_field_name("name")) for c in chain]
        if not names:
            return
        root = chain[0].child_by_field_name("object")
        client = text_of(root)
        for c in chain:
            cname = text_of(c.child_by_field_name("name"))
            if cname in ("get", "post", "put", "delete", "patch", "head", "options"):
                method = cname.upper()
            elif cname == "method":
                got = _http_method(_args(c)[0]) if _args(c) else []
                method = got[0] if got else ""
        if not method:
            return
        url_node = args[0]
    else:
        return
    parts = _parts(url_node, consts)
    if not parts:
        return
    url = url_from_parts(parts)
    if url is None:
        return
    host, hint, path = url
    facts.calls.append(CallFact(
        method=method, path=path, file=rel, line=line_of(node), host=host,
        host_hint=clean_hint(hint), client=client, caller=_enclosing(node)))


def _enclosing(node) -> str:  # noqa: ANN001
    cur = node.parent
    while cur is not None:
        if cur.type == "method_declaration":
            return text_of(cur.child_by_field_name("name"))
        cur = cur.parent
    return ""


def _imports(root, rel: str, facts: FactSet) -> None:  # noqa: ANN001
    bound: dict[str, ImportFact] = {}
    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        txt = text_of(node).removeprefix("import").strip().rstrip(";").strip()
        if txt.startswith("static "):
            txt = txt[len("static "):].strip()
        if txt.startswith(_STDLIB_PREFIXES) or txt.startswith("static"):
            continue
        cls = txt.rsplit(".", 1)[-1]
        fact = ImportFact(package=txt, names=[cls], file=rel, line=line_of(node), ecosystem="maven")
        facts.imports.append(fact)
        if cls != "*":
            bound[cls] = fact
    if not bound:
        return
    for node in walk(root):
        if node.type == "method_invocation":
            obj = node.child_by_field_name("object")
            if obj is not None and obj.type == "identifier" and text_of(obj) in bound:
                bound[text_of(obj)].calls.append(ImportCall(
                    name=f"{text_of(obj)}.{text_of(node.child_by_field_name('name'))}",
                    line=line_of(node)))
        elif node.type == "object_creation_expression":
            t = node.child_by_field_name("type")
            if t is not None and text_of(t) in bound:
                bound[text_of(t)].calls.append(ImportCall(name=text_of(t), line=line_of(node)))
