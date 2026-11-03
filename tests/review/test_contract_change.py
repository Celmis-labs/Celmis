"""What counts as a breaking change, and what does not."""

from __future__ import annotations

import difflib
from types import SimpleNamespace

import pytest

from src.review.contract_change import (
    compare_definitions,
    compare_params,
    definitions,
    detect,
)
from src.review.diff import parse_unified_diff
from src.review.models import PullRequest

PY_OLD = '''
class Client:
    def pay(self, invoice_id, amount, note=None):
        return 1

    def _private(self, a):
        return 2


def pay_invoice(invoice_id, amount, *, currency="EUR"):
    return invoice_id


def _helper(x):
    return x
'''


def _kinds(old: str, new: str, lang: str = "python", path: str = "pkg/a.py") -> list[tuple[str, str]]:
    return [(c.kind, c.symbol) for c in
            compare_definitions(definitions(lang, path, old), definitions(lang, path, new), {})]


def test_python_public_definitions_skip_private_names() -> None:
    names = {d.name for d in definitions("python", "a.py", PY_OLD)}
    assert names == {"Client", "Client.pay", "pay_invoice"}


def test_a_body_only_change_is_not_a_contract_change() -> None:
    assert _kinds(PY_OLD, PY_OLD.replace("return invoice_id", "return invoice_id + 1")) == []


def test_a_renamed_parameter_breaks_keyword_callers_in_python() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    assert _kinds(PY_OLD, new) == [("param_renamed", "pay_invoice")]


def test_a_removed_parameter() -> None:
    new = PY_OLD.replace("def pay(self, invoice_id, amount, note=None)", "def pay(self, invoice_id, amount)")
    assert _kinds(PY_OLD, new) == [("param_removed", "Client.pay")]


def test_a_newly_required_parameter_and_an_optional_one_that_becomes_required() -> None:
    assert _kinds(PY_OLD, PY_OLD.replace("amount, *,", "amount, tax, *,")) == [
        ("param_required", "pay_invoice")]
    assert _kinds(PY_OLD, PY_OLD.replace("note=None", "note")) == [("param_required", "Client.pay")]
    # an optional addition is compatible
    assert _kinds(PY_OLD, PY_OLD.replace("note=None", "note=None, tag=None")) == []


def test_reordered_positional_parameters() -> None:
    new = PY_OLD.replace("(invoice_id, amount, *,", "(amount, invoice_id, *,")
    assert ("param_reordered", "pay_invoice") in _kinds(PY_OLD, new)


def test_a_removed_public_function_or_class_but_not_a_private_one() -> None:
    new = PY_OLD.replace("def pay_invoice", "def _pay_invoice")
    assert _kinds(PY_OLD, new) == [("symbol_removed", "pay_invoice")]
    assert _kinds(PY_OLD, "") == [("symbol_removed", "Client"), ("symbol_removed", "pay_invoice")]


def test_a_symbol_moved_to_another_changed_file_is_a_move_not_a_removal() -> None:
    old = definitions("python", "a.py", "def f(a):\n    pass\n")
    moved = {d.name: [d] for d in definitions("python", "b.py", "def f(a):\n    pass\n")}
    assert compare_definitions(old, [], moved) == []


def test_typescript_exported_functions_and_arity() -> None:
    old = "export function charge(id: string, amount: number) {}\nfunction local(a) {}\n"
    assert {d.name for d in definitions("typescript", "a.ts", old)} == {"charge"}
    assert _kinds(old, old.replace("amount: number", "amount: number, cur: string"), "typescript", "a.ts") == [
        ("param_required", "charge")]
    assert _kinds(old, old.replace("amount: number", "amount?: number"), "typescript", "a.ts") == []
    assert _kinds(old, old.replace("id: string, amount: number", "id: string"), "typescript", "a.ts") == [
        ("param_removed", "charge")]
    # a rename is invisible to positional callers
    assert _kinds(old, old.replace("amount", "total"), "typescript", "a.ts") == []


def test_go_exported_functions_and_methods_only() -> None:
    old = "package p\nfunc Pay(id string, n int) {}\nfunc (c *Client) Refund(id string) {}\nfunc hidden(a int) {}\n"
    assert {d.name for d in definitions("go", "p.go", old)} == {"Pay", "Client.Refund"}
    assert _kinds(old, old.replace("n int", "n int, cur string"), "go", "p.go") == [("param_required", "Pay")]
    assert _kinds(old, old.replace("func Pay(id string, n int)", "func Pay2(id string, n int)"), "go", "p.go") == [
        ("symbol_removed", "Pay")]


