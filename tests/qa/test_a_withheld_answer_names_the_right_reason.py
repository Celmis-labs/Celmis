"""Told to generate a vault that had been generated an hour earlier.

A member restricted to `metadata` asked for the source of one function. The
product did the right thing — not a line of code was quoted — and then
explained it with the wrong cause: "generate the vault on the «Repositories»
page and wait for the repositories to be indexed". The vault existed. Indexing
was done. Nothing the asker could do would have changed the outcome, because
the outcome was a policy, not a gap.

That is correct behaviour reading as a broken product. The notice had two
reasons in it — a missing vault and unreadable files — and no notion of a third
in which there is no remedy to name at all.
"""

from __future__ import annotations

import pytest

from src.qa.multi_repo_retriever import MultiRepoRetriever

BUILD = MultiRepoRetriever._build_no_code_notice


# ─── the reason with no button ───────────────────────────────────────


def test_a_policy_refusal_does_not_send_the_user_to_generate_a_vault():
    note = BUILD(vault_missing=True, access_denied=True).lower()

    assert "generate" not in note or "do not tell" in note
    assert "generate a vault" not in note.replace("do not tell them to generate a vault", "")


def test_it_says_the_code_was_withheld_on_purpose():
    note = BUILD(vault_missing=False, access_denied=True).lower()

    assert "access rule" in note
    assert "working as configured" in note


def test_it_names_the_one_step_that_can_change_it():
    """A refusal the reader cannot act on at all is only half an answer — the
    step exists, it just belongs to somebody else."""
    note = BUILD(vault_missing=False, access_denied=True)

    assert "admin" in note.lower()
    assert "Team & access" in note


def test_the_policy_reason_wins_over_a_missing_vault():
    """Both can be true at once. Only one of them is actionable, and it is not
    the one the asker can reach."""
    note = BUILD(vault_missing=True, access_denied=True).lower()

    assert "access rule" in note
    assert "has not been generated yet" not in note


# ─── and the other two reasons are untouched ─────────────────────────


def test_a_genuinely_missing_vault_still_says_so():
    note = BUILD(vault_missing=True, access_denied=False).lower()

    assert "has not been generated yet" in note
    assert "generate the vault" in note
    assert "access rule" not in note


def test_unreadable_files_still_say_so():
    note = BUILD(vault_missing=False, access_denied=False).lower()

    assert "none of the relevant files could be opened" in note
    assert "access rule" not in note


@pytest.mark.parametrize("denied", [True, False])
def test_no_variant_ever_licenses_invented_code(denied):
    """The rule every branch of this notice carries, and the one that must not
    be lost when a branch is added."""
    note = BUILD(vault_missing=False, access_denied=denied).lower()

    assert "never write code you have not read" in note


# ─── the caller has to notice the policy in the first place ──────────


def test_the_retriever_reads_the_decision_rather_than_guessing():
    """`access` holds one decision per repository the question reached. If not
    one of them makes code visible, no amount of indexing would have produced
    a line."""
    import inspect

    src = inspect.getsource(MultiRepoRetriever.retrieve) \
        if hasattr(MultiRepoRetriever, "retrieve") \
        else inspect.getsource(MultiRepoRetriever)
    assert "code_visible" in src
    assert "access_denied=" in src


def test_an_empty_access_map_is_not_a_refusal():
    """No decisions means nothing was scoped, not that everything was denied —
    the difference between a restricted member and a repository nobody has
    ruled on."""
    import ast
    import inspect

    src = inspect.getsource(MultiRepoRetriever)
    tree = ast.parse(src.lstrip())
    found = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "denied_by_policy" for t in n.targets)
    ]
    assert found, "the decision is no longer computed"
    assert "bool(decisions)" in ast.unparse(found[0]), (
        "an empty map would read as denied"
    )
