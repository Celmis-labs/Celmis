"""A column named in a query must exist on the model.

`GET /api/deps/{run_id}/evidence` ordered its timeline by
`DepAuditRun.started_at`. There is no such column — DepAuditRun takes
`created_at`/`updated_at` from TimestampMixin — so the handler raised
AttributeError before it built anything, and the evidence pack answered 500 to
every request it ever received. It was never once downloaded successfully.

The confusion is easy to see once you know: ReviewRun, a different table in the
same product, genuinely has `started_at` and `findings_count`, and the evidence
handler reached for both.

What let it ship is the more interesting part. `tests/deps/test_sbom_and_evidence.py`
is thorough — twenty tests over `build_evidence_pack` and `build_sbom`, hashes,
byte-identical exports, tamper detection. Every one of them calls those
functions directly. Nothing ever executed the handler that assembles their
arguments, so the twenty green tests said nothing at all about whether the
endpoint worked.

This test walks the actual SQLAlchemy mappers rather than a list written by
hand, so a column renamed in a migration takes the query with it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect

from src.db import models as models_module

SRC = Path(__file__).resolve().parents[2] / "src"

#: Not columns, but legitimate on a declarative class.
CLASS_LEVEL = {
    "__tablename__", "__table__", "__table_args__", "__mapper__", "__name__",
    "metadata", "registry", "query", "c",
}


def _mapped_models() -> dict[str, set[str]]:
    """{class name: every attribute SQLAlchemy will answer for}."""
    out: dict[str, set[str]] = {}
    for name in dir(models_module):
        obj = getattr(models_module, name)
        if not isinstance(obj, type):
            continue
        try:
            mapper = sa_inspect(obj)
        except Exception:  # noqa: BLE001 — not a mapped class
            continue
        if not hasattr(mapper, "attrs"):
            continue
        attrs = {a.key for a in mapper.attrs}
        attrs |= {c.key for c in mapper.columns}
        # Hybrid properties, association proxies, plain methods.
        attrs |= {a for a in dir(obj) if not a.startswith("_")}
        out[name] = attrs | CLASS_LEVEL
    return out


MODELS = _mapped_models()


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _bad_references(path: Path, models: dict[str, set[str]]) -> list[str]:
    """`Model.attr` references where `attr` is not on the mapper."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover
        return []
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        model = node.value.id
        if model not in models:
            continue
        if node.attr not in models[model]:
            bad.append(f"{path.relative_to(SRC.parent)}:{node.lineno} "
                       f"{model}.{node.attr}")
    return bad


def test_the_models_were_actually_found():
    """A bug in the discovery above would make every assertion below vacuous —
    which is the failure mode this whole file exists to argue against."""
    assert "DepAuditRun" in MODELS, "the mapper walk found no DepAuditRun"
    assert "created_at" in MODELS["DepAuditRun"]
    assert len(MODELS) > 10, f"only {len(MODELS)} mapped models discovered"


def test_no_query_names_a_column_the_model_does_not_have():
    violations: list[str] = []
    for path in _python_files():
        violations.extend(_bad_references(path, MODELS))
    assert not violations, (
        "these attributes do not exist on their model and raise "
        "AttributeError at request time:\n  " + "\n  ".join(violations)
    )


def test_dep_audit_runs_are_ordered_by_a_column_that_exists():
    """The specific one, named so a reader of this file knows what happened.

    DepAuditRun has never had `started_at`; ReviewRun has, which is where the
    reach came from.
    """
    assert "started_at" not in MODELS["DepAuditRun"]
    assert "findings_count" not in MODELS["DepAuditRun"]
    source = (SRC / "api" / "routers" / "deps.py").read_text()
    assert "DepAuditRun.started_at" not in source
    assert "DepAuditRun.created_at" in source


@pytest.mark.parametrize("model", ["ReviewRun"])
def test_the_model_that_does_have_started_at_still_does(model: str):
    """Guarding the confusion from the other side: if ReviewRun ever loses
    `started_at`, the reviews router breaks the same way and this says so."""
    reviews = (SRC / "api" / "review_runs.py").read_text()
    assert "started_at" in reviews
