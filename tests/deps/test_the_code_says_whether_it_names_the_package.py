"""Which findings your own code actually mentions, and which arrived silently.

NOT reachability. Reachability needs the dependency's source in the index
(excluded on purpose), advisories that name the vulnerable symbol (OSV carries
those for a small minority) and a notion of where execution starts (there is
none). This is an import-position search, and the tests below hold it to
exactly that claim — including the third answer, which is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.deps.imports import (
    IMPORTED,
    NOT_FOUND,
    UNKNOWN,
    module_candidates,
    scan_imports,
)


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ─── the ecosystems whose rule is exact ──────────────────────────────


def test_npm_require_and_esm_are_both_found(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.js", "const _ = require('lodash');\n")
    _write(tmp_path, "src/b.ts", "import merge from \"lodash/merge\";\n")
    answers = scan_imports(tmp_path, [("npm", "lodash")])
    answer = answers[("npm", "lodash")]
    assert answer.state == IMPORTED
    assert {s.path for s in answer.sites} == {"src/a.js", "src/b.ts"}
    assert all(s.line >= 1 for s in answer.sites)


def test_a_scoped_package_is_matched_whole(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.ts", "import type { X } from '@acme/widgets';\n")
    answers = scan_imports(tmp_path, [("npm", "@acme/widgets")])
    assert answers[("npm", "@acme/widgets")].state == IMPORTED


def test_npm_absence_is_a_real_answer(tmp_path: Path) -> None:
    """The specifier IS the package name, so absence means something here."""
    _write(tmp_path, "src/a.js", "const _ = require('underscore');\n")
    answers = scan_imports(tmp_path, [("npm", "lodash")])
    answer = answers[("npm", "lodash")]
    assert answer.state == NOT_FOUND
    assert "dynamic import" in answer.detail


def test_a_package_named_in_a_comment_is_not_an_import(tmp_path: Path) -> None:
    """Import POSITION, not the string appearing somewhere in the file."""
    _write(tmp_path, "src/a.js", "// we should stop using lodash one day\n")
    assert scan_imports(tmp_path, [("npm", "lodash")])[("npm", "lodash")].state == NOT_FOUND


def test_go_uses_the_module_path(tmp_path: Path) -> None:
    _write(tmp_path, "main.go", 'import (\n\t"github.com/pkg/errors"\n)\n')
    answers = scan_imports(tmp_path, [("Go", "github.com/pkg/errors")])
    assert answers[("Go", "github.com/pkg/errors")].state == IMPORTED


def test_a_rust_crate_is_matched_with_underscores(tmp_path: Path) -> None:
    """`serde-json` on crates.io is `serde_json` in source — a mechanical rule."""
    _write(tmp_path, "src/lib.rs", "use serde_json::Value;\n")
    answers = scan_imports(tmp_path, [("crates.io", "serde-json")])
    assert answers[("crates.io", "serde-json")].state == IMPORTED


# ─── the ecosystem whose rule is not exact ───────────────────────────


def test_a_python_package_that_is_imported_is_found(tmp_path: Path) -> None:
    _write(tmp_path, "app/main.py", "import requests\nfrom requests import Session\n")
    answers = scan_imports(tmp_path, [("PyPI", "requests")])
    assert answers[("PyPI", "requests")].state == IMPORTED


def test_an_absent_python_package_is_unknown_not_absent(tmp_path: Path) -> None:
    """`beautifulsoup4` imports as `bs4`. Absence of the name proves nothing.

    Reporting it as "not imported" would be a silent zero — the exact failure
    this subsystem exists against — so the answer is that we cannot tell.
    """
    _write(tmp_path, "app/main.py", "import bs4\n")
    answers = scan_imports(tmp_path, [("PyPI", "beautifulsoup4")])
    answer = answers[("PyPI", "beautifulsoup4")]
    assert answer.state == UNKNOWN
    assert "does not determine its module name" in answer.detail


def test_the_exactness_of_each_rule_is_declared() -> None:
    for ecosystem, package, exact in (
        ("npm", "lodash", True),
        ("Go", "github.com/pkg/errors", True),
        ("crates.io", "serde-json", True),
        ("PyPI", "beautifulsoup4", False),
    ):
        _, is_exact = module_candidates(ecosystem, package)
        assert is_exact is exact, ecosystem


def test_an_unsupported_ecosystem_says_so_rather_than_no(tmp_path: Path) -> None:
    answers = scan_imports(tmp_path, [("Hackage", "aeson")])
    answer = answers[("Hackage", "aeson")]
    assert answer.state == UNKNOWN
    assert "Hackage" in answer.detail


# ─── the walk is bounded, and says when it was ───────────────────────


def test_the_file_cap_reports_itself(tmp_path: Path, monkeypatch) -> None:
    from src.deps import imports as mod

    monkeypatch.setattr(mod, "MAX_FILES_PER_REPO", 3)
    for i in range(6):
        _write(tmp_path, f"src/f{i}.js", "const x = 1;\n")
    notes: list[dict] = []
    mod.scan_imports(tmp_path, [("npm", "lodash")], notes)
    assert notes, "the scan stopped early and said nothing"
    assert "partial" in notes[0]["detail"]


def test_matches_per_package_are_capped(tmp_path: Path) -> None:
    from src.deps.imports import MAX_MATCHES_PER_PACKAGE

    for i in range(MAX_MATCHES_PER_PACKAGE + 4):
        _write(tmp_path, f"src/f{i}.js", "const _ = require('lodash');\n")
    answer = scan_imports(tmp_path, [("npm", "lodash")])[("npm", "lodash")]
    assert answer.state == IMPORTED
    assert len(answer.sites) == MAX_MATCHES_PER_PACKAGE


def test_dependencies_of_dependencies_are_not_searched(tmp_path: Path) -> None:
    """node_modules is somebody else's code; a hit in there says nothing."""
    _write(tmp_path, "node_modules/x/index.js", "require('lodash');\n")
    assert scan_imports(tmp_path, [("npm", "lodash")])[("npm", "lodash")].state == NOT_FOUND