def test_java_public_methods_and_overloads() -> None:
    old = ("public class Pay {\n public void run(String id) {}\n"
           " public void run(String id, int n) {}\n void hidden(int a) {}\n}\n")
    assert {d.name for d in definitions("java", "Pay.java", old)} == {"Pay", "Pay.run"}
    # untouched overloads are not a change
    assert _kinds(old, old + "\n", "java", "Pay.java") == []
    # an overload that is rewritten leaves its old callers without a match
    assert _kinds(old, old.replace("run(String id, int n)", "run(String id, int n, int m)"),
                  "java", "Pay.java") != []


def test_compare_params_is_empty_for_identical_signatures() -> None:
    defs = definitions("python", "a.py", PY_OLD)
    for d in defs:
        assert compare_params(d.params, d.params, "python") == []


# ─── the diff, with and without the files ────────────────────────


def _pr(changes: dict[str, tuple[str | None, str | None]]) -> tuple[PullRequest, dict]:
    diff = ""
    for path, (old, new) in changes.items():
        a = (old or "").splitlines(True)
        b = (new or "").splitlines(True)
        head = f"diff --git a/{path} b/{path}\n"
        if old is None:
            head += "new file mode 100644\n--- /dev/null\n" + f"+++ b/{path}\n"
        elif new is None:
            head += "deleted file mode 100644\n" + f"--- a/{path}\n+++ /dev/null\n"
        else:
            head += f"--- a/{path}\n+++ b/{path}\n"
        diff += head + "".join(list(difflib.unified_diff(a, b, n=3))[2:])
    hunks, _ = parse_unified_diff(diff)
    pr = PullRequest(provider="github", repo="acme/svc", number=1, title="t", description="",
                     author="a", base_ref="main", base_sha="B", head_ref="f", head_sha="H",
                     state="open", hunks=hunks, raw_diff=diff)
    files = {("B", p): o for p, (o, n) in changes.items()} | {("H", p): n for p, (o, n) in changes.items()}
    return pr, files


def _provider(files: dict):  # noqa: ANN202
    return SimpleNamespace(read_file_at=lambda repo, ref, path: files.get((ref, path)))


def test_detect_from_the_files_at_base_and_head() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, files = _pr({"pkg/a.py": (PY_OLD, new)})
    got = detect(pr, _provider(files))
    assert [(c.kind, c.symbol, c.file) for c in got] == [("param_renamed", "pay_invoice", "pkg/a.py")]


def test_detect_from_the_diff_alone() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, _ = _pr({"pkg/a.py": (PY_OLD, new)})
    got = detect(pr, None)
    assert [(c.kind, c.symbol) for c in got] == [("param_renamed", "pay_invoice")]


def test_detect_ignores_body_only_and_test_files() -> None:
    pr, files = _pr({
        "pkg/a.py": (PY_OLD, PY_OLD.replace("return invoice_id", "return 0")),
        "tests/test_a.py": ("def test_x(a):\n    pass\n", "def test_x():\n    pass\n"),
    })
    assert detect(pr, _provider(files)) == []


def test_a_provider_that_cannot_read_falls_back_to_the_diff() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, _ = _pr({"pkg/a.py": (PY_OLD, new)})

    def boom(repo, ref, path):  # noqa: ANN001, ANN202
        raise RuntimeError("403")

    got = detect(pr, SimpleNamespace(read_file_at=boom))
    assert [c.kind for c in got] == ["param_renamed"]


ROUTES_OLD = '''
@app.post("/invoices/{id}/pay")
async def pay(id): ...

@app.get("/invoices/{id}")
async def show(id): ...
'''


@pytest.mark.parametrize("new,expected", [
    (ROUTES_OLD.replace('"/invoices/{id}/pay"', '"/v2/invoices/{id}/settle"'),
     [("route_changed", "POST /invoices/{}/pay")]),
    (ROUTES_OLD.replace("@app.post", "@app.put"), [("route_changed", "POST /invoices/{}/pay")]),
    (ROUTES_OLD.replace('@app.get("/invoices/{id}")\nasync def show(id): ...\n', ""),
     [("route_removed", "GET /invoices/{}")]),
    (ROUTES_OLD.replace("async def pay(id): ...", "async def pay(id):\n    return 1"), []),
    (ROUTES_OLD.replace('"/invoices/{id}/pay"', '"/invoices/{invoice}/pay"'), []),
])
def test_route_changes(new: str, expected: list[tuple[str, str]]) -> None:
    pr, files = _pr({"app/main.py": (ROUTES_OLD, new)})
    got = detect(pr, _provider(files))
    assert [(c.kind, c.symbol) for c in got if c.is_route] == expected


def test_a_deleted_file_removes_its_symbols_and_routes() -> None:
    pr, files = _pr({"app/main.py": (ROUTES_OLD + PY_OLD, None)})
    kinds = {(c.kind, c.symbol) for c in detect(pr, _provider(files))}
    assert ("route_removed", "POST /invoices/{}/pay") in kinds
    assert ("symbol_removed", "pay_invoice") in kinds


