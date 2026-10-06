"""`outcome_hooks`: one place says what became of an issue, any number hear it.

The learning store and the productivity metrics listen without the ledger
importing them. A listener that raises must not break the ledger or silence
the others, and registering twice registers once.
"""

from __future__ import annotations

import pytest

from src.review import outcome_hooks as hooks


def _outcome(kind="implemented") -> hooks.IssueOutcome:
    return hooks.IssueOutcome(
        kind=kind, workspace_id="ws", issue_id="i1", repo_slug="r", pr_provider="github",
        pr_repo="acme/api", pr_number=7, fingerprint="fp")


@pytest.fixture(autouse=True)
def _clean():
    before = hooks.listeners()
    for fn in before:
        hooks.unregister_outcome_listener(fn)
    yield
    for fn in hooks.listeners():
        hooks.unregister_outcome_listener(fn)
    for fn in before:
        hooks.register_outcome_listener(fn)


def test_every_listener_hears_the_outcome() -> None:
    a, b = [], []
    hooks.register_outcome_listener(a.append)
    hooks.register_outcome_listener(b.append)
    assert hooks.emit_outcome(_outcome()) == 2
    assert a == b == [_outcome()]


def test_a_listener_that_raises_does_not_silence_the_others() -> None:
    heard = []

    def broken(_o):
        raise RuntimeError("boom")

    hooks.register_outcome_listener(broken)
    hooks.register_outcome_listener(heard.append)
    assert hooks.emit_outcome(_outcome()) == 1
    assert len(heard) == 1


def test_registering_the_same_function_twice_registers_it_once() -> None:
    seen = []
    hooks.register_outcome_listener(seen.append)
    hooks.register_outcome_listener(seen.append)
    hooks.emit_outcome(_outcome())
    assert len(seen) == 1


def test_an_unregistered_listener_hears_nothing() -> None:
    seen = []
    hooks.register_outcome_listener(seen.append)
    hooks.unregister_outcome_listener(seen.append)
    hooks.unregister_outcome_listener(seen.append)   # twice: harmless
    assert hooks.emit_outcome(_outcome()) == 0 and seen == []


def test_the_kinds_are_the_ones_the_ledger_emits() -> None:
    assert set(hooks.OUTCOME_KINDS) == {
        "implemented", "unimplemented", "dismissed", "abandoned",
        "resolved_later", "reopened"}
