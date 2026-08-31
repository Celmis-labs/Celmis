"""The README said eighteen tools. The mount serves twenty-three.

Eighteen was not invented: it is the number of read-only tools, and a client
holding only read scopes sees exactly that many. But the README presented it as
what the mount serves, which understated the product by five and described a
subset as the whole. Nobody noticed because nothing counted.

What this pins is the pair, not one number. A tool added without a scope entry
is visible to every caller including one that should not see it — line 297 of
http_app.py falls open for names the map does not know — so the two counts
drifting apart is a security fact before it is a documentation one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTTP_APP = ROOT / "src" / "mcp_server" / "http_app.py"
README = ROOT / "README.md"

WORDS = {
    18: "eighteen", 19: "nineteen", 20: "twenty", 21: "twenty-one",
    22: "twenty-two", 23: "twenty-three", 24: "twenty-four",
    25: "twenty-five", 26: "twenty-six", 27: "twenty-seven",
}


def _module() -> ast.Module:
    return ast.parse(HTTP_APP.read_text(encoding="utf-8"))


def _registered_tools() -> set[str]:
    """Every @mcp.tool(name=...) registration, read from the syntax tree.

    Deliberately not a grep: `name="..."` occurs in this file inside tool
    descriptions and error strings, and a count that includes those is a count
    that is wrong in the direction nobody checks.
    """
    names: set[str] = set()
    for node in ast.walk(_module()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not (isinstance(func, ast.Attribute) and func.attr == "tool"):
                continue
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
    return names


def _scope_map() -> dict[str, str]:
    for node in ast.walk(_module()):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_TOOL_SCOPES":
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_TOOL_SCOPES":
                    return ast.literal_eval(node.value)
    raise AssertionError("_TOOL_SCOPES is gone — the scope filter falls open without it")


def test_every_registered_tool_has_a_scope():
    """The filter at http_app.py:297 keeps tools whose name is NOT in the map.

    That default is deliberate for forward compatibility, and it means a tool
    added without a scope entry is handed to every caller. This is the test
    that turns that from a silent grant into a failing build.
    """
    registered, scoped = _registered_tools(), set(_scope_map())
    unscoped = registered - scoped
    assert not unscoped, (
        f"registered without a scope, therefore visible to every token: "
        f"{sorted(unscoped)}"
    )
    stale = scoped - registered
    assert not stale, f"scoped but not registered: {sorted(stale)}"


def test_the_readme_states_the_counts_the_code_produces():
    scopes = _scope_map()
    total = len(scopes)
    read_only = sum(1 for s in scopes.values() if s.startswith("read"))
    body = README.read_text(encoding="utf-8").lower()

    for count, label in ((total, "tools the mount serves"),
                         (read_only, "read-only tools")):
        forms = [str(count)]
        if count in WORDS:
            forms.append(WORDS[count])
        assert any(f in body for f in forms), (
            f"the README does not state the number of {label} ({count}). "
            f"Looked for {forms}. A count in prose that nothing checks is the "
            f"reason this file exists"
        )

    # The specific stale claim, so a rewrite cannot reintroduce it while still
    # mentioning the right numbers somewhere else on the page.
    for stale in ("serves **18 tools**", "eighteen tools over mcp",
                  "eighteen tools, served over"):
        assert stale not in body, (
            f"the README still says {stale!r}, which describes the read-only "
            f"subset as though it were the whole mount"
        )


def test_the_write_tools_are_named_so_a_reader_knows_what_is_hidden():
    """A client that sees eighteen tools should be able to find out what the
    other five are without reading the source. Naming them is the difference
    between a scoped surface and one that just looks incomplete."""
    write_tools = {n for n, s in _scope_map().items() if s.startswith("write")}
    body = README.read_text(encoding="utf-8")
    missing = [t for t in write_tools if f"`{t}`" not in body]
    assert not missing, f"write tools the README never names: {sorted(missing)}"
