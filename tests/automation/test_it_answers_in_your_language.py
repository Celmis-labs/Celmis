"""A question asked in Ukrainian is answered in Ukrainian, including the half
that was never written by a model.

Two changes, and they only make sense together.

THE PLANNER NOW REPORTS THE LANGUAGE. The canned parts of a reply — what
Celmis is, what this agent can do, "no repositories are registered yet" —
exist in sixteen languages precisely so that saying them costs no model
tokens. They were rendered in the INTERFACE language, so somebody asking in
Ukrainian got a Ukrainian sentence from the model with an English panel bolted
underneath it: the translation was paid for and then not delivered. The model
has already read the sentence, so it reports the code as one extra field on a
reply that was being paid for anyway.

The dangerous way to implement that is a lenient parser. "Ukrainian" truncated
to two characters gives "uk" and looks like it works; "Deutsch" gives "de" and
still looks like it works; "Spanish" gives "sp", which is not a language code,
matches no dictionary, and silently falls back — so the bug ships looking
fixed and shows up only for the languages nobody on the team reads. So the
parser takes exactly two letters or nothing, and nothing is a fine answer:
the interface language is the fallback and it is usually right.

`EXPLAIN` IS THE VERB THAT MAKES THE LANGUAGE VISIBLE. "розкажи про celmis,
що це, що ти умієш" used to route to "I could not read that as an action" with
the capability list underneath — an answer, arriving as a failure, in the
wrong language. It is a real verb now, it is a READ so it answers without a
second press, and it returns `{"topic": "product" | "capabilities"}` and
NOTHING ELSE. The prose is canned on the client. That is the whole point: an
`explain` that returned paragraphs would be paying a model to recite a string
that is already written out in sixteen languages.

What breaks without each test below is in its docstring. Source is parsed
rather than grepped wherever it matters: the files here explain at length what
they no longer do — "rendering them in the interface language answered a
Ukrainian question with an English paragraph" contains every token a careless
check would look for — and this repository has shipped a test five times that
found its own token in the comment explaining its absence.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

# The strippers and the bracket-matching TSX readers, with their own guard
# tests, live next door. Importing them is deliberate: a second copy is a
# second thing to keep correct, and the guards would only cover one of them.
from tests.web.test_the_agent_is_a_conversation import (
    CODE,
    EN,
    LOCALES,
    MESSAGES,
    ROOT,
    SRC,
    WEB,
    _array,
    _body,
    _code,
    _pair,
    _python_source,
    _squash,
)

CHAT_SRC = (SRC / "automation" / "chat.py").read_text(encoding="utf-8")
HANDLERS_SRC = (SRC / "sync" / "handlers.py").read_text(encoding="utf-8")
VERSIONS = ROOT / "alembic" / "versions"
I18N = _code((WEB / "lib" / "i18n" / "index.tsx").read_text(encoding="utf-8"))

#: The canned prose `explain` stands in for. Both halves: the paragraphs the
#: page lists, and the two strings around them.
PRODUCT_KEYS = _array(CODE, "PRODUCT") + ["automation.product.title",
                                          "automation.product.scope"]
#: Keys under `automation.capabilities.` that are furniture rather than a verb.
CAPABILITY_FURNITURE = {"title", "intro", "readsTitle", "writesTitle",
                        "tryThis"}
#: The components that ARE a reply rather than decorate one: everything whose
#: text comes out of a dictionary keyed by the language of the QUESTION. Found
#: by the hook they call, so a block added for a new topic is covered by the
#: tests below on the day it is written.
CANNED_BLOCKS = {name for name in re.findall(r"function (\w+)\(", CODE)
                 if "useDictFor(" in _body(CODE, name)}


def _function(source: str, name: str) -> ast.AST:
    """The definition of `name`, sync or async."""
    node = next((n for n in ast.walk(ast.parse(source))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == name), None)
    assert node is not None, f"{name} is gone or renamed"
    return node


def _strings(node: ast.AST) -> str:
    """Every string literal under `node`, concatenated.

    Used on an assignment rather than a whole function, so a docstring is out
    of scope by construction — and it reads f-strings, which `literal_eval`
    refuses and which is what the planner's prompt is built as.
    """
    return "".join(n.value for n in ast.walk(node)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _assignment(fn: ast.AST, name: str) -> ast.AST:
    node = next((n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == name for t in n.targets)),
                None)
    assert node is not None, f"no assignment to {name}"
    return node


def _locale(code: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{code}.json").read_text(encoding="utf-8"))


def _revision_graph() -> dict[str | None, str | None]:
    """{revision: down_revision} over every migration in the tree.

    Both spellings are read: alembic's template writes `revision: str = "..."`
    and hand-written ones here use a bare assignment. Missing half of them
    would invent orphans and heads that are not there.
    """
    graph: dict[str | None, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        found: dict[str, object] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            target = None
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                target = node.targets[0].id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            if target in ("revision", "down_revision"):
                try:
                    found[target] = ast.literal_eval(node.value)
                except ValueError:  # pragma: no cover - a computed revision
                    found[target] = "?"
        graph[found.get("revision")] = found.get("down_revision")
    return graph


def _adds_column(path: Path, table: str, column: str) -> bool:
    """True when this migration's `upgrade()` really adds `table.column`.

    Read as a CALL, never as a substring of the file. This started life as
    `ast.unparse(module)` plus three `in` tests, and `ast.unparse` keeps the
    module docstring — so d1a7f3e0c945, which migrates `incoming_alerts` and
    whose prose explains that `automation_runs` was migrated separately, was
    counted as a second migration adding this column and failed the suite for
    saying true things about it. The trap this file's own docstring warns
    about, sprung by the one check here that was still grepping.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    upgrades = [n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "upgrade"]
    for node in (m for fn in upgrades for m in ast.walk(fn)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_column"
                and len(node.args) >= 2):
            continue
        target, col = node.args[0], node.args[1]
        if not (isinstance(target, ast.Constant) and target.value == table):
            continue
        # `sa.Column("name", …)` — the column's name is its first argument.
        if (isinstance(col, ast.Call) and col.args
                and isinstance(col.args[0], ast.Constant)
                and col.args[0].value == column):
            return True
    return False


