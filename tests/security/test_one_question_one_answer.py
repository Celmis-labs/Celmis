"""Two independent answers to one question is how they start to disagree.

Not a style rule. Every tenant-isolation bug fixed in this codebase in the
last two days had the same shape: a rule enforced in one place and derived
again, differently, somewhere else. The job queue filtered by workspace
while the audit log did not. The vector reads were scoped while purge was
not. `_is_single_tenant` counted the workspaces table while `deployment.py`
read a declared mode — and pointing the first at the second, without also
keeping the count, said "safe to wipe everyone's vectors" on an
installation with three tenants.

This file pins the questions that now have exactly one authoritative answer,
and fails when a second appears. It cannot catch every duplication; it
catches the ones that have already cost something.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _defs(name: str) -> list[str]:
    """Every module defining a function of this name."""
    out = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name):
                out.append(str(path.relative_to(ROOT)))
    return sorted(set(out))


# ─── "whose data is this" ────────────────────────────────────────────


def test_one_rule_for_what_counts_as_a_tenant():
    """`normalize_workspace_id` decides that "default" and "" are NOT an
    attribution. Every reader and writer must ask the same function, or one
    of them hands somebody else's rows to whoever owns the seeded workspace.
    """
    assert _defs("normalize_workspace_id") == ["src/security/audit.py"], (
        "a second definition of what a tenant is: "
        f"{_defs('normalize_workspace_id')}"
    )


def test_the_vector_scope_is_built_in_one_place():
    """Every vector read splices the same condition. A second builder is a
    second chance to forget the filter."""
    assert _defs("must_conditions") == ["src/retrieval/vector_store.py"]


# ─── "is this installation one tenant" ───────────────────────────────


def test_the_deployment_mode_has_one_source():
    assert _defs("get_mode") == ["src/deployment.py"]
    assert _defs("is_single_tenant") == ["src/deployment.py"], (
        "somebody derived the mode again instead of asking for it"
    )


def test_the_wipe_guard_asks_both_the_declaration_and_the_count():
    """The regression this file exists for. The declared mode says what the
    operator INTENDS; the count says what is actually in there. A guard that
    trusts only the declaration says "safe" on an installation that has
    three tenants and simply never declared otherwise."""
    src = (ROOT / "src" / "api" / "routers" / "llm.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_is_single_tenant")
    body = ast.dump(fn)
    assert "is_single_tenant" in body, "it derives the mode again"
    assert "count_workspaces" in body, (
        "it trusts the declaration alone — an undeclared multi-tenant "
        "installation would be told it is safe to wipe"
    )


# ─── "is this secret real" ───────────────────────────────────────────


def test_one_rule_for_a_usable_secret():
    """The web surface refused a shipped placeholder and MCP accepted it,
    which is a refusal that tells you it is safe."""
    assert _defs("secret_problem") == ["src/api/jwt_auth.py"]

    mcp = (ROOT / "src" / "mcp_server" / "auth.py").read_text(encoding="utf-8")
    assert "secret_problem" in mcp, (
        "the MCP surface no longer applies the same bar as the web one"
    )


# ─── the shipped placeholders ────────────────────────────────────────


def test_no_compose_service_falls_back_to_a_placeholder_secret():
    """A fallback value means the stack comes up on a secret printed in a
    public file. `${VAR:?...}` stops it at config time, naming the variable."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for line in compose.splitlines():
        if line.strip().startswith("#") or "SECRET" not in line:
            continue
        # `${X:-}` is fine: an OPTIONAL secret defaulting to empty, which the
        # code then treats as "not configured". What must not appear is a
        # fallback to a literal — that is a stack coming up on a value
        # printed in a public file.
        for fallback in re.findall(r":-([^}]*)", line):
            fallback = fallback.strip()
            if not fallback:
                continue
            assert fallback.startswith("${") and ":?" in fallback, (
                f"a secret falls back to a literal: {line.strip()}"
            )


@pytest.mark.parametrize("name", [
    "CELMIS_DEPLOYMENT_MODE", "CELMIS_PUBLIC_API_DOCS",
    "EMBEDDING_TASK_TYPE_ENABLED", "CREDENTIAL_MASTER_KEY",
])
def test_the_example_env_documents_what_the_deploy_actually_sets(name):
    """A setting nobody can find is a setting nobody sets."""
    body = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(rf"^{name}=", body, re.M), f"{name} is undocumented"


def test_the_example_does_not_ask_for_a_provider_key():
    """Provider keys are per-workspace and entered in the interface — two
    workspaces on one installation may bill two different accounts. Asking
    for one in the environment teaches the opposite."""
    body = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^GEMINI_API_KEY=", body, re.M)
    assert "LLM Setup" in body, "nothing says where keys actually go"
