"""`codebaseApi` calls exactly the routes of the Codebase API: no route is
missing from the client (a button with nothing behind it), and no call goes to
a route that is not served (a button that always fails).

Two sources of truth are held against the client:

* the route table the feature was specified with, written out below, which
  works before the router is importable and pins the shape afterwards;
* the router itself (`src.api.routers.codebase`), compared by method and by
  path with every `{parameter}` flattened, so renaming `{pack_id}` to `{id}`
  is not a failure and moving a route is.

The client's calls are read off the source — a method from `method: "X"`
(a call without one is a GET), a path from the template literal with every
`${…}` flattened to `{}` and the query string dropped — so a route added to
the client without a line here fails, and so does one removed from it.
"""

from __future__ import annotations

import importlib
import importlib.util
import re

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    WEB,
    _strip_comments,
)

API = WEB / "lib" / "api.ts"
PREFIX = "/api/codebase"

#: The specified table (METHOD, path with `{}` for every parameter).
SPECIFIED = {
    ("GET", "/packs"),
    ("POST", "/packs"),
    ("GET", "/packs/{}"),
    ("PATCH", "/packs/{}"),
    ("DELETE", "/packs/{}"),
    ("PUT", "/packs/{}/teams"),
    ("POST", "/packs/{}/members"),
    ("DELETE", "/packs/{}/members/{}"),
    ("GET", "/packs/{}/versions"),
    ("POST", "/packs/{}/versions"),
    ("POST", "/packs/{}/versions/{}/activate"),
    ("GET", "/packs/{}/versions/{}/diff"),
    ("GET", "/packs/{}/versions/{}/archive"),
    ("GET", "/packs/{}/items/{}"),
    ("GET", "/packs/{}/invites"),
    ("POST", "/packs/{}/invites"),
    ("DELETE", "/packs/{}/invites/{}"),
    ("GET", "/packs/{}/tokens"),
    ("POST", "/packs/{}/tokens"),
    ("DELETE", "/packs/{}/tokens/{}"),
    ("GET", "/packs/{}/tokens"),
    ("POST", "/packs/{}/tokens"),
    ("DELETE", "/packs/{}/tokens/{}"),
    ("POST", "/invites/preview"),
    ("POST", "/invites/redeem"),
    ("GET", "/search"),
    ("POST", "/brief"),
}

#: Served, and called by `celmis-pack` with a pack publish token — never by the
#: web app, which has no such token (it manages them under /packs/{}/tokens).
PUBLISH = {
    ("GET", "/publish"),
    ("POST", "/publish"),
    ("GET", "/publish/archive"),
}


def _flatten(expr: str) -> str:
    """A template literal's path with each `${…}` (braces balanced) as `{}`,
    `packPath(x)` as the pack prefix, and the query string dropped."""
    out, i = [], 0
    while i < len(expr):
        if expr.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(expr) and depth:
                depth += {"{": 1, "}": -1}.get(expr[j], 0)
                j += 1
            inner = expr[i + 2:j - 1].strip()
            if inner.startswith("packPath("):
                out.append(f"{PREFIX}/packs/{{}}")
            elif inner.startswith("queryString(") or inner == "API_BASE":
                pass
            else:
                out.append("{}")
            i = j
        else:
            out.append(expr[i])
            i += 1
    return "".join(out).split("?")[0]


def _client_calls() -> set[tuple[str, str]]:
    code = _strip_comments(API.read_text(encoding="utf-8"))
    start = code.index("export const codebaseApi = {")
    body = code[start:code.index("\n};", start)]
    upload = code[code.index("export function uploadPackVersion("):]
    upload = upload[:upload.index("\n}\n")]

    calls: set[tuple[str, str]] = set()
    for entry in re.split(r"\n(?=  \w+: )", body)[1:]:
        text = entry
        if re.match(r"\s*upload: uploadPackVersion", entry):
            # The one call made with XMLHttpRequest, for the progress events.
            m = re.search(r'xhr\.open\("(\w+)",\s*(`[^`]*`)', upload)
            assert m, "the upload no longer goes through xhr.open"
            calls.add((m.group(1), _flatten(m.group(2)[1:-1]).removeprefix(PREFIX)))
            continue
        m = re.search(
            r'(?:api<[^>]*>|downloadWithAuth)\(\s*(`[^`]*`|"[^"]*"|packPath\([^)]*\))', text)
        assert m, f"no request in: {entry.strip()[:80]}"
        arg = m.group(1)
        path = _flatten(arg[1:-1] if arg[0] in "`\"" else "${" + arg + "}")
        assert path.startswith(PREFIX), path
        method = re.search(r'method: "(\w+)"', text)
        calls.add((method.group(1) if method else "GET", path.removeprefix(PREFIX)))
    return calls


def test_the_client_calls_exactly_the_specified_routes():
    calls = _client_calls()
    assert calls - SPECIFIED == set(), f"calls to routes nobody specified: {sorted(calls - SPECIFIED)}"
    assert SPECIFIED - calls == set(), f"routes with no client call: {sorted(SPECIFIED - calls)}"


def test_the_client_helper_reads_what_it_should():
    """Guards the guard: a parser that finds nothing would pass the test above
    only if the table were empty, but one that mangles a path would not say so."""
    assert _flatten("${API_BASE}${packPath(id)}/versions/${number}/archive") == (
        "/api/codebase/packs/{}/versions/{}/archive")
    assert _flatten("${packPath(id)}/versions/${number}/diff${queryString({ against })}") == (
        "/api/codebase/packs/{}/versions/{}/diff")
    assert _flatten("/api/codebase/search${queryString({ q, limit })}") == "/api/codebase/search"
    assert len(_client_calls()) == len(SPECIFIED)


@pytest.mark.skipif(
    importlib.util.find_spec("src.api.routers.codebase") is None,
    reason="the Codebase router is not written yet",
)
def test_the_client_calls_exactly_the_routes_the_router_serves():
    router = importlib.import_module("src.api.routers.codebase").router
    served: set[tuple[str, str]] = set()
    for route in router.routes:
        path = route.path if route.path.startswith(PREFIX) else PREFIX + route.path
        flat = re.sub(r"\{[^}]+\}", "{}", path).removeprefix(PREFIX)
        for method in route.methods or ():
            if method not in {"HEAD", "OPTIONS"}:
                served.add((method, flat))
    calls = _client_calls()
    assert calls - served == set(), f"client calls the router does not serve: {sorted(calls - served)}"
    assert served - calls == PUBLISH, (
        f"routes the client never calls: {sorted(served - calls - PUBLISH)}; "
        f"publish routes gone: {sorted(PUBLISH - served)}")