def _client_locales() -> list[str]:
    """The codes `asLocale` will accept, read out of the client's own list."""
    at = I18N.index("const LOCALES")
    begin, end = _pair(I18N, I18N.index("=", at), "[", "]")
    return re.findall(r'"([a-z-]+)"', I18N[begin:end + 1])


# ─── the planner reports it, and only a real code is accepted ────────


def test_the_planner_is_asked_for_the_code_rather_than_the_name():
    """Read off the prompt that is actually sent, not off the comment above
    it. Asking for "the language" gets "Ukrainian" as often as "uk", and a
    field the model was never told the shape of is a field the parser will
    spend its life guessing at."""
    prompt = _strings(_assignment(_function(CHAT_SRC, "interpret"), "prompt"))
    assert "language" in prompt, "the planner is never asked what language it read"
    assert "ISO 639-1" in prompt, (
        "the prompt asks for a language without saying it wants a code"
    )
    assert "REQUEST" in prompt, (
        "the model is not told it is the question's language that is wanted — "
        "it will report the language it is answering in"
    )


@pytest.mark.parametrize("reported,expected", [
    ("uk", "uk"), ("EN", "en"), (" de ", "de"), ("Ja", "ja"),
    # The whole reason this is not `value[:2]`.
    ("Ukrainian", ""), ("Deutsch", ""), ("Spanish", ""), ("English", ""),
    # Neither a name nor a code.
    ("", ""), ("   ", ""), ("e", ""), ("e1", ""), ("en-GB", ""), ("und", ""),
])
def test_only_a_two_letter_code_is_accepted(reported: str, expected: str):
    """A language NAME truncated to two characters is the bug that hides.

    "Ukrainian" gives "uk" and "Deutsch" gives "de", so the reading looks
    correct for exactly the languages the people who wrote it can check.
    "Spanish" gives "sp", which is not ISO 639-1, matches no dictionary, and
    falls back silently — a Spanish speaker gets an English panel and there is
    nothing anywhere that says why.
    """
    from src.automation.chat import _language

    assert _language({"language": reported}) == expected


@pytest.mark.parametrize("data", [{}, {"language": None}, {"language": 7},
                                  {"language": ["uk"]}])
def test_no_language_is_a_normal_answer_not_an_error(data: dict):
    """The planner failing to name a language is expected — a two-word
    sentence, a repository slug, a shrug. It must cost nothing: the interface
    language is the fallback and it is usually right."""
    from src.automation.chat import _language

    assert _language(data) == ""


