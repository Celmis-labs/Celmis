"""The chat owns no rules. That is the whole design.

Every cap, ownership check and refusal lives in actions.py. This layer decides
WHICH action a sentence means and with what arguments, and stops. If it grew
logic of its own it would become the third surface that diverges from the other
two — which is exactly what converting the audit route was for.

The other half is that nothing runs on a guess. These verbs cost hours of model
time, so interpretation produces a PLAN and a person approves it. The plan is
also where a misreading becomes visible: "all of them" meaning forty
repositories instead of four is obvious in a list and invisible in a sentence.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
CHAT = (SRC / "automation" / "chat.py").read_text(encoding="utf-8")
ROUTER = (SRC / "api" / "routers" / "automation.py").read_text(encoding="utf-8")


# ─── thin ────────────────────────────────────────────────────────────


def test_the_chat_only_calls_actions():
    """No queue, no run row, no job payload. The moment this file enqueues
    anything itself it is a second implementation."""
    tree = ast.parse(CHAT)
    body = ast.unparse(tree)
    assert "enqueue(" not in body, "the chat queues its own jobs"
    assert "DepAuditRun(" not in body, "the chat creates its own rows"
    assert "from src.automation.actions import" in CHAT


def test_every_verb_that_starts_work_takes_a_set():
    """Single-object work stays on the buttons. A WRITE here that acts on one
    repository would be a worse version of a control that already exists —
    and the reason this surface is justified at all is sets.

    Reads are exempt and were the missing half: "which repositories do I have"
    has no set to take, and refusing it because the argument list has no
    repo_slugs is how the agent ended up unable to answer anything at all.
    """
    from src.automation.chat import CATALOGUE

    for name, spec in CATALOGUE.items():
        if spec.get("reads"):
            continue
        assert "repo_slugs" in spec["arguments"], (
            f"{name} cannot take a set, so it belongs on a button"
        )


def test_the_catalogue_reaches_what_actions_can_do():
    """The divergence that made this surface feel narrow: get_dep_audit and
    list_dep_findings were written in actions.py and never listed here, so the
    chat was narrower than the code behind it."""
    import inspect

    from src.automation import actions

    public = {n for n, o in vars(actions).items()
              if not n.startswith("_") and inspect.isfunction(o)
              and o.__module__ == actions.__name__}
    # register_repo takes a URL rather than a set — it is the one verb that
    # belongs on the Add-repository form and nowhere else.
    unreachable = public - {"register_repo"} - {
        "list_repos", "get_dep_audit", "list_dep_findings", "generate_docs",
        "start_dep_audit", "set_auto_review",
    }
    assert not unreachable, (
        f"actions.py can do {sorted(unreachable)} and no sentence can reach it"
    )


# ─── nothing runs on a guess ─────────────────────────────────────────


def test_interpretation_and_execution_are_separate_endpoints():
    assert '@router.post("/plan"' in ROUTER
    assert '@router.post("/execute"' in ROUTER
    plan_fn = ROUTER[ROUTER.index('@router.post("/plan"'):ROUTER.index('class ExecuteIn')]
    assert "execute(" not in plan_fn, "planning can run something"


def test_an_unreadable_reply_is_a_refusal_not_a_default():
    """The failure to avoid is a misread sentence that runs something
    plausible."""
    from src.automation.chat import _parse

    for junk in ("I think you want to generate docs", "", "{not json"):
        assert _parse(junk).steps == []


def test_a_hallucinated_verb_is_refused_by_name():
    from src.automation.chat import _parse

    plan = _parse('{"action": "delete_everything", "arguments": {}}')
    assert plan.steps == []
    assert "delete_everything" in plan.note

    # And inside a list, where dropping the bad step silently would leave a
    # plan that runs two of the three things asked for.
    plan = _parse('{"steps": [{"action": "generate_docs", "arguments": {}}, '
                  '{"action": "rm_rf", "arguments": {}}]}')
    assert plan.steps == []
    assert "rm_rf" in plan.note


def test_a_valid_plan_survives_parsing():
    from src.automation.chat import _parse

    plan = _parse('{"action": "generate_docs", '
                  '"arguments": {"missing_only": true}, "note": "ok"}')
    assert [st.action for st in plan.steps] == ["generate_docs"]
    assert plan.steps[0].arguments == {"missing_only": True}


def test_one_sentence_can_be_two_jobs():
    """"Arm review on the release branch of one and audit the feature branch
    of another" is two steps. While a plan was a single action the model had
    to pick a half, and the half it dropped happened silently."""
    from src.automation.chat import _parse

    plan = _parse('{"steps": ['
                  '{"action": "set_auto_review", "arguments": '
                  '{"repo_slugs": ["a"], "branch": "release"}}, '
                  '{"action": "start_dep_audit", "arguments": '
                  '{"repo_slugs": ["b"], "branch": "feat"}}], "note": "both"}')
    assert [st.action for st in plan.steps] == ["set_auto_review",
                                                "start_dep_audit"]
    assert plan.steps[0].arguments["branch"] == "release"
    assert plan.steps[1].arguments["branch"] == "feat"


def test_a_question_is_answered_rather_than_planned():
    """A plan card asking somebody to approve "which repositories do I have"
    is a form with one button. Reads run and their result is the reply."""
    from src.automation.chat import _parse

    plan = _parse('{"steps": [{"action": "list_repos", "arguments": {}}]}')
    assert plan.reads_only is True

    mixed = _parse('{"steps": [{"action": "list_repos", "arguments": {}}, '
                   '{"action": "generate_docs", "arguments": {}}]}')
    assert mixed.reads_only is False, (
        "one write in the plan means the whole plan waits for a press"
    )


# ─── the plan is checked, and checked again ──────────────────────────


def test_the_scope_is_resolved_before_approval_not_after():
    """The count is not enough — the list is the point. A scope read as forty
    repositories instead of four is invisible in a sentence."""
    from src.automation.chat import Plan, Step

    assert "resolved_repos" in Step.__dataclass_fields__
    assert hasattr(Plan, "resolved_repos"), "the plan cannot say what it covers"
    assert "resolve_scope" in CHAT
    assert "resolved_repos" in ROUTER


def test_a_blocked_plan_says_so_before_the_second_press():
    """A refusal that arrives after somebody approves the work is a refusal
    they had no chance to act on."""
    from src.automation.chat import Plan, Step

    assert "blocked" in Step.__dataclass_fields__
    assert hasattr(Plan, "blocked"), "a blocked step does not block the plan"
    assert "step.blocked" in CHAT


def test_execute_refuses_a_blocked_plan():
    """One blocked step blocks the press. A plan is approved whole, and
    half-running one leaves nobody able to say what happened."""
    assert "blocked = plan.blocked" in CHAT
    assert "raise ActionError(blocked)" in CHAT


def test_the_scope_shown_is_not_trusted_as_authorisation():
    """The client posts the plan back, and a client can post anything. The
    server re-resolves and the action re-checks — the list on screen is for the
    person, not for the permission system."""
    execute_fn = ROUTER[ROUTER.index("async def execute_plan("):]
    assert "resolve_scope(" in execute_fn, (
        "the posted scope is used as-is, so a caller can name any repository"
    )


def test_an_unknown_action_is_rejected_at_the_endpoint_too():
    """Not only in the parser: /execute is reachable directly."""
    assert "not in CATALOGUE" in ROUTER


# ─── the guards it inherits ──────────────────────────────────────────


def test_the_caps_come_from_the_actions():
    """Not redefined here. Two numbers for one limit is the divergence this
    codebase just finished removing."""
    from src.automation import chat

    assert "MAX_VAULT_REPOS" not in [
        n for n in dir(chat) if not n.startswith("_")
    ], "the chat defines its own cap"
    # The NAMES, not the line they happen to sit on: the linter re-wrapped
    # this import and a test that pinned the formatting failed while the
    # property it was guarding was untouched.
    for cap in ("MAX_AUDIT_REPOS", "MAX_AUTO_REVIEW_REPOS", "MAX_VAULT_REPOS"):
        assert "from src.automation.actions import" in CHAT
        assert cap in CHAT, f"{cap} is no longer taken from actions.py"


def test_spend_goes_to_the_right_surface():
    """Interpretation is a model call, and it gets its own line on the bill.

    It billed to "qa" — the same bucket as chat — so Usage could not tell
    what the agent cost from what asking questions cost, which are different
    decisions. Not "review" either: that is the surface this codebase had to
    unpick once already, and a budget set on review would then throttle a
    planner.
    """
    assert 'spend_surface="automation"' in CHAT
    assert 'spend_surface="review"' not in CHAT


def test_the_model_is_told_not_to_widen_the_scope():
    """The dangerous failure is not refusing too much, it is quietly meaning
    more repositories than were asked for."""
    assert "Never widen the scope" in CHAT
    assert "missing_only" in CHAT


def test_the_answer_follows_the_question_language():
    """Same rule as Q&A: a person who asks in Ukrainian is asking to be
    answered in Ukrainian."""
    assert "same language the request was written in" in CHAT.lower()


@pytest.mark.parametrize("locale", sorted(
    p.stem for p in (ROOT / "web" / "lib" / "i18n" / "messages").glob("*.json")))
def test_every_locale_has_the_page_strings(locale):
    import json

    messages = ROOT / "web" / "lib" / "i18n" / "messages"
    en = json.loads((messages / "en.json").read_text(encoding="utf-8"))
    data = json.loads((messages / f"{locale}.json").read_text(encoding="utf-8"))
    keys = [k for k in en if k.startswith("automation.")]
    assert keys
    missing = [k for k in keys if k not in data]
    assert not missing, f"{locale} is missing {missing}"


def test_every_catalogue_verb_has_a_label():
    """The plan card names the action. A verb added to the catalogue without a
    string renders the raw key to the user."""
    import json

    from src.automation.chat import CATALOGUE

    en = json.loads(
        (ROOT / "web" / "lib" / "i18n" / "messages" / "en.json").read_text(
            encoding="utf-8"))
    for verb in CATALOGUE:
        assert f"automation.action.{verb}" in en, f"{verb} has no label"


def test_the_two_agents_are_told_apart_by_something_other_than_the_label():
    """The sidebar now reads "Claude agent" and "Celmis agent" next to each
    other — one word apart, and they do opposite things: one writes code inside
    a repository, this one runs Celmis's own operations across a set of them.

    The label can no longer carry that distinction, so the subtitle has to.
    """
    import json

    en = json.loads(
        (ROOT / "web" / "lib" / "i18n" / "messages" / "en.json").read_text(
            encoding="utf-8"))
    subtitle = en["automation.subtitle"].lower()
    assert "not a coding agent" in subtitle, (
        "nothing on the page says which of the two agents this is"
    )
