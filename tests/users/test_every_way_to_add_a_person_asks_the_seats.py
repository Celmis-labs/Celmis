"""Every way of adding a person goes through the seats, or says why not.

The contract (src/users/seats.py): the seat limit is enforced in the FLOWS —
the sign-up, the Google sign-in, the single sign-on exchange, the command
line, the admin's "switch on" button — by calling `seats.create_person` or
`seats.reactivate_person` (or, for single sign-on, the same two names on
`src/ee/sso/allowance.py`, which also asks the single sign-on allowance)
instead of writing to the user store directly. That
is a convention, and a convention is only as good as the next person adding a
way in. So this test reads the source:

  * any function in `src/` that builds a `User(...)`, calls `.create(...)` on a
    user store, switches an account on (`is_active = True`) or binds one to
    single sign-on (`oidc_sub = ...`) must call one of those three functions;
  * the few that legitimately do not are listed below with the reason, and a
    listed function that no longer does what made it a candidate fails the
    test, so the list cannot rot.

A new sign-in route that forgets the seats fails here, with its name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

#: module name used at the call site → the calls that ask the seats.
SEAT_CALLS = {"create_person", "bind", "reactivate_person"}
SEAT_MODULES = {"seats", "allowance"}
USER_STORES = {"users", "store", "user_store", "_users"}

#: (file relative to src/, function) → why it takes no seat.
EXEMPT: dict[tuple[str, str], str] = {
    ("api/routers/auth.py", "_master_login"):
        "the master (break-glass) account is the platform's, never a seat",
    ("users/store.py", "ensure_default_user"):
        "seeds the local `default` user, a platform account",
    ("users/store.py", "_row_to_user"):
        "turns a stored row back into a User; adds nobody",
    ("users/seats.py", "create_person"): "the enforcement itself",
    ("users/seats.py", "reactivate_person"): "the enforcement itself",
    ("ee/sso/allowance.py", "bind"):
        "the enforcement itself (binds an existing account under the seat lock)",
}


def _is_user_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "User")


def _is_store_create(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in USER_STORES)


def _switches_on(node: ast.AST) -> bool:
    """`x.is_active = True`, `User(..., is_active=True)`, or binding an account
    to single sign-on (`x.oidc_sub = ...`, which takes an SSO seat)."""
    def true(v: ast.AST) -> bool:
        return isinstance(v, ast.Constant) and v.value is True

    if isinstance(node, ast.Assign) and true(node.value):
        return any(isinstance(t, ast.Attribute) and t.attr == "is_active"
                   for t in node.targets)
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Attribute) and t.attr in {"oidc_sub", "oidc_iss"}
                   for t in node.targets) or (
            isinstance(node.targets[0], ast.Tuple) and any(
                isinstance(t, ast.Attribute) and t.attr in {"oidc_sub", "oidc_iss"}
                for t in node.targets[0].elts))
    if _is_user_call(node):
        return any(k.arg == "is_active" and true(k.value) for k in node.keywords)
    return False


def _is_seat_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in SEAT_CALLS
            and isinstance(node.func.value, ast.Name) and node.func.value.id in SEAT_MODULES)


class _Scan(ast.NodeVisitor):
    """For each function: does it add a person, does it ask the seats."""

    def __init__(self) -> None:
        self.stack: list[str] = ["<module>"]
        self.adds: dict[str, int] = {}
        self.asks: set[str] = set()

    def _enter(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit(self, node: ast.AST) -> None:
        here = self.stack[-1]
        if _is_user_call(node) or _is_store_create(node) or _switches_on(node):
            self.adds.setdefault(here, getattr(node, "lineno", 0))
        if _is_seat_call(node):
            self.asks.add(here)
        super().visit(node)


def _scan_all() -> dict[tuple[str, str], tuple[int, bool]]:
    found: dict[tuple[str, str], tuple[int, bool]] = {}
    for path in sorted(SRC.rglob("*.py")):
        scan = _Scan()
        scan.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        rel = path.relative_to(SRC).as_posix()
        for func, line in scan.adds.items():
            found[(rel, func)] = (line, func in scan.asks)
    return found


FOUND = _scan_all()


def test_the_scan_finds_the_flows_that_add_people():
    """A scan that finds nothing proves nothing: these are known to exist."""
    for key in [("api/routers/auth.py", "signup"), ("api/routers/auth.py", "google_callback"),
                ("ee/sso/router.py", "oidc_callback"), ("cli.py", "auth_google_cmd"),
                ("api/routers/auth.py", "_master_login")]:
        assert key in FOUND, f"the scan no longer sees {key}; has the code moved?"


@pytest.mark.parametrize(
    "key", sorted(k for k in FOUND if k not in EXEMPT), ids=lambda k: f"{k[0]}::{k[1]}")
def test_a_function_that_adds_a_person_asks_the_seats(key):
    line, asks = FOUND[key]
    assert asks, (
        f"{key[0]}::{key[1]} (line {line}) adds or switches on an account but does not call "
        "seats.create_person / seats.reactivate_person / allowance.create_person / allowance.bind. "
        "Call one, or, if it "
        "really is not a person (a platform account), list it in EXEMPT with the reason.")


@pytest.mark.parametrize("key", sorted(EXEMPT), ids=lambda k: f"{k[0]}::{k[1]}")
def test_an_exemption_is_still_needed(key):
    """An entry for a function that no longer adds anybody is a hole waiting
    for the next one that does."""
    assert key in FOUND, (
        f"{key[0]}::{key[1]} no longer builds a User, creates one or switches one on: "
        "remove it from EXEMPT")


def test_every_flow_that_asks_the_seats_also_answers_a_refusal():
    """Asking is half of it: a `SeatLimitReached` nobody catches is a 500. A
    caller must handle it (the sign-ups turn it into a 403, the command line
    prints it)."""
    for rel in ("api/routers/auth.py", "api/routers/admin_users.py", "ee/sso/router.py", "cli.py"):
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        handled = sum(
            1 for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
            and n.type is not None and "SeatLimitReached" in ast.unparse(n.type))
        asked = sum(1 for n in ast.walk(tree) if _is_seat_call(n))
        assert handled >= 1 and handled >= asked, (
            f"{rel}: {asked} call(s) to the seats but {handled} handler(s) of SeatLimitReached")


def test_the_seats_module_does_not_import_the_enterprise_code():
    """It is AGPL and the licence code (EE) imports IT, not the other way round."""
    tree = ast.parse((SRC / "users/seats.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("src.ee"), node.module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("src.ee") for a in node.names)
