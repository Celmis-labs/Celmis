"""Routes, HTTP calls and external imports of a Python file.

Routes: FastAPI / Starlette / Flask decorators (`@app.get`, `@bp.route`,
`@router.api_route`), `Route(...)`, `add_route`, `add_api_route`,
`add_url_rule`. A router's own `prefix=` / `url_prefix=` is applied; a prefix
given where the router is MOUNTED (`include_router(..., prefix=)`, in another
file) is not known here, which the matcher answers with a weaker "suffix" link.

Calls: httpx, requests and aiohttp, as module functions or through a client
the file built (`httpx.Client(base_url=...)`, `requests.Session()`,
`aiohttp.ClientSession()`), and any receiver named like a client.
"""

from __future__ import annotations

import re
import sys

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
    CallFact,
    ExportFact,
    FactSet,
    ImportCall,
    ImportFact,
    RouteFact,
    normalize_path,
)

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_ATTRS = {"route", "api_route"}
_CLIENT_CTORS = {"Client", "AsyncClient", "Session", "ClientSession"}
_HTTP_MODULES = {"httpx", "requests", "aiohttp"}
_CLIENT_NAME = re.compile(r"(client|session|http)$", re.I)
_STDLIB = set(sys.stdlib_module_names) | {"__future__"}


def extract(root, source: bytes, rel: str) -> FactSet:  # noqa: ANN001, ARG001
    facts = FactSet()
    ctx = _Ctx(rel)
    ctx.scan_module(root)
    for node in walk(root):
        t = node.type
        if t == "decorated_definition":
            _decorated_routes(node, ctx, facts)
        elif t == "call":
            _call_node(node, ctx, facts)
    _imports(root, ctx, facts)
    return facts


class _Ctx:
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.mod_alias: dict[str, str] = {}  # local name -> module (top-level or dotted)
        self.prefix: dict[str, str] = {}  # router variable -> its own prefix
        self.clients: dict[str, Parts] = {}  # client variable text -> base url parts
        self.framework = "python"

    def scan_module(self, root) -> None:  # noqa: ANN001
        for node in walk(root):
            t = node.type
            if t == "import_statement":
                for child in node.named_children:
                    name, alias = _dotted_alias(child)
                    if name:
                        self.mod_alias[alias or name.split(".")[0]] = name
                        if name.split(".")[0] in ("fastapi", "flask", "starlette"):
                            self.framework = name.split(".")[0]
            elif t == "import_from_statement":
                mod = text_of(node.child_by_field_name("module_name"))
                top = mod.lstrip(".").split(".")[0]
                if top in ("fastapi", "flask", "starlette"):
                    self.framework = top
            elif t == "assignment":
                self._assignment(node)
            elif t == "as_pattern":
                self._with_item(node)

    def _assignment(self, node) -> None:  # noqa: ANN001
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or right.type != "call":
            return
        ctor = text_of(right.child_by_field_name("function"))
        args = right.child_by_field_name("arguments")
        key = text_of(left)
        last = ctor.rsplit(".", 1)[-1]
        if last in ("APIRouter", "Blueprint", "Router"):
            for kw in ("prefix", "url_prefix"):
                value = _kwarg(args, kw)
                parts = _string_parts(value, self) if value is not None else None
                if parts:
                    self.prefix[key] = "".join(v for k, v in parts if k == "lit")
        elif last in _CLIENT_CTORS and ctor.split(".")[0] in _http_names(self):
            self.clients[key] = self._base_parts(args)

    def _with_item(self, node) -> None:  # noqa: ANN001
        # `with httpx.Client(base_url=...) as c` / `async with ClientSession() as s`
        kids = node.named_children
        if len(kids) < 2 or kids[0].type != "call":
            return
        ctor = text_of(kids[0].child_by_field_name("function"))
        if ctor.rsplit(".", 1)[-1] in _CLIENT_CTORS and ctor.split(".")[0] in _http_names(self):
            self.clients[text_of(kids[-1])] = self._base_parts(kids[0].child_by_field_name("arguments"))

    def _base_parts(self, args) -> Parts:  # noqa: ANN001
        for kw in ("base_url", "base"):
            value = _kwarg(args, kw)
            if value is not None:
                return _string_parts(value, self) or []
        return []


def _http_names(ctx: _Ctx) -> set[str]:
    return _HTTP_MODULES | {k for k, v in ctx.mod_alias.items() if v.split(".")[0] in _HTTP_MODULES}


def _dotted_alias(node) -> tuple[str, str]:  # noqa: ANN001
    if node.type == "aliased_import":
        return text_of(node.child_by_field_name("name")), text_of(node.child_by_field_name("alias"))
    if node.type == "dotted_name":
        return text_of(node), ""
    return "", ""


