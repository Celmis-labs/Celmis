"""Turning a sentence into one of the actions, and nothing else.

This is a thin layer on purpose. It owns no rules: every cap, every ownership
check and every refusal lives in `actions.py`, and this file's only job is to
decide WHICH action a sentence means and with what arguments. If it grew logic
of its own it would become the third surface that diverges from the other two —
which is the thing the convergence in actions.py was for.

WHY A CHAT AT ALL, given a form is better for a single action.

It is not better for a SET defined by a condition. "Generate documentation for
every service that has none" is a sentence; through the interface it is finding
them among forty and pressing a button forty times. "Audit everything under
acme-ai that has not been audited in thirty days" needs filters, saved
selections and bulk operations — a subsystem — and costs one line here.

So the catalogue below is deliberately short. Single-object work stays on the
buttons where it belongs.

NOTHING RUNS WITHOUT A SECOND PRESS.

Interpretation is a guess, and these verbs cost money and hours: a vault build
over twenty repositories is twenty times one model call per module. So the
model produces a PLAN — the action, the arguments, and the repositories it
resolves to — and a person approves that plan before anything is queued. The
plan is also where a misreading becomes visible: "all of them" meaning forty
repositories instead of four is obvious in a list and invisible in a sentence.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The verbs a sentence may reach. Every one operates on a SET — that is the
#: whole argument for this surface, and adding a single-object verb here would
#: be building a worse version of a button that already exists.
#:
#: Two classes, and the distinction is the one that matters:
#:
#:   WRITES queue work that costs money and hours. They are planned, shown,
#:   and run only on a second press.
#:
#:   READS ("reads": True) answer. They are not planned — a person who asks
#:   which repositories exist and is handed a card to approve has been given a
#:   form, badly. They run immediately and their result IS the reply.
#:
#: The chat had no reads at all until now, which is the whole reason it felt
#: narrow: it could start a documentation build over twenty repositories and
#: could not say which twenty. The verbs existed in actions.py — get_dep_audit
#: and list_dep_findings were written and never listed here — so the surface
#: was narrower than the code behind it.
CATALOGUE: dict[str, dict[str, Any]] = {
    "list_repos": {
        "summary": "Answer what repositories this workspace has and the state "
                   "of each: indexed, documented, automatic review on or off, "
                   "and which branch it reads.",
        "reads": True,
        "arguments": {},
    },
    "explain": {
        "summary": "Answer what Celmis is, what this agent can do, or how to "
                   "run it against a model you host yourself. Choose this for "
                   "any question ABOUT the product, about your own "
                   "capabilities, or about where the models run, rather than "
                   "about a repository.",
        "reads": True,
        # These clauses are the ENTIRE basis on which a small model picks a
        # topic, so each one is written in the words a person actually types.
        # `self_hosted` carries the vendor names on purpose: nobody asks
        # "what is your self-hosting story", they ask "can I use Ollama?".
        #
        # The half who have not chosen a server yet name no vendor at all.
        # They ask about the property instead — does it run offline, is
        # there an on-premise option, does it work without the cloud, does
        # our code leave our network — and none of those words were on
        # offer, so there was nothing for that question to be picked on.
        # The second and third sentences are that concern spelled out; the
        # phrasings they were written against are in
        # tests/automation/test_it_explains_running_your_own_model.py.
        #
        # The semicolon separates one topic from the next. One inside a
        # clause reads as a fourth topic named after half a sentence, so the
        # punctuation within a clause is commas and full stops on purpose.
        "arguments": {
            "topic": "product — what Celmis is and what it is for; "
                     "capabilities — what this agent itself can do; "
                     "self_hosted — using your own local model instead of a "
                     "hosted provider: a self-hosted LLM server such as "
                     "Ollama, vLLM, LM Studio or llama.cpp, an "
                     "OpenAI-compatible endpoint, your own hardware, GPU, "
                     "servers or infrastructure. Host it yourself, run "
                     "offline with no internet, without the cloud, on-prem, "
                     "on-premise, on premises, air-gapped. Keep code and "
                     "data in-house, so that your code does not leave your "
                     "network and you never send it to an outside provider",
        },
    },
    "audit_status": {
        "summary": "Answer how the most recent dependency audit went — what "
                   "it covered, and what it found by severity.",
        "reads": True,
        "arguments": {
            "run_id": "a specific run, or null for the most recent one",
        },
    },
    "list_findings": {
        "summary": "Answer which dependency findings exist — outdated or "
                   "vulnerable packages, worst first.",
        "reads": True,
        "arguments": {
            "run_id": "a specific run, or null for the most recent one",
            "severity": "critical | high | medium | low, or null for all",
        },
    },
    "generate_docs": {
        "summary": "Queue documentation (module PRDs, feature docs, "
                   "integration guides) for a set of repositories.",
        "arguments": {
            "repo_slugs": "list of repository slugs, or null for all of them",
            "owner": "owner prefix such as 'acme', or null",
            "missing_only": "true to cover only repositories with no "
                            "documentation yet — usually what is meant",
            "language": "language code, or null for the workspace default",
            "engine": "api | claude_code, or null for the workspace default",
        },
    },
    "start_dep_audit": {
        "summary": "Queue a dependency and vulnerability audit over a set of "
                   "repositories.",
        "arguments": {
            "repo_slugs": "list of repository slugs, or null for all of them",
            "owner": "owner prefix, or null",
            "branch": "branch to read for this run only, or null",
            "report_engine": "none | api | claude_code",
        },
    },
    "set_auto_review": {
        "summary": "Turn automatic pull-request review on or off for a set of "
                   "repositories, optionally pinning the branch they are read "
                   "from.",
        "arguments": {
            "repo_slugs": "list of repository slugs, or null for all of them",
            "owner": "owner prefix, or null",
            "enabled": "true to arm review, false to disarm it",
            "branch": "branch to pin as the ref every surface reads, or null "
                      "to leave it as it is",
            "mode": "polling | webhook, or null for the provider default",
        },
    },
}

#: What `explain` can be asked about. Each one is a key the client renders a
#: written-down paragraph for, in sixteen languages — so this tuple is the
#: list of answers that exist, and a topic outside it renders as a blank reply
#: to a question somebody actually asked. The executor clamps to it rather
#: than passing the model's word through; the planner is shown the same names
#: in the `topic` argument above.
EXPLAIN_TOPICS = ("product", "capabilities", "self_hosted")


_SYSTEM = """You turn one sentence into a list of actions, or into a refusal.

