"""Three surfaces, three different answers to "what language is this in".

They are genuinely different questions and the product gets them wrong in
different ways, so they are pinned together where the difference is visible:

  documentation  → the workspace setting. A repository's docs belong to the
                   repository, not to whoever pressed Generate.
  Q&A answers    → the language of the question. Somebody who asks in German is
                   asking to be answered in German, and a workspace-wide
                   default would be wrong for whoever else is in the room.
  PR comments    → the review_language setting. Read by outside contributors on
                   GitHub, so the team's own language is often the wrong one.

Every prompt used to hardcode Ukrainian, which answered all three at once and
all three incorrectly.

The verifier is the exception that has to stay an exception: its output is
parsed as JSON and never shown to anyone, so telling it to write in Ukrainian
would corrupt a machine-read response. It works today by omission rather than
by decision, which is exactly the kind of thing somebody tidies up.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

CYRILLIC = re.compile(r"[а-яїієґА-ЯЇІЄҐ]")


def _prompt_strings(path: Path) -> list[str]:
    """Every string literal in the file that is not a docstring."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
               and isinstance(body[0].value, ast.Constant) \
               and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


# ─── Q&A: the question's language ────────────────────────────────────

QA_PROMPTS = ["ba_answer.py", "technical_answer.py", "overview.py"]


@pytest.mark.parametrize("module", QA_PROMPTS)
def test_a_chat_answer_follows_the_question(module):
    """The answer's language is tied to the question, not to a setting and not
    to the language of the surrounding context.

    This used to pin the rule's exact sentence — "answer in the SAME LANGUAGE
    the question is asked in" — and broke when that sentence was rewritten to
    stop naming German as its only worked example, which was itself the fix
    for an English question coming back in German on production. A test that
    fails when the code improves is keyed on the wrong thing.

    Keyed now on what any correct phrasing must contain: somewhere in the
    prompt, the answer's LANGUAGE is spoken about in the same breath as the
    QUESTION. Read from the AST constants, so the comment that explains all of
    this cannot satisfy the assertion by accident.
    """
    joined = "\n\n".join(_prompt_strings(SRC / "llm" / "prompts" / module))
    tied = [
        block for block in joined.split("\n\n")
        if "language" in block.lower() and "question" in block.lower()
    ]
    assert tied, (
        f"{module} never says the answer's language follows the question — "
        f"no passage mentions both"
    )


@pytest.mark.parametrize("module", QA_PROMPTS)
def test_no_chat_prompt_pins_one_language(module):
    """"Пишеш українською" answered a German question in Ukrainian."""
    for s in _prompt_strings(SRC / "llm" / "prompts" / module):
        low = s.lower()
        for phrase in ("пишеш українською", "українська мова", "in ukrainian",
                       "write in english", "answer in english"):
            assert phrase not in low, f"{module} pins the answer to one language"


def test_qa_does_not_go_through_the_documentation_setting():
    """`with_language` is for documentation. Wiring Q&A through it would make a
    workspace setting override what the person in front of it just asked."""
    qa = list((SRC / "qa").rglob("*.py"))
    offenders = [p.name for p in qa if "with_language" in p.read_text(encoding="utf-8")]
    assert not offenders, f"Q&A routed through the documentation language: {offenders}"


def test_the_classifier_still_understands_both_languages():
    """It routes a question to a prompt by regex. Translating those patterns
    would stop Ukrainian questions matching anything — the one translation in
    this codebase that would break a feature rather than reword it."""
    from src.qa.classifier import QuestionType, classify

    assert classify("як реалізовано авторизацію?") is QuestionType.TECHNICAL
    assert classify("how is auth implemented?") is QuestionType.TECHNICAL
    assert classify("де знайти конфіг?") is QuestionType.NAVIGATION
    assert classify("where is the config?") is QuestionType.NAVIGATION


def test_an_unmatched_language_degrades_instead_of_failing():
    """German matches no pattern. It must land on a usable type rather than
    raise — the answer will still come back in German."""
    from src.qa.classifier import QuestionType, classify

    assert classify("Wie funktioniert die Authentifizierung?") is QuestionType.FUNCTIONAL