def _kwarg(args, name: str):  # noqa: ANN001, ANN202
    if args is None:
        return None
    for a in args.named_children:
        if a.type == "keyword_argument" and text_of(a.child_by_field_name("name")) == name:
            return a.child_by_field_name("value")
    return None


def _positional(args) -> list:  # noqa: ANN001
    return [a for a in (args.named_children if args is not None else [])
            if a.type != "keyword_argument"]


# ─── strings ─────────────────────────────────────────────────────


def _string_parts(node, ctx: _Ctx) -> Parts | None:  # noqa: ANN001
    """The pieces of a string expression, or None when it is not one."""
    if node is None:
        return None
    t = node.type
    if t == "string":
        out: Parts = []
        for ch in node.children:
            if ch.type == "string_content":
                out = merge(out, lit(text_of(ch)))
            elif ch.type == "interpolation":
                inner = ch.named_children[0] if ch.named_children else None
                out = merge(out, var(clean_hint(text_of(inner))) if inner is not None else lit("{}"))
        return out
    if t == "binary_operator":
        op = text_of(node.child_by_field_name("operator"))
        left = _string_parts(node.child_by_field_name("left"), ctx)
        right = _string_parts(node.child_by_field_name("right"), ctx)
        if op == "+":
            return merge(left if left is not None else lit("{}"),
                         right if right is not None else lit("{}"))
        if op == "%" and left is not None:
            return left
        return None
    if t == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "attribute" and text_of(fn.child_by_field_name("attribute")) == "format":
            return _string_parts(fn.child_by_field_name("object"), ctx)
        if text_of(fn) in ("str", "int"):
            return lit("{}")
        return None
    if t in ("identifier", "attribute"):
        return var(clean_hint(text_of(node)))
    return None


def _plain_string(node) -> str | None:  # noqa: ANN001
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.children):
        return None
    return "".join(text_of(c) for c in node.children if c.type == "string_content")


# ─── routes ──────────────────────────────────────────────────────


def _decorated_routes(node, ctx: _Ctx, facts: FactSet) -> None:  # noqa: ANN001
    fn = node.child_by_field_name("definition")
    if fn is None or fn.type != "function_definition":
        return
    handler = text_of(fn.child_by_field_name("name"))
    for dec in node.children:
        if dec.type != "decorator":
            continue
        call = next((c for c in dec.named_children if c.type == "call"), None)
        if call is None:
            continue
        func = call.child_by_field_name("function")
        if func is None or func.type != "attribute":
            continue
        attr = text_of(func.child_by_field_name("attribute"))
        if attr not in _VERBS and attr not in _ROUTE_ATTRS:
            continue
        args = call.child_by_field_name("arguments")
        pos = _positional(args)
        path = _plain_string(pos[0]) if pos else None
        if path is None:
            path = _plain_string(_kwarg(args, "path")) or _plain_string(_kwarg(args, "rule"))
        if path is None or not path.startswith("/"):
            continue
        owner = text_of(func.child_by_field_name("object"))
        full = normalize_path(ctx.prefix.get(owner, "") + path)
        methods = [attr.upper()] if attr in _VERBS else _methods_kw(args) or ["GET"]
        for m in methods:
            facts.routes.append(RouteFact(
                method=m, path=full, file=ctx.rel, line=line_of(dec),
                handler=handler, framework=ctx.framework))


def _methods_kw(args) -> list[str]:  # noqa: ANN001
    value = _kwarg(args, "methods")
    if value is None:
        return []
    return [m for m in (method_word(_plain_string(s) or "") for s in value.named_children) if m]


def _route_calls(node, ctx: _Ctx, facts: FactSet, name: str) -> bool:  # noqa: ANN001
    """`Route("/x", endpoint, methods=[..])`, `app.add_route`, `add_api_route`,
    `add_url_rule`. True when `node` was one of them."""
    func = node.child_by_field_name("function")
    last = text_of(func).rsplit(".", 1)[-1]
    if last not in ("Route", "APIRoute", "add_route", "add_api_route", "add_url_rule"):
        return False
    args = node.child_by_field_name("arguments")
    pos = _positional(args)
    path = _plain_string(pos[0]) if pos else None
    if path is None or not path.startswith("/"):
        return True
    handler = clean_hint(text_of(pos[1])) if len(pos) > 1 else ""
    methods = _methods_kw(args) or ["GET"]
    owner = text_of(func.child_by_field_name("object")) if func.type == "attribute" else ""
    full = normalize_path(ctx.prefix.get(owner, "") + path)
    for m in methods:
        facts.routes.append(RouteFact(
            method=m, path=full, file=ctx.rel, line=line_of(node),
            handler=handler, framework=ctx.framework))
    return True


# ─── calls ───────────────────────────────────────────────────────