def test_a_code_the_client_cannot_use_is_the_same_as_none():
    """Ties the two halves together, and is the concrete argument against
    truncating a name: "sp" would be accepted by any check that only counts
    letters, and there is no `sp.json`. The client would find no dictionary,
    fall back, and the whole feature would quietly do nothing for Spanish."""
    known = _client_locales()
    assert "es" in known, "Spanish has a dictionary, so the code must reach it"
    assert "sp" not in known, (
        "a truncated 'Spanish' would look like a valid code and match nothing"
    )
    assert len(known) >= 16, known


def test_the_language_survives_the_parse():
    from src.automation.chat import _parse

    plan = _parse('{"steps": [{"action": "list_repos", "arguments": {}}], '
                  '"note": "ось вони", "language": "uk"}')
    assert plan.language == "uk"
    assert [st.action for st in plan.steps] == ["list_repos"]


def test_a_sentence_nobody_understood_still_carries_its_language():
    """The single most important case. No steps is precisely when the page
    answers with the canned capability list — so if the language is dropped
    on this path, the one reply that is entirely written-down text is the one
    reply rendered in the wrong language."""
    from src.automation.chat import _parse

    plan = _parse('{"steps": [], "note": "не зрозумів", "language": "uk"}')
    assert plan.steps == []
    assert plan.language == "uk"


def test_a_hallucinated_verb_keeps_the_language_too():
    """The refusal is built from scratch on this path rather than falling
    through, which is exactly how a field gets forgotten."""
    from src.automation.chat import _parse

    plan = _parse('{"action": "rm_rf", "arguments": {}, "language": "uk"}')
    assert plan.steps == []
    assert "rm_rf" in plan.note
    assert plan.language == "uk"


def test_an_unreadable_reply_reports_no_language_rather_than_a_wrong_one():
    """Nothing to read means nothing is known. Guessing from the request text
    here would be a script heuristic that cannot tell Ukrainian from
    Bulgarian."""
    from src.automation.chat import _parse

    for junk in ("", "{not json", "I think you want docs"):
        assert _parse(junk).language == ""


def test_the_plan_hands_the_language_on():
    """`as_dict` is what the worker writes to the row. A field on the
    dataclass that never reaches the dict is a field that exists only in
    memory, for the length of one function call."""
    from src.automation.chat import Plan

    assert Plan(language="uk").as_dict()["language"] == "uk"
    assert Plan().as_dict()["language"] == ""


# ─── it reaches the row, and the row reaches the client ──────────────


def test_the_row_has_somewhere_to_put_it():
    from src.db.models import AutomationRun

    column = AutomationRun.__table__.c["language"]
    assert column.nullable, (
        "a NOT NULL column would need a default, and every row written before "
        "this shipped has no language at all"
    )
    assert "TEXT" in str(column.type).upper()