# ─── review: the review_language setting ─────────────────────────────


def test_review_output_language_is_still_wired():
    base = (SRC / "review" / "agents" / "base.py").read_text(encoding="utf-8")
    assert "_review_language_instruction" in base
    assert '"review_language"' in base, "the setting is no longer read"
    # And it reaches the agents through the one composition point.
    assert "_compose_effective_system_prompt" in base


def test_english_adds_no_instruction():
    """The prompts are English now, so the default costs nothing. An
    instruction saying "write in English" on every review would be tokens spent
    to restate the obvious."""
    from src.review.agents.base import _REVIEW_LANG_NAMES

    assert _REVIEW_LANG_NAMES["en"] == "English"
    base = (SRC / "review" / "agents" / "base.py").read_text(encoding="utf-8")
    assert 'if code == "en":' in base


def test_the_claude_engine_gets_the_same_instruction():
    """Two review engines, one setting. The subscription path had to be wired
    separately and can silently fall behind.

    This used to pin the name of the helper the engine called. The engine now
    resolves the language itself (the repository's, else the workspace's: it
    read the workspace alone, so a repository that chose Ukrainian got an
    English review) and builds its directive from the one shared module, so it
    is keyed on that: the engine takes the repository's language, and the
    orchestrator hands it over.
    """
    import inspect

    from src.review.claude_engine import run_claude_review

    assert "language" in inspect.signature(run_claude_review).parameters
    engine = (SRC / "review" / "claude_engine.py").read_text(encoding="utf-8")
    assert "effective_language" in engine and "finding_directive" in engine
    orchestrator = (SRC / "review" / "orchestrator.py").read_text(encoding="utf-8")
    call = orchestrator[orchestrator.index("cr = run_claude_review("):][:2500]
    assert "language=" in call, "the repository's language never reaches the engine"


def test_the_verifier_is_deliberately_left_out():
    """Its response is parsed as JSON — `keep` indices — and shown to nobody.
    A language instruction there risks corrupting a machine-read reply for no
    benefit, so the omission is correct and must stay."""
    verifier = (SRC / "review" / "agents" / "verifier.py").read_text(encoding="utf-8")
    assert "_review_language_instruction" not in verifier, (
        "the verifier now asks for prose in a chosen language, but its answer "
        "is parsed as JSON and never read by a person"
    )
    assert '"keep"' in verifier


# ─── documentation: the workspace setting ────────────────────────────


def test_documentation_still_goes_through_the_workspace_setting():
    for module in ("module_prd.py", "feature_doc.py", "integration_doc.py"):
        text = (SRC / "generation" / module).read_text(encoding="utf-8")
        assert "with_language(" in text, f"{module} lost the language directive"


# ─── the translation itself ──────────────────────────────────────────

TRANSLATED = [
    "src/llm/prompts/ba_answer.py", "src/llm/prompts/feature.py",
    "src/llm/prompts/integration.py", "src/llm/prompts/module_prd.py",
    "src/llm/prompts/overview.py", "src/llm/prompts/technical_answer.py",
    "src/qa/exploration_agent.py", "src/qa/multi_repo_retriever.py",
    "src/review/agents/contract.py", "src/review/agents/defect.py",
    "src/review/agents/security.py", "src/review/agents/verifier.py",
]


def _is_prose(s: str) -> bool:
    """Is this string prose sent to a model, or data keyed on a language?

    The distinction is the whole point. Two kinds of Ukrainian survive in this
    codebase on purpose:

      * regex patterns that recognise a Ukrainian question and route it to the
        right prompt (src/qa/classifier.py);
      * stopwords filtered out before identifiers are pulled from a question,
        so that "знаходиться" is not searched for as a symbol name
        (src/qa/multi_repo_retriever.py).

    Translate either and a feature stops working for Ukrainian speakers —
    the only translations in this repository that would break behaviour rather
    than reword it. Neither is prose: a prompt is never one word.
    """
    return " " in s.strip() and len(s.strip()) > 30