def _call_node(node, ctx: _Ctx, facts: FactSet) -> None:  # noqa: ANN001
    if _route_calls(node, ctx, facts, ""):
        return
    func = node.child_by_field_name("function")
    if func is None or func.type != "attribute":
        return
    attr = text_of(func.child_by_field_name("attribute"))
    if attr not in _VERBS and attr not in ("request", "stream"):
        return
    recv = text_of(func.child_by_field_name("object"))
    base: Parts | None = None
    if recv in _http_names(ctx):
        base = []
        client = recv
    elif recv in ctx.clients:
        base = ctx.clients[recv]
        client = recv
    elif _CLIENT_NAME.search(recv.rsplit(".", 1)[-1]):
        base = []
        client = recv
    else:
        return
    args = node.child_by_field_name("arguments")
    pos = _positional(args)
    method = attr.upper() if attr in _VERBS else ""
    if attr in ("request", "stream"):
        # (method, url): the url is the SECOND positional argument
        if len(pos) < 2:
            return
        method = method_word(_plain_string(pos[0]) or "")
        url_node = pos[1]
    else:
        if not pos:
            url_node = _kwarg(args, "url")
            if url_node is None:
                return
        else:
            url_node = pos[0]
    parts = _string_parts(url_node, _Ctx(ctx.rel))
    if parts is None or not parts:
        return
    relative = not (parts[0][0] == "lit" and re.match(r"^(?:[a-z][a-z0-9+.\-]*:)?//", parts[0][1], re.I))
    if base and relative and parts[0][0] != "var":
        parts = merge(base, parts)
    elif base and relative and parts[0][0] == "var":
        pass  # an explicit variable base wins over the client's
    url = url_from_parts(parts)
    if url is None:
        return
    host, hint, path = url
    facts.calls.append(CallFact(
        method=method, path=path, file=ctx.rel, line=line_of(node),
        host=host, host_hint=hint, client=client.split(".")[0] if client in _HTTP_MODULES else client,
        caller=_enclosing(node),
    ))


def _enclosing(node) -> str:  # noqa: ANN001
    cur, parts = node.parent, []
    while cur is not None:
        if cur.type in ("function_definition", "class_definition"):
            parts.append(text_of(cur.child_by_field_name("name")))
        cur = cur.parent
    return ".".join(reversed(parts))


# ─── external imports ────────────────────────────────────────────


def _imports(root, ctx: _Ctx, facts: FactSet) -> None:  # noqa: ANN001
    # local binding -> (ImportFact, original name or "" for the module itself)
    bound: dict[str, tuple[ImportFact, str]] = {}
    for node in walk(root):
        if node.type == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            if mod_node is not None and ctx.rel.rsplit("/", 1)[-1] == "__init__.py":
                facts.exports.append(ExportFact(
                    file=ctx.rel, module=text_of(mod_node),
                    names=["*" if c.type == "wildcard_import" else _dotted_alias(c)[0]
                           for c in node.named_children if c != mod_node]))
            if mod_node is None or mod_node.type == "relative_import":
                continue
            mod = text_of(mod_node)
            if mod.split(".")[0] in _STDLIB:
                continue
            fact = ImportFact(package=mod, names=[], file=ctx.rel, line=line_of(node),
                              ecosystem="pypi")
            for child in node.named_children:
                if child == mod_node:
                    continue
                if child.type == "wildcard_import":
                    fact.names.append("*")
                    continue
                name, alias = _dotted_alias(child)
                if name:
                    fact.names.append(name)
                    bound[alias or name] = (fact, name)
            facts.imports.append(fact)
        elif node.type == "import_statement":
            for child in node.named_children:
                name, alias = _dotted_alias(child)
                if not name or name.split(".")[0] in _STDLIB:
                    continue
                fact = ImportFact(package=name, names=[], file=ctx.rel, line=line_of(node),
                                  ecosystem="pypi")
                facts.imports.append(fact)
                bound[alias or name.split(".")[0]] = (fact, "")
    if not bound:
        return
    for node in walk(root):
        if node.type != "call":
            continue
        func = node.child_by_field_name("function")
        if func is None:
            continue
        if func.type == "identifier":
            hit = bound.get(text_of(func))
            if hit and hit[1]:
                hit[0].calls.append(ImportCall(name=hit[1], line=line_of(node)))
        elif func.type == "attribute":
            chain = text_of(func).split(".")
            hit = bound.get(chain[0])
            if hit is None:
                continue
            fact, orig = hit
            last = chain[-1]
            if orig:  # `Client.create()` from `from pkg import Client`
                fact.calls.append(ImportCall(name=f"{orig}.{last}", line=line_of(node)))
            else:  # `pkg.pay()` / `pkg.sub.pay()` from `import pkg`
                fact.calls.append(ImportCall(name=last, line=line_of(node)))