def test_a_migration_adds_the_column_and_is_attached_to_the_chain():
    """A model column with no migration is a column that exists in the tests
    and not in production; a migration on a detached branch is a migration
    `alembic upgrade head` never runs. Both fail the same way — the ORM
    selects a column the database does not have."""
    adds = [p for p in sorted(VERSIONS.glob("*.py"))
            if _adds_column(p, "automation_runs", "language")]
    assert len(adds) == 1, f"expected exactly one migration adding it, got {adds}"

    text = adds[0].read_text(encoding="utf-8")
    assert "op.drop_column('automation_runs', 'language')" in ast.unparse(
        _function(text, "downgrade")), "the migration cannot be rolled back"

    graph = _revision_graph()
    parent = next(ast.literal_eval(n.value) for n in ast.parse(text).body
                  if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", None) == "down_revision")
    assert parent in graph, (
        f"the migration hangs off {parent!r}, which is not a revision here"
    )
    heads = [rev for rev in graph if rev not in set(graph.values())]
    assert len(heads) == 1, (
        f"{len(heads)} heads — `alembic upgrade head` cannot run them all: {heads}"
    )


def test_the_reading_writes_the_language_onto_the_row():
    """The reading happens on the worker, so this UPDATE is the only place the
    language can land. A column the planner fills and the writer ignores is
    the same as no column."""
    finish = _function(HANDLERS_SRC, "_finish_automation_plan")
    assert "language" in [a.arg for a in finish.args.kwonlyargs], (
        "the writer cannot be told a language"
    )
    sql = next(ast.literal_eval(c.args[0]) for c in ast.walk(finish)
               if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_text")
    assert "language" in sql, "the UPDATE does not set the column"


def test_a_later_write_cannot_erase_the_language():
    """The same UPDATE lands every outcome, and the paths that carry no
    language pass "". Written straight through, stopping a run — or any later
    bookkeeping — would blank the field on a row that had one, and the canned
    text would revert to the interface language on reload."""
    finish = _function(HANDLERS_SRC, "_finish_automation_plan")
    sql = next(ast.literal_eval(c.args[0]) for c in ast.walk(finish)
               if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_text")
    clause = sql[sql.index("language ="):]
    clause = clause[:clause.index(",")] if "," in clause else clause
    assert "COALESCE" in clause.upper() and "NULLIF" in clause.upper(), (
        f"an empty language overwrites a stored one: {clause!r}"
    )


@pytest.mark.parametrize("status", ["planned", "answered"])
def test_both_outcomes_a_person_reads_carry_the_language(status: str):
    """`answered` is a read, whose whole result is rendered from locale text.
    `planned` is also the path a sentence nobody understood takes — steps is
    empty, and the page answers it with the canned capability list. Those are
    the two replies made of written-down strings, so those are the two that
    must know which language to be written down in.
    """
    handler = _function(HANDLERS_SRC, "handle_automation_plan")
    for call in ast.walk(handler):
        if not (isinstance(call, ast.Call)
                and getattr(call.func, "id", None) == "_finish_automation_plan"):
            continue
        keywords = {k.arg: k.value for k in call.keywords}
        got = keywords.get("status")
        if got is None or ast.literal_eval(got) != status:
            continue
        assert "language" in keywords, (
            f"the {status} outcome is written without a language"
        )
        return
    raise AssertionError(f"no _finish_automation_plan(status={status!r}) call")


def test_the_run_the_client_polls_carries_the_language():
    """Both endpoints in the task go through one response model and one
    mapper, so this is `/runs/{id}` and `/history` at once."""
    from src.api.routers.automation import RunOut

    field = RunOut.model_fields.get("language")
    assert field is not None, "the client is never told what language to use"
    assert field.annotation is str
    assert RunOut(id="r", message="m", status="reading").language == "", (
        "a run still being read must not default to a language nobody reported"
    )


def test_a_row_written_before_this_shipped_is_not_a_crash():
    """The column is nullable and every existing row has NULL in it. Handed
    straight to a `str` field that is a 500 on the history endpoint — for
    every workspace that used the agent before today."""
    from types import SimpleNamespace

    from src.api.routers.automation import _as_run

    old = SimpleNamespace(
        id="r1", session_id=None, message="що ти вмієш", status="planned",
        steps=[], note="", language=None, resolved_repos=None, blocked=None,
        result=None, error=None, user_email=None, created_at=None,
        executed_at=None,
    )
    assert _as_run(old).language == ""


# ─── explain: a read, and only a topic ───────────────────────────────


def test_explain_is_in_the_catalogue():
    """The model is only ever shown the catalogue. A verb that is implemented
    and not listed is a verb no sentence can reach — which is how
    `get_dep_audit` sat unreachable in this codebase for a release."""
    from src.automation.chat import CATALOGUE

    assert "explain" in CATALOGUE
    summary = CATALOGUE["explain"]["summary"].lower()
    assert "celmis" in summary and "capabilit" in summary, (
        "the summary does not tell the model which questions route here"
    )


def test_explain_answers_without_a_second_press():
    """It is a question. Putting a plan card in front of somebody so they can
    approve "tell me what this product is" is a form with one button — and it
    is the exact failure this verb was added to remove, where asking about the
    product produced a refusal with an approval box under it."""
    from src.automation.chat import CATALOGUE, _parse

    assert CATALOGUE["explain"].get("reads") is True
    plan = _parse('{"steps": [{"action": "explain", '
                  '"arguments": {"topic": "capabilities"}}]}')
    assert plan.reads_only is True


def test_explain_has_no_scope_to_approve():
    """A read has no fan-out and no cap. If `resolve_scope` treated it as
    work, asking what Celmis is would resolve every repository in the
    workspace and could be blocked by a ceiling that has nothing to do with
    the question."""
    from src.automation.chat import CATALOGUE, Plan, Step, resolve_scope

    assert set(CATALOGUE["explain"]["arguments"]) == {"topic"}, (
        "explain grew an argument that is not the topic"
    )
    plan = resolve_scope(
        Plan(steps=[Step(action="explain", arguments={"topic": "product"})]),
        workspace_id="ws-nonexistent")
    assert plan.steps[0].resolved_repos == []
    assert plan.blocked is None, (
        "a question about the product was refused for lack of repositories"
    )


@pytest.mark.parametrize("asked,answered", [
    ("product", "product"),
    ("capabilities", "capabilities"),
    ("  Capabilities  ", "capabilities"),
    # Anything else is a key the client renders nothing for.
    ("pricing", "product"),
    ("", "product"),
    (None, "product"),
    ("Product; DROP TABLE", "product"),
])
def test_the_topic_is_validated_here_rather_than_at_the_screen(asked, answered):
    """The model picks the topic, so the model can pick a topic that does not
    exist. Passed through, it reaches the page as a string nothing matches —
    and an unknown topic renders as an empty reply to a question somebody
    actually asked, which reads as the agent ignoring them."""
    from src.automation.actions import Actor
    from src.automation.chat import Plan, Step, execute

    arguments = {} if asked is None else {"topic": asked}
    plan = Plan(steps=[Step(action="explain", arguments=arguments)])
    actor = Actor(user_id="u", email="a@b.c", workspace_id="ws", label="chat")
    # No session: a verb that answers out of a locale file must not need one.
    result = asyncio.run(execute(plan, actor, session=None))

    assert result["steps"] == [{"action": "explain",
                                "result": {"topic": answered}}]


def test_the_answer_is_the_topic_and_nothing_else():
    """The entire reason this verb exists. An `explain` that returned prose
    would be paying a model to recite a paragraph that is already written out
    in sixteen languages — and it would return it in ONE of them."""
    from src.automation.actions import Actor
    from src.automation.chat import Plan, Step, execute

    plan = Plan(steps=[Step(action="explain", arguments={"topic": "product"})])
    outcome = asyncio.run(execute(
        plan, Actor(user_id="u", email="a@b.c", workspace_id="ws", label="chat"),
        session=None))
    assert set(outcome["steps"][0]["result"]) == {"topic"}, (
        "explain returns something other than the topic — the prose is "
        "supposed to be free"
    )


def test_reading_explain_costs_no_model_call():
    """It is a locale lookup with a switch in front of it. A call into the
    LLM client, or into the vault, would make the cheapest question on the
    surface one of the priced ones."""
    execute_fn = _function(CHAT_SRC, "execute")
    branch = ast.unparse(execute_fn)
    branch = branch[branch.index("'explain'"):]
    branch = branch[:branch.index("elif")]
    # Guards the slice: an empty branch would satisfy every check below.
    assert "topic" in branch and len(branch) > 60, branch
    for forbidden in ("build_llm_client", "generate(", "await "):
        assert forbidden not in branch, (
            f"the explain branch calls {forbidden} — it is meant to be a "
            f"topic and a lookup"
        )


def test_the_server_never_writes_the_product_prose():
    """The same rule the capabilities message already lives by, extended to
    the other half of `explain`. Prose on the server is either a model call or
    sixteen translations living somewhere the translators do not look."""
    offenders = []
    for path in SRC.rglob("*.py"):
        raw = path.read_text(encoding="utf-8")
        if "automation.product" not in raw:
            continue  # a cheap filter; the strip below is the real check
        if "automation.product" in _python_source(path):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"the product description is built on the server in {offenders}"


# ─── the canned lists still describe the catalogue ───────────────────


def test_no_locale_advertises_a_verb_the_catalogue_no_longer_has():
    """The drift that matters is the outward one.

    A verb added to CATALOGUE and missing from the message is a capability
    nobody is told about — a waste. A capability line left behind after the
    verb is gone is worse: the page says it out loud, somebody types it, and
    the model — which is only ever handed the catalogue — refuses. Confidently
    wrong beats absent, in the wrong direction.
    """
    from src.automation.chat import CATALOGUE

    stale = {}
    for locale in LOCALES:
        data = _locale(locale)
        orphans = sorted(
            key for key in data
            if key.startswith("automation.capabilities.")
            and key.rsplit(".", 1)[1] not in CAPABILITY_FURNITURE
            and key.rsplit(".", 1)[1] not in CATALOGUE
        )
        if orphans:
            stale[locale] = orphans
    assert not stale, f"capability lines with no verb behind them: {stale}"


def test_the_action_labels_do_not_outlive_their_verbs_either():
    """The plan card and the reply header are titled `automation.action.<verb>`.
    A label with no verb is dead weight; the reverse is a raw key rendered
    into a heading, which the neighbouring suite already pins."""
    from src.automation.chat import CATALOGUE

    orphans = sorted(key for key in EN
                     if key.startswith("automation.action.")
                     and key.rsplit(".", 1)[1] not in CATALOGUE)
    assert not orphans, f"labels for verbs that no longer exist: {orphans}"


def test_explain_is_advertised_as_a_question_in_every_locale():
    """It is the verb somebody reaches for when they do not know what any of
    the others mean — the first line of the first reply they will ever read.
    A missing key here renders `automation.capabilities.explain` into the list
    of things the product can do."""
    for locale in LOCALES:
        data = _locale(locale)
        for key in ("automation.action.explain",
                    "automation.capabilities.explain"):
            assert data.get(key, "").strip(), f"{locale} has no {key}"
    assert "explain" in _array(CODE, "READS"), (
        "explain is not listed among the questions answered straight away"
    )


@pytest.mark.parametrize("locale", LOCALES)
def test_the_product_description_exists_in_every_locale(locale: str):
    """`explain` returns a topic because the prose is canned. Canned in
    English only is the same model call with extra steps — the person asks in
    Polish and the reply falls back to an English paragraph, which is the bug
    the whole language field was added to fix.
    """
    data = _locale(locale)
    missing = [key for key in PRODUCT_KEYS if not data.get(key, "").strip()]
    assert not missing, f"{locale}.json cannot answer 'what is Celmis': {missing}"


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_no_locale_reads_the_product_description_back_in_english(locale: str):
    """A key copied out of `en.json` to satisfy a completeness check is
    present and useless: it passes every test about missing keys and delivers
    the exact failure this feature exists to remove."""
    data = _locale(locale)
    echoed = [key for key in PRODUCT_KEYS if data.get(key) == EN[key]]
    assert not echoed, f"{locale} carries the English text for: {echoed}"


def test_the_page_renders_every_paragraph_it_has_a_string_for():
    """A paragraph translated into sixteen languages and never rendered is
    sixteen translations paid for and dropped."""
    listed = set(_array(CODE, "PRODUCT"))
    written = {key for key in EN if key.startswith("automation.product.")}
    unrendered = written - listed - {"automation.product.title",
                                     "automation.product.scope"}
    assert not unrendered, f"translated and never shown: {sorted(unrendered)}"
    product_body = _squash(_body(CODE, "Product"))
    for key in ("automation.product.title", "automation.product.scope"):
        assert key in product_body, f"{key} is written down and never rendered"


# ─── and it is rendered in the language of the question ──────────────


@pytest.mark.parametrize("component", ["Capabilities", "Product", "Answer"])
def test_the_canned_blocks_read_the_language_of_the_question(component: str):
    """These three are entirely written-down text: they are what the reply IS.
    `useT()` here would render them in whatever the switcher says, which is
    how a Ukrainian question ended up answered by a Ukrainian sentence with an
    English panel under it."""
    body = _squash(_body(CODE, component))
    assert "useDictFor(" in body, (
        f"{component} is canned text rendered in the interface language"
    )
    assert not re.search(r"\buseT\(\)", body), (
        f"{component} takes its strings from the switcher, not the question"
    )


def test_the_reply_hands_the_question_language_to_every_canned_part():
    """One run, two dictionaries. Missing the prop on either child is a block
    that silently falls back — and falls back to something that looks
    perfectly fine to whoever is reviewing it in English."""
    reply = _squash(_body(CODE, "Reply"))
    assert "useDictFor(run.language)" in reply, (
        "the reply has no dictionary for the language it was asked in"
    )
    rendered = _tags(reply)
    assert {"Capabilities", "Answer"} <= {name for name, _ in rendered}
    for name, tag in rendered:
        assert "language={run.language}" in tag, (
            f"<{name} is rendered without the question's language"
        )


def _explain_branch() -> str:
    """What `Answer` renders for `explain`, squashed."""
    answer = _squash(_body(CODE, "Answer"))
    branch = answer[answer.index('s.action === "explain"'):]
    return branch[:branch.index('s.action === "list_repos"')]


def _tags(code: str) -> list[tuple[str, str]]:
    """[(component, the whole opening tag)] for every canned block rendered.

    The tag rather than the next N characters: with a window, a block that
    lost the prop reads the NEXT block's, and the check passes by looking at
    the wrong element. Depth-counted because a prop value is braces and may
    contain a `>` of its own.
    """
    out = []
    for m in re.finditer(r"<([A-Z]\w*)", code):
        if m.group(1) not in CANNED_BLOCKS:
            continue
        depth = 0
        for i in range(m.start(), len(code)):
            if code[i] == "{":
                depth += 1
            elif code[i] == "}":
                depth -= 1
            elif code[i] == ">" and depth == 0:
                out.append((m.group(1), code[m.start():i + 1]))
                break
        else:
            raise AssertionError(f"unterminated <{m.group(1)}")
    return out


def _canned_in(branch: str) -> list[str]:
    """The written-down blocks the branch renders, in source order.

    Derived from which components read a dictionary rather than listed by
    name: `explain` grows topics, and a list would have to be edited by
    whoever adds one — which is precisely the person who would rather not
    know that a prop is missing.
    """
    return [name for name, _ in _tags(branch)]


def test_the_answer_passes_it_down_to_the_explain_blocks():
    """`Answer` is a step renderer; the blocks it can return for `explain` are
    the canned ones. Dropping the prop one level down is invisible in every
    language the reviewer reads."""
    branch = _explain_branch()
    blocks = _canned_in(branch)
    assert {"Capabilities", "Product"} <= set(blocks), (
        f"the page renders {blocks} for explain — a topic lost its paragraph"
    )
    for name, tag in _tags(branch):
        assert "language={language}" in tag, (
            f"<{name} is rendered without the question's language"
        )


def test_an_unrecognised_topic_still_renders_something():
    """Belt and braces with the server-side clamp: a topic that got past
    validation must land on a paragraph rather than on nothing.

    Said as arms and guards rather than as a ternary, because it is true of
    both spellings: one arm is reached without comparing the topic to
    anything, and it is the product description — the answer to the broadest
    version of the question.
    """
    branch = _explain_branch()
    blocks = _canned_in(branch)
    assert branch.count("r.topic ===") == len(blocks) - 1, (
        "every arm of the topic branch is guarded — an unknown topic renders "
        "nothing"
    )
    assert blocks[-1] == "Product", "Product is no longer the fallback arm"


def test_the_furniture_stays_in_the_language_the_person_chose():
    """Only the CONTENT of a reply follows the question. A Send button that
    changed language per message would be absurd, and the session list is
    about the person's own history rather than about any one sentence."""
    reply = _squash(_body(CODE, "Reply"))
    assert "const t = useT();" in reply, (
        "the reply's buttons have no interface dictionary"
    )
    for key in ("automation.stop", "automation.stopping", "automation.reading"):
        assert f't("{key}")' in reply, (
            f"{key} is a control, and it is being rendered in the question's "
            f"language"
        )
    page = _squash(_body(CODE, "AutomationPage"))
    assert "useDictFor(" not in page, (
        "the composer and the session list follow the question's language"
    )


def test_a_language_with_no_dictionary_falls_back_instead_of_showing_keys():
    """Three ways this is reached and all of them are normal: the code is
    empty, the dictionary is still crossing the network, or the planner named
    a language there is no file for. None of them may render
    `automation.product.index` into a paragraph."""
    body = _squash(_body(I18N, "useDictFor"))
    assert body.count("return active(key, vars);") == 2, (
        "an unknown language, or one still loading, does not fall back to the "
        "active locale"
    )
    as_locale = _squash(_body(I18N, "asLocale"))
    assert "LOCALES" in as_locale and "null" in as_locale, (
        "any two-letter string is treated as a locale"
    )


def test_the_named_dictionary_is_not_the_interface_one():
    """Asking for the active locale through this path would fetch a dictionary
    the provider already has — a second copy of ~170 KB per reply, and a
    render loop's worth of state churn."""
    body = _squash(_body(I18N, "useDictFor"))
    assert "target === locale" in body, (
        "a reply in the interface language takes the slow path"
    )