You may only choose from the catalogue you are given. You never invent an
action, an argument or a repository name.

Some actions ANSWER a question and some START work. A question about what
exists, what state something is in, or what was found is an answering action —
choose it freely, it costs nothing and runs immediately. An action that starts
work is shown to the person first and waits for their approval.

A sentence often asks for more than one thing: "find repo A, turn on review
for its release branch, and in parallel audit the feature branch of B" is
three arguments to two actions, not one action. Return one step per action, in
the order they were asked for. Every step is shown to the person and approved
together — they do not get executed one at a time, so "in parallel" and "then"
describe the same plan. Say in your note if the order actually matters and you
could not express it.

Rules that matter more than being helpful:
- If the sentence does not clearly name any of these actions, return an empty
  steps list and say what you would need to know. A wrong guess here costs
  somebody hours of model time.
- Two steps of the same action with different arguments is normal and correct
  — auditing branch X of one repository and branch Y of another is two steps,
  never one step with both branches.
- `missing_only` defaults to true when the person says "missing", "that have
  none", "not documented yet" or similar. It defaults to false only when they
  clearly ask to redo work that exists.
- Never widen the scope. "The billing services" is not "all repositories". If
  you cannot tell which repositories are meant, leave repo_slugs null and say
  so in your note rather than guessing a list.
- You are choosing, not executing. A person sees your plan and approves it.