def test_a_new_file_breaks_nothing() -> None:
    pr, files = _pr({"app/new.py": (None, ROUTES_OLD + PY_OLD)})
    assert detect(pr, _provider(files)) == []


# ─── review findings: unreadable sides, moves, budget, parameters, routes ───


def test_an_unreadable_side_is_read_from_the_diff_not_taken_for_a_missing_file() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, _ = _pr({"pkg/a.py": (PY_OLD, new)})
    # a fork pull request: the provider answers None (404) for both commits
    got = detect(pr, SimpleNamespace(read_file_at=lambda r, ref, p: None))
    assert [(c.kind, c.symbol) for c in got] == [("param_renamed", "pay_invoice")]
    assert not any(c.kind == "symbol_removed" for c in got)


def test_only_the_old_side_unreadable_does_not_skip_the_file() -> None:
    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, files = _pr({"pkg/a.py": (PY_OLD, new)})
    prov = SimpleNamespace(read_file_at=lambda r, ref, p: None if ref == "B" else files[(ref, p)])
    assert [c.kind for c in detect(pr, prov)] == ["param_renamed"]


def test_a_same_name_function_in_another_changed_file_does_not_hide_a_removal() -> None:
    a_old = "def pay(x):\n    return x\n"
    b_old = "def pay(x):\n    return 2 * x\n"
    pr, files = _pr({"billing/a.py": (a_old, ""), "billing/b.py": (b_old, b_old + "\n# touched\n")})
    got = detect(pr, _provider(files))
    assert ("symbol_removed", "pay") in [(c.kind, c.symbol) for c in got]


def test_a_function_moved_into_a_new_file_next_to_it_is_still_a_move() -> None:
    old = "def pay(x):\n    return x\n"
    pr, files = _pr({"billing/a.py": (old, ""), "billing/b.py": (None, old)})
    assert [c for c in detect(pr, _provider(files)) if c.kind == "symbol_removed"] == []


def test_a_name_gained_in_an_unrelated_tree_is_not_a_move() -> None:
    old = "def pay(x):\n    return x\n"
    pr, files = _pr({"billing/a.py": (old, ""), "other/b.py": (None, old)})
    assert ("symbol_removed", "pay") in [(c.kind, c.symbol) for c in detect(pr, _provider(files))]


def test_the_file_budget_falls_back_to_the_diff_and_says_so() -> None:
    from src.review.contract_change import read_sides

    new = PY_OLD.replace("def pay_invoice(invoice_id,", "def pay_invoice(invoice_ref,")
    pr, files = _pr({"pkg/a.py": (PY_OLD, new), "pkg/b.py": (PY_OLD, new)})
    asked: list[str] = []

    def reader(r, ref, p):  # noqa: ANN001, ANN202
        asked.append(p)
        return files[(ref, p)]

    notes: list[str] = []
    sides = read_sides(pr, SimpleNamespace(read_file_at=reader), notes, budget=-1.0)
    assert asked == [] and all(s.from_diff for s in sides) and len(sides) == 2
    assert notes and "diff" in notes[0]
    detect(pr, SimpleNamespace(read_file_at=reader))  # default budget: reads files
    assert asked


def test_a_parameter_inserted_before_existing_ones_breaks_positional_callers() -> None:
    old = "def f(a, b):\n    pass\n"
    assert _kinds(old, "def f(a, x=0, b=1):\n    pass\n") == [("param_reordered", "f")]
    assert _kinds(old, "def f(a, b, x=0):\n    pass\n") == []


def test_a_positional_parameter_made_keyword_only_breaks() -> None:
    old = "def f(a, b):\n    pass\n"
    assert _kinds(old, "def f(a, *, b):\n    pass\n") == [("param_reordered", "f")]


def test_plain_javascript_destructured_parameters_count_by_position() -> None:
    old = "export function f({a, b}, [c]) {}\n"
    assert _kinds(old, "export function f({a, b}, [c], d) {}\n", "javascript", "a.js") == [
        ("param_required", "f")]
    assert _kinds(old, "export function f({a, b}) {}\n", "javascript", "a.js") == [
        ("param_removed", "f")]
    assert _kinds(old, "export function f({a}, [c]) {}\n", "javascript", "a.js") == []


def test_a_route_moved_to_another_file_is_not_removed() -> None:
    one = '@app.post("/invoices/{id}/pay")\nasync def pay(id): ...\n'
    pr, files = _pr({"app/a.py": (one, ""), "app/b.py": (None, one)})
    assert [c for c in detect(pr, _provider(files)) if c.is_route] == []


def test_a_route_deleted_with_no_mate_is_still_removed() -> None:
    one = '@app.post("/invoices/{id}/pay")\nasync def pay(id): ...\n'
    pr, files = _pr({"app/a.py": (one, ""), "app/b.py": (None, "x = 1\n")})
    assert [c.kind for c in detect(pr, _provider(files)) if c.is_route] == ["route_removed"]
