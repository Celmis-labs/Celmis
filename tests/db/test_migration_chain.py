"""The migration chain has to survive `alembic upgrade head` on a live DB.

The api container runs `alembic upgrade head && uvicorn`. A migration that
raises does not degrade the deploy — it takes the whole API down, because the
`&&` never reaches uvicorn and the healthcheck then fails the `web` container
too. That is exactly how production went to 502 once: a second migration added
`agent_sessions.repo_slugs`, a column an earlier revision had already created,
and Postgres answered DuplicateColumn.

Neither failure is visible by reading one migration — you have to hold the
whole chain at once, which is what these tests do. They parse the files rather
than execute them, so they need no database and run in the normal unit suite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _revisions() -> dict[str, tuple[str | None, str]]:
    """{revision: (down_revision, filename)} for every migration on disk."""
    out: dict[str, tuple[str | None, str]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text()
        rev = re.search(r'^revision(?::[^=]+)?\s*=\s*["\']([^"\']+)', text, re.M)
        down = re.search(
            r'^down_revision(?::[^=]+)?\s*=\s*(?:None|["\']([^"\']+))', text, re.M
        )
        if rev:
            out[rev.group(1)] = (down.group(1) if down else None, path.name)
    return out


def _string_arg(node: ast.Call, index: int) -> str | None:
    if len(node.args) <= index:
        return None
    arg = node.args[index]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _added_columns() -> list[tuple[str, str, str]]:
    """(table, column, filename) for every literal op.add_column in the chain."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_column"):
                continue
            table = _string_arg(node, 0)
            column = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Call):
                column = _string_arg(node.args[1], 0)
            # Anything built dynamically is skipped rather than guessed at:
            # a false failure here would block every unrelated migration.
            if table and column:
                found.append((table, column, path.name))
    return found


def test_chain_has_exactly_one_head() -> None:
    """Two heads make `alembic upgrade head` refuse to run at all."""
    revisions = _revisions()
    parents = {down for down, _ in revisions.values() if down}
    heads = sorted(rev for rev in revisions if rev not in parents)
    assert len(heads) == 1, (
        "expected one head, found "
        + ", ".join(f"{h} ({revisions[h][1]})" for h in heads)
    )


def test_every_down_revision_resolves() -> None:
    revisions = _revisions()
    dangling = [
        (name, down)
        for down, name in revisions.values()
        if down and down not in revisions
    ]
    assert not dangling, f"down_revision points at a missing revision: {dangling}"


def test_no_column_is_added_twice() -> None:
    """The DuplicateColumn that took production down.

    A column added by two revisions is fine until the second one reaches a
    database where the first already ran — i.e. always, on the next deploy.
    """
    seen: dict[tuple[str, str], str] = {}
    clashes: list[str] = []
    for table, column, filename in _added_columns():
        key = (table, column)
        if key in seen:
            clashes.append(f"{table}.{column}: {seen[key]} and {filename}")
        else:
            seen[key] = filename
    assert not clashes, "the same column is added by two revisions:\n" + "\n".join(clashes)


@pytest.mark.parametrize("column", ["repo_slugs", "project_id"])
def test_agent_session_multi_repo_columns_exist_once(column: str) -> None:
    """Pins the specific pair that collided, since both are still in use."""
    owners = [f for t, c, f in _added_columns() if t == "agent_sessions" and c == column]
    assert len(owners) == 1, f"agent_sessions.{column} added by {owners}"


# ─── Drift: a model column the chain never creates ──────────────────
#
# GET /api/alerts answered 500 on every request for days. The commit that
# taught the product to answer in the language of the question added
# `language` to TWO models — automation_runs got migration e7b204c9f138,
# incoming_alerts got nothing — so SQLAlchemy SELECTed a column Postgres did
# not have. Every test passed: the suite builds its schema from the models
# themselves, so the model and the schema agreed by construction and the one
# place they could disagree, the migration chain, was never consulted.


def _created_columns() -> dict[str, set[str]]:
    """{table: columns} the chain creates, via create_table + add_column."""
    tables: dict[str, set[str]] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        # `upgrade()` ONLY. Walking the module reads downgrade() too, and every
        # well-written migration undoes there exactly what it did here — so the
        # add and the drop cancelled out and the chain looked like it created
        # nothing at all.
        upgrades = [
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
        ]
        # `for tbl in ("projects", "teams", …): op.add_column(tbl, …)` is a
        # real and common shape here (c9d8e1a5f612 adds workspace_id to six
        # tables that way). Bind the loop variable to each literal so those
        # columns are seen; a loop over anything non-literal is left alone
        # rather than guessed at.
        loop_bindings: dict[str, tuple[str, ...]] = {}
        for fn in upgrades:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name)
                    and isinstance(node.iter, ast.Tuple | ast.List)
                ):
                    names = tuple(
                        e.value for e in node.iter.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    )
                    if names:
                        loop_bindings[node.target.id] = names

        def _names_at(
            call: ast.Call,
            index: int,
            # Bound here, at definition time, rather than read from the
            # enclosing scope: `loop_bindings` is rebuilt for every migration
            # file, so a closure that looked it up late would answer with
            # whichever file the outer loop had reached by then. Right today
            # only because nothing calls these helpers after the iteration
            # that made them — which is exactly the accident ruff's B023 is
            # pointing at, and the ratchet is a zero.
            bindings: dict[str, tuple[str, ...]] = loop_bindings,
        ) -> tuple[str, ...]:
            """The string(s) an argument stands for — a literal, or every value
            a loop variable takes. Both shapes are real here: c9d8e1a5f612
            loops over TABLE names, c6e2f7041a2a over COLUMN names."""
            if len(call.args) <= index:
                return ()
            arg = call.args[index]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return (arg.value,)
            if isinstance(arg, ast.Name):
                return bindings.get(arg.id, ())
            return ()

        def _tables_for(call: ast.Call) -> tuple[str, ...]:
            return _names_at(call, 0)

        for node in (m for fn in upgrades for m in ast.walk(fn)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "create_table":
                for table in _tables_for(node):
                    cols = tables.setdefault(table, set())
                    for arg in node.args[1:]:
                        if isinstance(arg, ast.Call):
                            name = _string_arg(arg, 0)
                            if name:
                                cols.add(name)
            elif func.attr == "add_column":
                if len(node.args) > 1 and isinstance(node.args[1], ast.Call):
                    for name in _names_at(node.args[1], 0):
                        for table in _tables_for(node):
                            tables.setdefault(table, set()).add(name)
            elif func.attr == "drop_column":
                for name in _names_at(node, 1):
                    for table in _tables_for(node):
                        tables.setdefault(table, set()).discard(name)
    return tables


def test_every_model_column_exists_in_the_migration_chain() -> None:
    """The suite builds its schema from the models, so only this comparison
    can catch a column that exists in Python and not in Postgres."""
    from src.db.models import Base

    chain = _created_columns()
    missing: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in chain:
            # A table the chain never creates is a different (louder) bug and
            # is not what this test is for — flag it rather than crash on it.
            missing.append(f"{table_name}: the chain never creates this table")
            continue
        for column in table.columns:
            if column.name not in chain[table_name]:
                missing.append(f"{table_name}.{column.name}")

    assert not missing, (
        "these model columns have no migration — SQLAlchemy will SELECT them "
        "and Postgres will answer UndefinedColumn:\n  " + "\n  ".join(missing)
    )