@pytest.mark.parametrize("path", TRANSLATED)
def test_no_ukrainian_is_left_in_a_prompt(path):
    """Part of this is going open source. A prompt half in Ukrainian is worse
    than one entirely in it — a reader cannot tell which half they are missing."""
    leftovers = [s for s in _prompt_strings(ROOT / path)
                 if CYRILLIC.search(s) and _is_prose(s)]
    assert not leftovers, (
        f"{path} still has Ukrainian in {len(leftovers)} prompt string(s): "
        f"{leftovers[0][:80]!r}"
    )


def test_the_question_patterns_are_exempt_and_still_ukrainian():
    """The two places that must NOT be translated, named so the exemption is a
    decision on the record rather than an oversight somebody later 'fixes'."""
    patterns = _prompt_strings(SRC / "qa" / "classifier.py")
    assert any(CYRILLIC.search(s) for s in patterns), (
        "the Ukrainian question patterns are gone — Ukrainian questions will "
        "no longer be routed to the right prompt"
    )
    stopwords = _prompt_strings(SRC / "qa" / "multi_repo_retriever.py")
    assert any(CYRILLIC.search(s) for s in stopwords), (
        "the Ukrainian stopwords are gone — common words like 'знаходиться' "
        "will be searched for as if they were symbol names"
    )


# ─── review: the language reaches every prompt that writes review text ──
#
# A review has one language, and the cases below are the paths a person reads
# a model's sentence on: the finders (title, the "why" sentence, body, fix),
# the Claude Code engine, the overview and merged description, the audit
# verdict, and the answer to a question. The bot's fixed words are the message
# catalogue's business and are pinned in tests/review. Each case is run, not
# read: the prompt a model receives is captured and looked at.


def _finder_pr(description: str = "Adds the buyer name to every row of the totals report."):
    from src.review.models import Hunk, PullRequest

    diff = (
        "@@ -10,3 +10,6 @@ def totals(orders):\n"
        "     out = []\n"
        "+    for order in orders:\n"
        "+        user = db.query(User).get(order.user_id)\n"
        "+        out.append((user.name, order.total))\n"
        "     return out\n"
    )
    return PullRequest(
        provider="github", repo="acme/shop", number=12, title="Show buyer names",
        description=description, author="a", base_ref="main", base_sha="x",
        head_ref="feat", head_sha="y", state="open",
        hunks=[Hunk(file_path="app/report.py", old_file_path="app/report.py",
                    old_start=10, old_count=3, new_start=10, new_count=6, content=diff)],
    )


def _finder_calls(agent, language: str | None):
    """Every model call one finder makes on a small pull request."""
    from unittest.mock import MagicMock

    from src.review.agents.base import AgentContext
    from src.review.settings import AgentLLMSettings

    reply = MagicMock()
    reply.text = "[]"
    reply.input_tokens = reply.output_tokens = 10
    reply.cost_usd = 0.0
    reply.cost_source = "litellm_estimate"
    reply.model = "m"
    client = MagicMock()
    client.generate.return_value = reply
    context = AgentContext(
        pull_request=_finder_pr(), llm_client=client, review_language=language,
        workspace_id="ws-lang",
        agent_llm={agent.name: AgentLLMSettings(
            model="m", max_output_tokens=2000, reasoning=None)},
    )
    agent.review(context)
    assert client.generate.call_count >= 1, f"{agent.name} never called the model"
    return [c.kwargs for c in client.generate.call_args_list]


def _finders():
    from src.review.agents.business_logic import BusinessLogicAgent
    from src.review.agents.contract import ContractAgent
    from src.review.agents.defect import DefectAgent
    from src.review.agents.performance import PerformanceAgent
    from src.review.agents.security import SecurityAgent

    return [DefectAgent, ContractAgent, SecurityAgent, PerformanceAgent, BusinessLogicAgent]


@pytest.mark.parametrize("agent_cls", _finders(), ids=lambda c: c.name)
def test_every_finder_is_told_the_language_in_both_turns(agent_cls):
    """The system prompt names it, last; the user turn repeats it, last."""
    from src.llm.prompts.language import NATIVE_PHRASES

    calls = _finder_calls(agent_cls(), "uk")
    for kw in calls:
        system, prompt = kw["system_instruction"], kw["prompt"]
        assert "Ukrainian" in system
        assert system.rstrip().rindex("OUTPUT LANGUAGE") > system.rindex("written LAST"), (
            "the language comes before the output format: the format's English "
            "examples are then the last thing the model reads"
        )
        assert prompt.rstrip().endswith(f"Ukrainian ({NATIVE_PHRASES['uk']}).")


