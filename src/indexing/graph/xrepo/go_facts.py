"""Routes, HTTP calls and external imports of a Go file.

Routes: net/http `HandleFunc` / `Handle` (including the Go 1.22 `"POST /x/{id}"`
pattern), gin and echo (`r.GET`, `Any`), chi (`r.Get`, `Method`, `Route`
nesting). `Group("/v1")` variables and chi `Route("/p", func(r) {...})` closures
add their prefix. `Mount` is not followed.

Calls: `http.Get/Post/PostForm/Head`, `http.NewRequest[WithContext]`, and
`Get/Post/Head/PostForm` on a client variable. `fmt.Sprintf` formats and `+`
become path templates; `os.Getenv("X")` at the start of a URL becomes the host
hint `X`.
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

_UPPER = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_CHI = {"Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
        "Head": "HEAD", "Options": "OPTIONS"}
_CLIENT_FUNCS = {"Get": "GET", "Post": "POST", "PostForm": "POST", "Head": "HEAD"}
_CLIENT_NAME = re.compile(r"(client|http|httpc|c)$", re.I)
_FMT = re.compile(r"%[-+# 0-9.*]*[a-zA-Z]")


def extract(root, source: bytes, rel: str) -> FactSet:  # noqa: ANN001, ARG001
    facts = FactSet()
    consts: dict[str, Parts] = {}
    groups: dict[str, str] = {}
    clients: set[str] = set()
    for node in walk(root):
        t = node.type
        if t in ("const_spec", "var_spec"):
            names = [c for c in node.named_children if c.type == "identifier"]
            vals = node.child_by_field_name("value")
            first = vals.named_children[0] if vals is not None and vals.named_children else None
            if names and first is not None:
                p = _parts(first, consts)
                if p is not None and first.type in ("interpreted_string_literal", "raw_string_literal"):
                    consts[text_of(names[0])] = p
        elif t in ("short_var_declaration", "assignment_statement"):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is None or right is None or not left.named_children or not right.named_children:
                continue
            name, value = text_of(left.named_children[0]), right.named_children[0]
            if value.type in ("interpreted_string_literal", "raw_string_literal"):
                p = _parts(value, consts)
                if p is not None:
                    consts[name] = p
            elif value.type == "call_expression":
                fn = value.child_by_field_name("function")
                if fn is not None and fn.type == "selector_expression" \
                        and text_of(fn.child_by_field_name("field")) == "Group":
                    args = _args(value)
                    path = _plain(args[0]) if args else None
                    if path is not None:
                        groups[name] = _prefix(value, text_of(fn.child_by_field_name("operand")), groups) + path
            elif "http.Client" in text_of(value):
                clients.add(name)
    for node in walk(root):
        if node.type == "call_expression":
            _route(node, rel, facts, groups)
            _call(node, rel, facts, consts, clients)
    _imports(root, rel, facts)
    return facts


def _args(node) -> list:  # noqa: ANN001
    a = node.child_by_field_name("arguments")
    return list(a.named_children) if a is not None else []


def _plain(node) -> str | None:  # noqa: ANN001
    if node is None or node.type not in ("interpreted_string_literal", "raw_string_literal"):
        return None
    return text_of(node)[1:-1]


def _prefix(call, owner: str, groups: dict[str, str]) -> str:  # noqa: ANN001
    """The prefix `owner` carries at `call`: a closure parameter of an
    enclosing `.Route("/p", func(r) {...})`, else its `Group(...)` assignment."""
    cur = call.parent
    while cur is not None:
        if cur.type == "func_literal":
            outer = cur.parent.parent if cur.parent is not None else None
            if outer is not None and outer.type == "call_expression":
                fn = outer.child_by_field_name("function")
                params = [text_of(p.child_by_field_name("name")) for p in _param_decls(cur)]
                if fn is not None and fn.type == "selector_expression" \
                        and text_of(fn.child_by_field_name("field")) in ("Route", "Group") \
                        and owner in params:
                    args = _args(outer)
                    path = _plain(args[0]) if args else ""
                    return _prefix(outer, text_of(fn.child_by_field_name("operand")), groups) + (path or "")
        cur = cur.parent
    return groups.get(owner, "")


def _param_decls(func_lit) -> list:  # noqa: ANN001
    params = func_lit.child_by_field_name("parameters")
    return [p for p in (params.named_children if params is not None else [])
            if p.type == "parameter_declaration"]


def _route(node, rel: str, facts: FactSet, groups: dict[str, str]) -> None:  # noqa: ANN001
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return
    name = text_of(fn.child_by_field_name("field"))
    owner = text_of(fn.child_by_field_name("operand"))
    args = _args(node)
    method = ""
    if name in ("HandleFunc", "Handle"):
        pattern = _plain(args[0]) if args else None
        if pattern is None:
            return
        m = re.match(r"^([A-Z]+)\s+(\S.*)$", pattern)
        method, path = (m.group(1), m.group(2)) if m else (ANY_METHOD, pattern)
        path = re.sub(r"^[^/]*(?=/)", "", path) if not path.startswith("/") else path  # host-qualified
        handler_arg = args[1] if len(args) > 1 else None
    elif name in _UPPER or name == "Any":
        path = _plain(args[0]) if args else None
        method = ANY_METHOD if name == "Any" else name
        handler_arg = args[-1] if len(args) > 1 else None
    elif name in _CHI and len(args) >= 2:
        path, method, handler_arg = _plain(args[0]), _CHI[name], args[-1]
    elif name in ("Method", "MethodFunc") and len(args) >= 3:
        method = method_word(_plain(args[0]) or "") or ANY_METHOD
        path, handler_arg = _plain(args[1]), args[-1]
    else:
        return
    if path is None or not path.startswith("/"):
        return
    full = _prefix(node, owner, groups) + path
    handler = clean_hint(text_of(handler_arg)) if handler_arg is not None \
        and handler_arg.type in ("identifier", "selector_expression") else ""
    facts.routes.append(RouteFact(
        method=method, path=normalize_path(full), file=rel, line=line_of(node),
        handler=handler, framework="go"))


# ─── calls ───────────────────────────────────────────────────────


def _parts(node, consts: dict[str, Parts]) -> Parts | None:  # noqa: ANN001
    if node is None:
        return None
    t = node.type
    if t in ("interpreted_string_literal", "raw_string_literal"):
        return lit(text_of(node)[1:-1])
    if t == "binary_expression" and text_of(node.child_by_field_name("operator")) == "+":
        left = _parts(node.child_by_field_name("left"), consts)
        right = _parts(node.child_by_field_name("right"), consts)
        return merge(left if left is not None else lit("{}"),
                     right if right is not None else lit("{}"))
    if t == "identifier":
        return consts.get(text_of(node)) or var(text_of(node))
    if t == "selector_expression":
        return var(text_of(node.child_by_field_name("field")))
    if t == "parenthesized_expression" and node.named_children:
        return _parts(node.named_children[0], consts)
    if t == "call_expression":
        fn = text_of(node.child_by_field_name("function"))
        args = _args(node)
        if fn == "fmt.Sprintf" and args:
            fmt = _plain(args[0])
            if fmt is None:
                return None
            # the first verb standing at the very start is the host hint
            rest = [_parts(a, consts) for a in args[1:]]
            out: Parts = []
            pos, i = 0, 0
            for m in _FMT.finditer(fmt):
                out = merge(out, lit(fmt[pos:m.start()]))
                arg = rest[i] if i < len(rest) else None
                if m.start() == 0 and arg and arg[0][0] == "var":
                    out = merge(out, arg[:1])
                elif arg and all(k == "lit" for k, _ in arg) and arg[0][1].startswith(("http", "/")):
                    out = merge(out, arg)
                else:
                    out = merge(out, lit("{}"))
                pos, i = m.end(), i + 1
            return merge(out, lit(fmt[pos:]))
        if fn == "os.Getenv" and args and _plain(args[0]):
            return var(_plain(args[0]) or "")
        return lit("{}")
    return None


def _method_of(node) -> str:  # noqa: ANN001
    if node is None:
        return ""
    plain = _plain(node)
    if plain is not None:
        return method_word(plain)
    if node.type == "selector_expression":  # http.MethodPost
        field = text_of(node.child_by_field_name("field"))
        if field.startswith("Method"):
            return method_word(field[len("Method"):])
    return ""


def _call(node, rel: str, facts: FactSet, consts: dict[str, Parts], clients: set[str]) -> None:  # noqa: ANN001
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return
    name = text_of(fn.child_by_field_name("field"))
    owner = text_of(fn.child_by_field_name("operand"))
    args = _args(node)
    method, url_node, client = "", None, owner
    if owner == "http" and name in _CLIENT_FUNCS and args:
        method, url_node = _CLIENT_FUNCS[name], args[0]
    elif owner == "http" and name == "NewRequest" and len(args) >= 2:
        method, url_node = _method_of(args[0]), args[1]
    elif owner == "http" and name == "NewRequestWithContext" and len(args) >= 3:
        method, url_node = _method_of(args[1]), args[2]
    elif name in _CLIENT_FUNCS and args and (owner in clients or _CLIENT_NAME.search(owner.rsplit(".", 1)[-1])):
        method, url_node = _CLIENT_FUNCS[name], args[0]
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
        method=method, path=path, file=rel, line=line_of(node), host=host, host_hint=hint,
        client=client, caller=_enclosing(node)))


def _enclosing(node) -> str:  # noqa: ANN001
    cur = node.parent
    while cur is not None:
        if cur.type in ("function_declaration", "method_declaration"):
            return text_of(cur.child_by_field_name("name"))
        cur = cur.parent
    return ""


# ─── external imports ────────────────────────────────────────────


def _imports(root, rel: str, facts: FactSet) -> None:  # noqa: ANN001
    bound: dict[str, ImportFact] = {}
    for node in walk(root):
        if node.type != "import_spec":
            continue
        path = _plain(node.child_by_field_name("path"))
        if not path or "." not in path.split("/")[0]:
            continue  # standard library
        alias = text_of(node.child_by_field_name("name"))
        if alias in ("_", "."):
            alias = ""
        local = alias or _default_name(path)
        fact = ImportFact(package=path, names=[], file=rel, line=line_of(node), ecosystem="go")
        facts.imports.append(fact)
        bound[local] = fact
    if not bound:
        return
    for node in walk(root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression":
            continue
        fact = bound.get(text_of(fn.child_by_field_name("operand")))
        if fact is not None:
            called = text_of(fn.child_by_field_name("field"))
            if called not in fact.names:
                fact.names.append(called)
            fact.calls.append(ImportCall(name=called, line=line_of(node)))


def _default_name(path: str) -> str:
    parts = path.split("/")
    if re.fullmatch(r"v\d+", parts[-1]) and len(parts) > 1:
        return parts[-2]
    return parts[-1].removeprefix("go-")
