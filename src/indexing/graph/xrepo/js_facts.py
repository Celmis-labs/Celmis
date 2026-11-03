"""Routes, HTTP calls and external imports of a JavaScript / TypeScript file.

Routes: Express / Fastify / Koa-router style `app.get("/x", handler)` (also
`router.route("/x").get(h)`), Next.js `app/**/route.ts` (exported `GET`, `POST`
...) and `pages/api/**`. A prefix given at `app.use("/api", router)` is in
another statement and not applied; the matcher's "suffix" reading covers it.

Calls: `fetch`, `axios` (module, `axios.create` instances, `axios({url})`),
and clients named like one. Template literals and `+` concatenation become
path templates; a leading `${BASE_URL}` becomes a host hint.
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

_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}
_CLIENTS = {"axios", "$http", "http", "httpClient", "ky", "got", "ofetch", "$fetch", "request"}
_CLIENT_NAME = re.compile(r"(client|http|axios|api|instance)$", re.I)
_HANDLER_NODES = {"arrow_function", "function_expression", "function", "identifier",
                  "member_expression", "call_expression", "await_expression"}
_NEXT_APP = re.compile(r"(?:^|/)(?:src/)?app/(?:(.*)/)?route\.[cm]?[jt]sx?$")
_NEXT_PAGES = re.compile(r"(?:^|/)(?:src/)?pages/api/(.*)\.[cm]?[jt]sx?$")
_OPTIONS_NAME = re.compile(r"^(opts?|options|config|cfg|params|headers|init)$", re.I)
_FUNCS = ("function_declaration", "method_definition", "generator_function_declaration")


def extract(root, source: bytes, rel: str) -> FactSet:  # noqa: ANN001, ARG001
    facts = FactSet()
    consts: dict[str, Parts] = {}
    instances: dict[str, Parts] = {}  # axios.create() variables -> baseURL
    for node in walk(root):
        if node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                continue
            if value.type in ("string", "template_string"):
                parts = _parts(value, {})
                if parts is not None:
                    consts[text_of(name)] = parts
            elif value.type == "call_expression":
                fn = text_of(value.child_by_field_name("function"))
                if fn in ("axios.create", "ky.create", "got.extend"):
                    base: Parts = []
                    args = value.child_by_field_name("arguments")
                    cfg = args.named_children[0] if args is not None and args.named_children else None
                    for key in ("baseURL", "baseUrl", "prefixUrl"):
                        v = _prop(cfg, key)
                        if v is not None:
                            base = _parts(v, consts) or []
                    instances[text_of(name)] = base
    _next_routes(root, rel, facts)
    for node in walk(root):
        if node.type == "call_expression" and not _express_route(node, rel, facts, instances):
            _http_call(node, rel, facts, consts, instances)
    _imports(root, rel, facts)
    return facts


# ─── expression helpers ──────────────────────────────────────────


def _parts(node, consts: dict[str, Parts]) -> Parts | None:  # noqa: ANN001
    if node is None:
        return None
    t = node.type
    if t == "string":
        return lit("".join(text_of(c) for c in node.children if c.type == "string_fragment"))
    if t == "template_string":
        out: Parts = []
        for ch in node.children:
            if ch.type == "string_fragment":
                out = merge(out, lit(text_of(ch)))
            elif ch.type == "template_substitution":
                inner = ch.named_children[0] if ch.named_children else None
                if inner is not None and inner.type == "identifier" and text_of(inner) in consts:
                    out = merge(out, consts[text_of(inner)])
                else:
                    out = merge(out, var(clean_hint(text_of(inner))) if inner is not None else lit("{}"))
        return out
    if t == "binary_expression" and text_of(node.child_by_field_name("operator")) == "+":
        left = _parts(node.child_by_field_name("left"), consts)
        right = _parts(node.child_by_field_name("right"), consts)
        return merge(left if left is not None else lit("{}"),
                     right if right is not None else lit("{}"))
    if t == "parenthesized_expression" and node.named_children:
        return _parts(node.named_children[0], consts)
    if t == "identifier":
        name = text_of(node)
        return consts.get(name) or var(name)
    if t in ("member_expression", "subscript_expression"):
        return var(clean_hint(text_of(node)))
    if t == "call_expression":
        return lit("{}")
    return None


def _plain(node) -> str | None:  # noqa: ANN001
    """A string literal with nothing spliced in, else None."""
    if node is None:
        return None
    if node.type == "string":
        return "".join(text_of(c) for c in node.children if c.type == "string_fragment")
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
        return "".join(text_of(c) for c in node.children if c.type == "string_fragment")
    return None


def _prop(obj, key: str):  # noqa: ANN001, ANN202
    if obj is None or obj.type != "object":
        return None
    for pair in obj.named_children:
        if pair.type == "pair" and text_of(pair.child_by_field_name("key")).strip("'\"") == key:
            return pair.child_by_field_name("value")
    return None


def _args(node) -> list:  # noqa: ANN001
    a = node.child_by_field_name("arguments")
    return list(a.named_children) if a is not None else []


def _enclosing(node) -> str:  # noqa: ANN001
    cur = node.parent
    while cur is not None:
        if cur.type in _FUNCS:
            return text_of(cur.child_by_field_name("name"))
        if cur.type == "variable_declarator" and cur.child_by_field_name("value") is not None \
                and cur.child_by_field_name("value").type in ("arrow_function", "function_expression", "function"):
            return text_of(cur.child_by_field_name("name"))
        cur = cur.parent
    return ""


# ─── routes ──────────────────────────────────────────────────────


def _express_route(node, rel: str, facts: FactSet, instances: dict[str, Parts]) -> bool:  # noqa: ANN001
    func = node.child_by_field_name("function")
    if func is None or func.type != "member_expression":
        return False
    verb = text_of(func.child_by_field_name("property"))
    if verb not in _VERBS and verb != "all":
        return False
    obj = func.child_by_field_name("object")
    args = _args(node)
    path = None
    handler_args = args[1:]
    if obj is not None and obj.type == "call_expression" \
            and text_of(obj.child_by_field_name("function")).endswith(".route"):
        inner = _args(obj)  # router.route("/x").get(handler)
        path = _plain(inner[0]) if inner else None
        handler_args = args
    elif args:
        path = _plain(args[0])
    if path is None or not path.startswith("/"):
        return False
    owner = text_of(obj)
    if owner in _CLIENTS or owner in instances or re.search(r"(client|http|axios|instance)$", owner, re.I):
        return False
    if not handler_args or handler_args[-1].type not in _HANDLER_NODES:
        return False
    if handler_args[-1].type == "identifier" and _OPTIONS_NAME.search(text_of(handler_args[-1])):
        return False  # `api.get("/x", options)` is a call with a config
    method = ANY_METHOD if verb == "all" else verb.upper()
    handler = ""
    last = handler_args[-1]
    if last.type == "identifier":
        handler = text_of(last)
    facts.routes.append(RouteFact(
        method=method, path=normalize_path(path), file=rel, line=line_of(node),
        handler=handler or _enclosing(node), framework="express"))
    return True


def _next_segment(seg: str) -> str | None:
    if seg.startswith("(") and seg.endswith(")") or seg.startswith("@"):
        return None  # route group / parallel slot: not in the URL
    if seg.startswith("[[...") or seg.startswith("[..."):
        return "{**}"
    if seg.startswith("[") and seg.endswith("]"):
        return "{}"
    return seg


def _next_path(raw: str) -> str:
    segs = [s for s in (_next_segment(x) for x in raw.split("/")) if s]
    return "/" + "/".join(segs)


def _next_routes(root, rel: str, facts: FactSet) -> None:  # noqa: ANN001
    m = _NEXT_APP.search(rel)
    if m:
        path = _next_path(m.group(1) or "")
        for node in root.named_children:
            if node.type != "export_statement":
                continue
            for name in _exported_names(node):
                if (name in _VERBS or name.lower() in _VERBS) and name.upper() == name:
                    facts.routes.append(RouteFact(
                        method=name, path=normalize_path(path), file=rel,
                        line=line_of(node), handler=name, framework="next-app"))
        return
    m = _NEXT_PAGES.search(rel)
    if m:
        sub = re.sub(r"(^|/)index$", "", m.group(1))
        facts.routes.append(RouteFact(
            method=ANY_METHOD, path=normalize_path(_next_path("api/" + sub)), file=rel, line=1,
            handler="default", framework="next-pages"))


def _exported_names(export) -> list[str]:  # noqa: ANN001
    out: list[str] = []
    for ch in export.named_children:
        if ch.type in ("function_declaration", "generator_function_declaration"):
            out.append(text_of(ch.child_by_field_name("name")))
        elif ch.type == "lexical_declaration":
            for d in ch.named_children:
                if d.type == "variable_declarator":
                    out.append(text_of(d.child_by_field_name("name")))
        elif ch.type == "export_clause":
            for spec in ch.named_children:
                out.append(text_of(spec.child_by_field_name("name")))
    return out


# ─── calls ───────────────────────────────────────────────────────


def _http_call(node, rel: str, facts: FactSet, consts: dict[str, Parts],  # noqa: ANN001
               instances: dict[str, Parts]) -> None:
    func = node.child_by_field_name("function")
    if func is None:
        return
    args = _args(node)
    method = ""
    url_node = None
    base: Parts = []
    client = ""
    if func.type == "identifier" and text_of(func) in ("fetch", "axios", "$fetch", "ofetch", "got", "ky"):
        client = text_of(func)
        if not args:
            return
        first = args[0]
        if first.type == "object":  # axios({url, method})
            url_node, method = _prop(first, "url"), method_word(_plain(_prop(first, "method")) or "")
            b = _prop(first, "baseURL")
            base = _parts(b, consts) or [] if b is not None else []
        else:
            url_node = first
            cfg = args[1] if len(args) > 1 else None
            method = method_word(_plain(_prop(cfg, "method")) or "") or ("GET" if client == "fetch" else "")
    elif func.type == "member_expression":
        prop = text_of(func.child_by_field_name("property"))
        recv = text_of(func.child_by_field_name("object"))
        known = recv in _CLIENTS or recv in instances
        if not (known or _CLIENT_NAME.search(recv.rsplit(".", 1)[-1])):
            return
        if prop in _VERBS:
            method = prop.upper()
            url_node = args[0] if args else None
        elif prop == "request" and args and args[0].type == "object":
            url_node = _prop(args[0], "url")
            method = method_word(_plain(_prop(args[0], "method")) or "")
        else:
            return
        client = recv
        base = instances.get(recv, [])
    else:
        return
    if url_node is None:
        return
    parts = _parts(url_node, consts)
    if not parts:
        return
    absolute = parts[0][0] == "lit" and re.match(r"^(?:[a-z][a-z0-9+.\-]*:)?//", parts[0][1], re.I)
    if base and not absolute and parts[0][0] != "var":
        parts = merge(base, parts)
    url = url_from_parts(parts)
    if url is None:
        return
    host, hint, path = url
    facts.calls.append(CallFact(
        method=method, path=path, file=rel, line=line_of(node), host=host, host_hint=hint,
        client=client, caller=_enclosing(node)))


# ─── external imports ────────────────────────────────────────────


def _package_of(spec: str) -> str | None:
    if not spec or spec.startswith((".", "/", "@/", "~", "#", "node:")):
        return None
    return spec


def _imports(root, rel: str, facts: FactSet) -> None:  # noqa: ANN001
    bound: dict[str, tuple[ImportFact, str]] = {}
    for node in walk(root):
        if node.type == "import_statement":
            src = node.child_by_field_name("source")
            pkg = _package_of(_plain(src) or "")
            if pkg is None:
                continue
            fact = ImportFact(package=pkg, names=[], file=rel, line=line_of(node), ecosystem="npm")
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            for part in (clause.named_children if clause is not None else []):
                if part.type == "identifier":  # default import
                    fact.names.append("default")
                    bound[text_of(part)] = (fact, "default")
                elif part.type == "namespace_import":
                    ident = next((c for c in part.named_children if c.type == "identifier"), None)
                    if ident is not None:
                        bound[text_of(ident)] = (fact, "")
                elif part.type == "named_imports":
                    for spec in part.named_children:
                        name = text_of(spec.child_by_field_name("name"))
                        alias = text_of(spec.child_by_field_name("alias")) or name
                        fact.names.append(name)
                        bound[alias] = (fact, name)
            facts.imports.append(fact)
        elif node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            if val is None or val.type != "call_expression" \
                    or text_of(val.child_by_field_name("function")) != "require":
                continue
            args = _args(val)
            pkg = _package_of(_plain(args[0]) or "") if args else None
            if pkg is None:
                continue
            fact = ImportFact(package=pkg, names=[], file=rel, line=line_of(node), ecosystem="npm")
            target = node.child_by_field_name("name")
            if target is not None and target.type == "identifier":
                bound[text_of(target)] = (fact, "")
            elif target is not None and target.type == "object_pattern":
                for item in target.named_children:
                    if item.type == "shorthand_property_identifier_pattern":
                        fact.names.append(text_of(item))
                        bound[text_of(item)] = (fact, text_of(item))
                    elif item.type == "pair_pattern":
                        key = text_of(item.child_by_field_name("key"))
                        val_node = item.child_by_field_name("value")
                        fact.names.append(key)
                        bound[text_of(val_node)] = (fact, key)
            facts.imports.append(fact)
    if not bound:
        return
    for node in walk(root):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
        elif node.type == "new_expression":
            func = node.child_by_field_name("constructor")
        else:
            continue
        if func is None:
            continue
        if func.type == "identifier":
            hit = bound.get(text_of(func))
            if hit and hit[1]:
                hit[0].calls.append(ImportCall(name=hit[1], line=line_of(node)))
        elif func.type == "member_expression":
            obj = func.child_by_field_name("object")
            root_name = text_of(obj).split(".")[0]
            hit = bound.get(root_name)
            if hit:
                prop = text_of(func.child_by_field_name("property"))
                fact, orig = hit
                hit[0].calls.append(ImportCall(
                    name=f"{orig}.{prop}" if orig and orig != "default" else prop,
                    line=line_of(node)))