@pytest.mark.parametrize("agent_cls", _finders(), ids=lambda c: c.name)
def test_the_why_sentence_is_named_as_text_a_person_reads(agent_cls):
    """The cause of the English "why": the instruction named titles, bodies and
    suggestions, never the first-written `reasoning` field, which a model reads
    as a scratchpad line and writes in the language of the examples."""
    system = _finder_calls(agent_cls(), "uk")[0]["system_instruction"]
    directive = system[system.rindex("OUTPUT LANGUAGE"):]
    assert '"reasoning"' in directive
    assert "not a scratchpad" in directive
    assert "the examples" in directive and "not its language" in directive


@pytest.mark.parametrize("agent_cls", _finders(), ids=lambda c: c.name)
def test_the_fields_a_program_reads_stay_as_they_are(agent_cls):
    """`input_size` is read by English patterns and `severity` is an enum."""
    system = _finder_calls(agent_cls(), "uk")[0]["system_instruction"]
    directive = system[system.rindex("OUTPUT LANGUAGE"):]
    for key in ('"severity"', '"rule_id"', '"input_size"', '"confidence"'):
        assert key in directive


@pytest.mark.parametrize("agent_cls", _finders(), ids=lambda c: c.name)
def test_english_adds_nothing_to_either_turn(agent_cls):
    for kw in _finder_calls(agent_cls(), "en"):
        assert "OUTPUT LANGUAGE" not in kw["system_instruction"]
        assert "Write every sentence" not in kw["prompt"]


def test_the_workspace_language_applies_when_the_repository_chose_none(monkeypatch):
    import src.api.routers.llm as llm_router
    from src.review.agents.defect import DefectAgent

    monkeypatch.setattr(
        llm_router, "_load_workspace_config", lambda ws: {"review_language": "de"})
    kw = _finder_calls(DefectAgent(), None)[0]
    assert "German" in kw["system_instruction"]
    assert "in German" in kw["prompt"].rstrip().rsplit("Write every sentence", 1)[1]


def test_a_language_without_a_catalogue_still_reaches_the_model():
    """The bot's own words come in two languages; the model can write in 37."""
    from src.review.agents.defect import DefectAgent

    kw = _finder_calls(DefectAgent(), "bg")[0]
    assert "Bulgarian" in kw["system_instruction"]
    assert "Bulgarian" in kw["prompt"]


def test_the_overview_and_the_merged_description_are_asked_in_the_language():
    from src.llm.prompts.language import NATIVE_PHRASES
    from src.review.pr_summary import build_prompt

    prompt = build_prompt(_finder_pr(), language="uk")
    assert f"Ukrainian ({NATIVE_PHRASES['uk']})" in prompt
    # Not only the languages the summary used to know about.
    assert "Bulgarian" in build_prompt(_finder_pr(), language="bg")
    assert "English" in build_prompt(_finder_pr(), language=None)


def test_the_merged_description_is_asked_in_the_language():
    from types import SimpleNamespace

    from src.review.pr_summary import complement_description

    seen: dict = {}

    def generate(**kw):
        seen.update(kw)
        return SimpleNamespace(text="merged", input_tokens=1, output_tokens=1, cost_usd=0.0)

    complement_description(
        _finder_pr(), "Author text.", "Our summary.",
        llm_client=SimpleNamespace(generate=generate), language="pl")
    assert "Polish" in seen["prompt"]