Answer in the same language the request was written in.

Write the fields of your answer in this order and no other: `language`, then
`note`, then `steps`. Your answer is read as it arrives and the person sees
`note` the moment it is written — before the steps exist — so a note written
last is a person watching nothing happen for several seconds.
"""


@dataclass
class Step:
    """One action with its arguments — the unit a person approves.

    A sentence is often two jobs: arm review on one repository's release
    branch and, in the same breath, audit another's feature branch. That was
    unreachable while a plan was a single action, and the honest failure was
    worse than the obvious one — the model picked whichever half it liked and
    the other half silently did not happen.
    """

    action: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    #: One sentence back to the person, in their own language.
    note: str = ""
    #: The repositories this step resolves to right now. Computed here rather
    #: than taken from the model: "all of them" meaning forty instead of four
    #: is obvious in a list and invisible in a sentence.
    resolved_repos: list[str] = field(default_factory=list)
    #: Populated when the step cannot run — the cap, an unregistered slug — so
    #: the refusal arrives before the person presses the second button.
    blocked: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "arguments": self.arguments,
            "note": self.note,
            "resolved_repos": self.resolved_repos,
            "blocked": self.blocked,
        }


@dataclass
class Plan:
    """What the sentence was understood to mean, before anything runs.

    The fields are declared in the order the model is told to write them —
    language, note, steps — so that reading this class and reading the prompt
    give the same answer to "what arrives first". See `interpret`.
    """

    #: The language the REQUEST was written in, as an ISO 639-1 code.
    #:
    #: Reported by the model, which has already read the sentence — one extra
    #: field on a reply we were paying for anyway, rather than a second call
    #: or a script-guessing heuristic that cannot tell Ukrainian from
    #: Bulgarian. It exists so the parts of the answer that are CANNED can be
    #: shown in the language the person used: those are written out in
    #: sixteen languages precisely so nothing pays a model to say them, and
    #: rendering them in the interface language instead answered a Ukrainian
    #: question with an English paragraph.
    language: str = ""
    #: One sentence about the whole thing, or the refusal when there are no
    #: steps at all. Second on the wire, and second here: it is the only field
    #: a person can read, so it is generated before the machine-readable half.
    note: str = ""
    steps: list[Step] = field(default_factory=list)

    @property
    def blocked(self) -> str | None:
        """The first refusal, if any step carries one.

        A plan is approved whole, so one blocked step blocks the press. Which
        step is at fault is visible in the list.
        """
        return next((st.blocked for st in self.steps if st.blocked), None)

    @property
    def reads_only(self) -> bool:
        """True when nothing here starts work.

        A plan that only answers is not a plan — it is the answer, and asking
        somebody to approve it would be handing them a form to press OK on.
        """
        return bool(self.steps) and all(
            CATALOGUE.get(st.action or "", {}).get("reads") for st in self.steps
        )

    @property
    def resolved_repos(self) -> list[str]:
        """Every repository the plan touches, deduplicated, in step order."""
        seen: dict[str, None] = {}
        for st in self.steps:
            for slug in st.resolved_repos:
                seen.setdefault(slug, None)
        return list(seen)

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "note": self.note,
            "steps": [st.as_dict() for st in self.steps],
            "resolved_repos": self.resolved_repos,
            "blocked": self.blocked,
        }


def _catalogue_prompt() -> str:
    lines = []
    for name, spec in CATALOGUE.items():
        lines.append(f"- {name}: {spec['summary']}")
        for arg, desc in spec["arguments"].items():
            lines.append(f"    {arg}: {desc}")
    return "\n".join(lines)


def interpret(
    message: str,
    *,
    workspace_id: str,
    user_id: str,
    on_note: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Plan:
    """Read a sentence into a plan. Runs nothing.

    Uses the same gateway client every other surface does, so a workspace's own
    provider and its spend apply here too.

    `on_note` is handed the human-readable sentence WHILE the model is still
    writing the rest of the plan — see the field-order comment below for why
    that is possible at all. `should_stop` is asked between chunks, so a person
    pressing Stop interrupts the model instead of waiting for it to finish.
    Pass neither and this is the single blocking call it has always been.
    """
    from src.llm.client import build_llm_client

    # ══ FIELD ORDER IS THE FEATURE. DO NOT "TIDY" IT. ══════════════════
    #
    # This answer is JSON, and it is read as it arrives. Streaming it in the
    # obvious order puts `{"steps": [{"action": "expl` on screen, which is
    # worse than a spinner — it is a spinner plus noise.
    #
    # `note` is the one field a human can read. Asking for it SECOND — after
    # the two tokens of `language`, before the steps — means the sentence is
    # complete about a second in, while the plan itself is still being
    # generated. It used to be last, so it existed only once everything else
    # did: three to seven seconds of nothing.
    #
    # Reordering these fields back would silently delete the whole feature and
    # every parser test would still pass, because `_parse` does not care about
    # order. tests/automation/test_the_sentence_arrives_first.py is what
    # notices. The same order is stated in `_SYSTEM`, on purpose: models weight
    # the two differently and this one is cheap to say twice.
    prompt = (
        f"Available actions:\n{_catalogue_prompt()}\n\n"
        f"Request: {message}\n\n"
        'Answer with JSON only, with the fields in EXACTLY this order — '
        '"language" first, then "note", then "steps":\n'
        '{"language": "<ISO 639-1 code of the language the REQUEST was '
        'written in>", '
        '"note": "<one sentence about the whole request>", '
        '"steps": [{"action": "<name>", "arguments": {...}, '
        '"note": "<one sentence about this step>"}]}\n'
        'An empty steps list means you did not recognise an action.'
    )

    # An unset agent profile means "whatever chat uses" — the behaviour before
    # the profile existed. Only a workspace that has actually chosen one gets
    # a separate route, so adding this surface cannot break a workspace that
    # never asked for it.
    try:
        from src.llm.profiles import is_configured

        surface = "agent" if is_configured("agent", workspace_id) else "chat"
    except Exception:  # noqa: BLE001
        surface = "chat"

    def _model(_agent: str | None = None) -> str | None:
        try:
            from src.llm.profiles import resolve_profile

            return resolve_profile(surface, workspace_id).model
        except Exception:  # noqa: BLE001
            return None

    # Its own line on the bill. It booked to "qa" — the same bucket as chat —
    # so a workspace looking at Usage could not tell what the agent cost it
    # from what asking questions cost it, which are different decisions.
    client = build_llm_client(user_id, workspace_id, surface=surface,
                              spend_surface="automation", resolve_model=_model)

    seen = {"note": ""}

    def _delta(text_so_far: str) -> bool:
        """Called between chunks with everything written so far."""
        if should_stop is not None and should_stop():
            return False
        if on_note is not None:
            note = _partial_note(text_so_far)
            if note and note != seen["note"]:
                seen["note"] = note
                on_note(note)
        return True

    response = client.generate(
        prompt=prompt, agent="automation", system_instruction=_SYSTEM,
        mode="qa", operation="automation_interpret", temperature=0.0,
        max_output_tokens=800,
        # Only when somebody is listening. A caller that wants neither the
        # sentence nor the ability to stop takes the plain call — one path
        # fewer to be wrong in the CLI and in tests.
        on_delta=_delta if (on_note is not None or should_stop is not None) else None,
        # A person is watching a spinner for this one. The client's defaults —
        # 120 s, three retries — are sized for an architect call carrying a
        # whole diff, and inheriting them here means up to eight minutes of
        # "Reading…" for a sentence a flash model answers in under two
        # seconds. Measured on production: 0.9 s typical, 200 s when the
        # upstream stalled and the retry ladder ran.
        #
        # One retry, because a single transient 503 should not cost the person
        # a second press; a short ceiling, because after twenty seconds the
        # honest thing is to say so and let them try again.
        timeout=20, num_retries=1,
    )
    return _parse(getattr(response, "text", "") or "")


#: The opening of the note field in a JSON object that is not finished yet.
_NOTE_OPENS = re.compile(r'"note"\s*:\s*"')


def _partial_note(buffer: str) -> str:
    """The sentence out of a half-written plan, or "".

    A tolerant scan rather than a parse, because the object is unfinished BY
    DEFINITION: `json.loads` on `{"language": "uk", "note": "Читаю` raises,
    and it would go on raising until the last brace arrives — which is the
    exact moment this is no longer needed.

    So it walks the string value by hand, respecting backslash escapes, and
    stops at the closing quote or at the end of what has arrived. A trailing
    half-escape (`\\` with nothing after it, `\\u00` mid-codepoint) is dropped:
    it would be a decode error one chunk before it becomes a character.

    It takes the FIRST `note` in the buffer. With the documented field order
    that is the plan's own note. If a model ignores the order and writes steps
    first, the first note found is a step's note — still a sentence about the
    work, which is a good degradation rather than a wrong one.
    """
    match = _NOTE_OPENS.search(buffer)
    if not match:
        return ""

    out: list[str] = []
    escaped = False
    for ch in buffer[match.end():]:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            break
        out.append(ch)

    raw = "".join(out)
    if escaped:            # dangling backslash — its partner has not arrived
        raw = raw[:-1]
    raw = re.sub(r'\\u[0-9a-fA-F]{0,3}$', "", raw)   # half a \\uXXXX escape
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _language(data: dict[str, Any]) -> str:
    """The ISO 639-1 code the model reported, or nothing.

    Exactly two letters, not the first two of whatever it said. Truncating a
    language NAME looks like it works — "Ukrainian" gives "uk", "Deutsch"
    gives "de" — right up to "Spanish", which gives "sp" and silently picks
    no dictionary at all. Nothing is a fine answer here: the interface
    language is the fallback and it is usually right.
    """
    value = str(data.get("language") or "").strip().lower()
    return value if len(value) == 2 and value.isalpha() else ""


def _parse(text: str) -> Plan:
    """Read the model's JSON, or refuse.

    A reply that cannot be parsed becomes "I did not understand", never a
    default action — the failure mode to avoid is a misread sentence that runs
    something plausible.

    The single-object shape is still accepted. Not for old clients — there are
    none — but because a model asked for a list occasionally answers with one
    object anyway, and throwing that away would turn a understood sentence
    into a shrug.
    """
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return Plan(note="I could not read that as an action.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Plan(note="I could not read that as an action.")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = [data] if data.get("action") else []

    steps: list[Step] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        if action is None:
            continue
        if action not in CATALOGUE:
            # A hallucinated verb is a refusal, not an attempt. The step is
            # dropped and said out loud rather than silently skipped: a plan
            # that runs two of the three things asked for is worse than one
            # that admits it.
            logger.info("automation_chat_unknown_action action=%s", action)
            return Plan(note=f"There is no action called {action!r}.",
                        language=_language(data))
        arguments = raw.get("arguments")
        steps.append(Step(
            action=action,
            arguments=arguments if isinstance(arguments, dict) else {},
            note=str(raw.get("note") or ""),
        ))

    note = str(data.get("note") or "")
    if not steps and not note:
        note = "I could not read that as an action."
    return Plan(steps=steps, note=note, language=_language(data))


def resolve_scope(plan: Plan, *, workspace_id: str) -> Plan:
    """Fill in which repositories each step actually covers, and block early.

    The same selection the action will make, run here so the person sees the
    list before approving rather than the count afterwards. Every step is
    resolved — a plan with one good step and one that names an unregistered
    repository must show which is which, not fail at the first.
    """
    from src.api.auto_review import get_auto_review_store
    from src.automation.actions import (
        MAX_AUDIT_REPOS,
        MAX_AUTO_REVIEW_REPOS,
        MAX_VAULT_REPOS,
    )

    caps = {
        "generate_docs": MAX_VAULT_REPOS,
        "start_dep_audit": MAX_AUDIT_REPOS,
        "set_auto_review": MAX_AUTO_REVIEW_REPOS,
    }

    if not plan.steps:
        return plan

    store = get_auto_review_store()
    registered = {c.repo_slug: c for c in store.list_for_workspace(workspace_id)}

    for step in plan.steps:
        if CATALOGUE.get(step.action or "", {}).get("reads"):
            # A read has no fan-out to show and no cap to breach. Resolving a
            # repository list for it would put a scope card in front of a
            # question.
            continue
        slugs = step.arguments.get("repo_slugs") or None
        owner = (step.arguments.get("owner") or "").strip() or None

        if slugs:
            unknown = [s for s in slugs if s not in registered]
            if unknown:
                step.blocked = ("Not registered in this workspace: "
                                + ", ".join(sorted(unknown)))
                continue
            chosen = list(dict.fromkeys(slugs))
        else:
            chosen = sorted(registered)
            if owner:
                prefix = owner.rstrip("/") + "/"
                chosen = [s for s in chosen
                          if registered[s].full_name.startswith(prefix)]

        step.resolved_repos = chosen
        if not chosen:
            step.blocked = "Nothing matched — no repositories in scope."
            continue

        cap = caps.get(step.action or "", MAX_AUDIT_REPOS)
        if len(chosen) > cap:
            step.blocked = (
                f"That is {len(chosen)} repositories; at most {cap} can be "
                f"queued at once. Narrow it down, or run it in batches."
            )
    return plan


def _explain_topic(raw: Any) -> str:
    """Which written-down answer to show, out of whatever the model wrote.

    The model chooses the topic, so the model can choose one that does not
    exist — passed through, it reaches the page as a string nothing matches
    and the person who asked gets an empty reply.

    Hyphens and spaces fold into the underscore form because `self-hosted` is
    how that word is spelled everywhere except in this tuple, and a model
    echoing the person's spelling would otherwise be answered with the
    product paragraph: "can I use Ollama?" met with "Celmis is a…" is worse
    than saying nothing.
    """
    topic = str(raw or "product").strip().lower()
    topic = topic.replace("-", "_").replace(" ", "_")
    return topic if topic in EXPLAIN_TOPICS else "product"


def _self_hosted_surfaces() -> dict[str, list[str]]:
    """Which surfaces a workspace admin can point at their own server, and
    which belong to whoever owns the installation's .env.

    Derived from the rule that enforces it instead of restated next to it.
    Saving a profile refuses a base_url on any surface outside
    `_BASE_URL_SURFACES` — indexing ships source code to the embedder, so
    where the embeddings go is an operator decision rather than a dropdown —
    and an agent that answered "choose it in Settings" for embeddings would be
    sending a workspace admin to a form that rejects them. Written out here it
    would be true today and quietly wrong the day that rule moves.
    """
    from src.api.routers.llm import _BASE_URL_SURFACES
    from src.llm.profiles import PROFILE_NAMES

    return {
        "ui_surfaces": [s for s in PROFILE_NAMES if s in _BASE_URL_SURFACES],
        "env_surfaces": [s for s in PROFILE_NAMES
                         if s not in _BASE_URL_SURFACES],
    }


async def execute(plan: Plan, actor, session) -> dict[str, Any]:
    """Run an approved plan. Refuses anything the plan itself blocked.

    All steps or none. A plan is approved as a whole, and half-running one is
    the outcome nobody can act on: the person cannot tell what happened
    without reading a log, and pressing again would redo the half that worked.
    """
    from src.automation.actions import (
        ActionError,
        generate_docs,
        get_dep_audit,
        list_dep_findings,
        list_repos,
        set_auto_review,
        start_dep_audit,
    )

    if not plan.steps:
        raise ActionError("There is nothing to run.")
    blocked = plan.blocked
    if blocked:
        raise ActionError(blocked)

    logger.info("automation_chat_execute steps=%d ws=%s repos=%d by=%s",
                len(plan.steps), actor.workspace_id,
                len(plan.resolved_repos), actor.email)

    results: list[dict[str, Any]] = []
    for step in plan.steps:
        args = dict(step.arguments)
        if step.action == "explain":
            # Canned on the client, in sixteen languages. Nothing here costs
            # a token: the result says WHICH text to show, not the text.
            topic = _explain_topic(args.get("topic"))
            outcome = {"topic": topic}
            if topic == "self_hosted":
                # The commands and the .env lines are not that written-down
                # prose. They are English, they change when a server project
                # renames a flag, and GET /api/llm/local-setup-guide already
                # serves them to this same client. Copying them into this
                # reply would put them in a second place AND freeze them
                # there: a run's result is written to its row, so every answer
                # ever given would keep the commands as they were on the day
                # it was asked.
                #
                # What travels instead is the one thing the guide does not
                # say — which of these surfaces the person reading the answer
                # can actually change, and which one is not theirs to change.
                outcome.update(_self_hosted_surfaces())
        elif step.action == "list_repos":
            outcome = list_repos(actor)
        elif step.action == "audit_status":
            try:
                outcome = await get_dep_audit(
                    actor, session, run_id=args.get("run_id"))
            except ActionError:
                # "Nothing has been audited yet" is an ANSWER to the question
                # that was asked, not a failure to answer it. The action
                # raises because MCP callers want the refusal; a person who
                # asked how the last audit went should be told there wasn't
                # one, rather than shown a red error.
                outcome = {"run_id": None, "status": "", "error": "",
                           "summary": {}, "created_at": ""}
        elif step.action == "list_findings":
            # `run_id` is required below, and a person asking "what did it
            # find" means the last run. Resolving it here rather than making
            # them quote a uuid.
            run_id = args.get("run_id")
            if not run_id:
                run_id = (await get_dep_audit(actor, session))["run_id"]
            outcome = {"findings": await list_dep_findings(
                actor, session, run_id,
                severity=args.get("severity"), limit=50,
            ), "run_id": run_id}
        elif step.action == "generate_docs":
            outcome = await generate_docs(
                actor, session,
                repo_slugs=args.get("repo_slugs"),
                owner=args.get("owner"),
                missing_only=bool(args.get("missing_only")),
                language=args.get("language"),
                engine=args.get("engine"),
            )
        elif step.action == "set_auto_review":
            # Synchronous: it writes config rather than queueing work, so it
            # is done by the time this returns.
            outcome = set_auto_review(
                actor,
                repo_slugs=args.get("repo_slugs"),
                owner=args.get("owner"),
                enabled=bool(args.get("enabled", True)),
                branch=args.get("branch"),
                mode=args.get("mode"),
            )
        else:
            outcome = await start_dep_audit(
                actor, session,
                repo_slugs=args.get("repo_slugs"),
                owner=args.get("owner"),
                branch=args.get("branch"),
                report_engine=str(args.get("report_engine") or "none"),
            )
        results.append({"action": step.action, "result": outcome})

    # Flattened alongside the per-step results: the toast says "N started",
    # and counting queued jobs across steps in the browser would put that sum
    # in a second place where it can disagree with this one.
    queued = [q for r in results for q in (r["result"].get("queued") or [])]
    skipped = [q for r in results for q in (r["result"].get("skipped") or [])]
    changed = sum(int(r["result"].get("count") or 0) for r in results)
    run_ids = [r["result"]["run_id"] for r in results if r["result"].get("run_id")]
    return {
        "steps": results,
        "queued": queued,
        "skipped": skipped,
        "changed": changed,
        "run_id": run_ids[0] if run_ids else None,
    }


__all__ = ["CATALOGUE", "EXPLAIN_TOPICS", "Plan", "Step", "execute",
           "interpret", "resolve_scope"]