def test_the_answer_is_json(tmp_path: Path) -> None:
    """It travels on a finding row, which is stored as JSON."""
    import json

    _write(tmp_path, "a.js", "require('lodash')\n")
    answer = scan_imports(tmp_path, [("npm", "lodash")])[("npm", "lodash")]
    payload = json.loads(json.dumps(answer.as_dict()))
    assert payload["state"] == IMPORTED
    assert payload["sites"][0]["path"] == "a.js"


@pytest.mark.parametrize("state", [IMPORTED, NOT_FOUND, UNKNOWN])
def test_there_are_three_states_and_they_are_distinct(state: str) -> None:
    """Two would force "cannot tell" into one of the other two, and both are lies."""
    assert len({IMPORTED, NOT_FOUND, UNKNOWN}) == 3
    assert isinstance(state, str)


# ─── the path from the scan to the row and the API ───────────────────


def test_the_column_and_the_model_agree() -> None:
    """A model column with no migration behind it is an UndefinedColumn on
    the first SELECT — which is what tests/db/test_migration_chain.py exists
    for; this only checks the name is the one this feature writes."""
    from src.db.models import DepFinding

    assert "named_in_code" in DepFinding.__table__.columns


def test_the_auditor_writes_it_onto_every_row() -> None:
    """Read with ast: the comment above the assignment names the field, so a
    substring search would pass with the line deleted."""
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "src" / "deps" / "auditor.py").read_text("utf-8"))
    written = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert "named_in_code" in written, (
        "no finding row is given an import answer, so the column stays null "
        "and the feature is invisible"
    )


def test_the_api_hands_it_to_the_reader() -> None:
    from src.api.routers.deps import FindingOut

    assert "named_in_code" in FindingOut.model_fields
    assert FindingOut.model_fields["named_in_code"].default is None, (
        "it must default to None — a row from before the scan has no answer, "
        "and that is not the same as 'not imported'"
    )


def test_nothing_in_the_feature_is_NAMED_reachability() -> None:
    """The name is the claim, and only names are checked.

    An import-position search is not reachability. The prose in these files
    says so repeatedly, so a line scan trips on the explanation — the first
    version of this test did exactly that. What must not happen is a
    FUNCTION, VARIABLE, ARGUMENT or COLUMN called `reachable`: a column name
    reaches an API response, a CSV export and eventually a filing, where
    nobody reads the docstring that qualified it.
    """
    import ast
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[2]
    for rel in ("src/deps/imports.py", "src/api/routers/deps.py", "src/db/models.py"):
        tree = ast.parse((root / rel).read_text("utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        offenders = sorted(n for n in names if "reachab" in n.lower())
        assert not offenders, (
            f"{rel} names something reachability: {offenders}. This reports "
            f"import positions, not whether a vulnerable function is called."
        )