def test_the_audit_verdict_is_asked_for_in_the_language():
    """`reason` is the one sentence of a compliance verdict a person reads."""
    from src.review.agents.base import AgentContext
    from src.review.compliance import _SYSTEM, compliance_system_prompt

    prompt = compliance_system_prompt(
        AgentContext(pull_request=_finder_pr(), review_language="uk", workspace_id="ws-lang"))
    assert prompt.startswith(_SYSTEM.rstrip())
    rider = prompt.rstrip().rsplit("OUTPUT LANGUAGE", 1)[1]
    assert 'Write "reason" in Ukrainian' in rider
    assert '"reason"' in prompt and '"passes"' in prompt
    plain = compliance_system_prompt(
        AgentContext(pull_request=_finder_pr(), review_language="en", workspace_id="ws-lang"))
    assert plain == _SYSTEM


def test_the_answer_to_a_question_is_written_in_the_real_language(monkeypatch):
    """The bot's own words have two catalogues, so the command context holds
    "en" for German; the model is told German all the same."""
    from types import SimpleNamespace

    from src.review import memories
    from src.review.commands import chat, handlers
    from src.review.commands.handlers import CommandContext
    from src.review.commands.parser import parse_comment
    from tests.review.comment_support import FakeProvider, event

    calls: list[dict] = []
    fake = SimpleNamespace(generate=lambda **kw: calls.append(kw) or SimpleNamespace(text="ok"))
    monkeypatch.setattr(chat, "_client", lambda _ctx: fake)
    monkeypatch.setattr(chat, "enforce", lambda _ws: None)
    monkeypatch.setattr(memories, "render_for_chat", lambda *a, **k: "")
    body = "@celmis why is the total wrong?"
    ctx = CommandContext(
        ev=event(body=body), provider=FakeProvider(), command=parse_comment(body, "@celmis"),
        settings={}, workspace_id="ws-a", user_id="u-1", language="en", prose_language="de")
    handlers._REGISTRY["chat"](ctx)
    assert "German" in calls[0]["system_instruction"]


def test_the_claude_engine_prompt_carries_the_language(monkeypatch):
    """Run through a stand-in for the SDK: the prompt the `claude` session is
    sent ends with the directive and the reminder, in the repository's
    language, and names the fields that reply has (no `reasoning`)."""
    import sys
    import types

    import src.agent.connection as conn_mod
    import src.agent.runner as runner_mod
    from src.agent.connection import ClaudeConnection
    from src.llm.prompts.language import NATIVE_PHRASES
    from src.review import claude_engine as ce

    queries: list[str] = []

    class _Block:
        def __init__(self, text):
            self.text = text

    class _Assistant:
        def __init__(self, text):
            self.content = [_Block(text)]

    class _Result:
        num_turns = 1
        total_cost_usd = 0.1
        is_error = False
        result = ""
        usage = {"input_tokens": 10, "output_tokens": 10}
        model = "claude-sonnet-4-6"

    class _Client:
        def __init__(self, options=None):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            queries.append(prompt)

        async def receive_response(self):
            yield _Assistant('```json\n{"summary": "ok", "findings": []}\n```')
            yield _Result()

    module = types.ModuleType("claude_agent_sdk")
    module.AssistantMessage, module.TextBlock = _Assistant, _Block
    module.ResultMessage, module.ClaudeAgentOptions = _Result, lambda **kw: kw
    module.ClaudeSDKClient = _Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    monkeypatch.setattr(runner_mod, "_mint_mcp_token", lambda user_id, workspace_id="default": "t")
    monkeypatch.setattr(
        conn_mod, "resolve_connection",
        lambda user_id, ws: ClaudeConnection(token="sk-ant-oat-placeholder", source="personal",
                                             saved_by=None))
    monkeypatch.setattr(ce, "record_claude_code_spend", lambda message, **kw: None)

    pr = _finder_pr()
    pr.raw_diff = "--- a/app/report.py\n+++ b/app/report.py\n@@ -1 +1,2 @@\n x\n+y\n"
    result = ce.run_claude_review(
        pr, user_id="u1", workspace_id="ws-lang", language="uk")

    assert result.error is None
    [prompt] = queries
    assert "Ukrainian" in prompt[prompt.rindex("OUTPUT LANGUAGE"):]
    directive = prompt[prompt.rindex("OUTPUT LANGUAGE"):]
    assert '"summary"' in directive and '"reasoning"' not in directive
    assert prompt.rstrip().endswith(f"Ukrainian ({NATIVE_PHRASES['uk']}).")
