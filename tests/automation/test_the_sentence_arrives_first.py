"""The sentence is written before the plan, and reaches the row while it is.

Measured on production: POST /api/automation/plan returns in 0.2s because it
only enqueues, and the row reaches a terminal status 1.8-5.5s later. The page
polls every 1200ms on top of that. So a person watched a spinner for three to
seven seconds with nothing on screen, and said so: "ніби уся відповідь
генерується та потім показується".

Streaming the plan token by token does not fix that, because the plan is JSON:
it would put `{"steps": [{"action": "expl` on screen, which is a spinner plus
noise. What fixes it is the ORDER of the fields. `note` is the one field a
person can read, so the model is asked to write it SECOND — after the two
tokens of `language`, before the steps — and the sentence is complete about a
second in while the plan is still being generated.

That is a prompt-shaped feature, which is the dangerous kind: reordering the
fields to look tidier deletes it entirely and every parser test still passes,
because `_parse` does not care what order the keys arrive in. This file is
what notices.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAT = ROOT / "src" / "automation" / "chat.py"
HANDLERS = ROOT / "src" / "sync" / "handlers.py"


def _assignment(source: str, function: str, name: str) -> str:
    """The value assigned to `name` inside `function`, comments stripped.

    Via the AST rather than the text, so a comment that happens to mention the
    field names cannot make this pass — the failure mode is a prompt whose
    order was quietly changed while the paragraph above it still describes the
    old one.
    """
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == function)
    node = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == name for t in n.targets))
    # The prompt is implicit concatenation with f-strings in it, so this
    # collects the literal halves and ignores the interpolations — the JSON
    # contract is literal text.
    return "".join(
        part.value for part in ast.walk(node.value)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )


# ─── the order, in the two places it is stated ───────────────────────


def test_the_prompt_asks_for_the_readable_field_before_the_machine_one():
    """What breaks: three to seven seconds of blank spinner, again.

    `note` last means the sentence exists only once every step, argument and
    repository list has been generated — which is the whole latency the
    streaming is for.
    """
    prompt = _assignment(CHAT.read_text(encoding="utf-8"), "interpret", "prompt")

    language, note, steps = (prompt.index(f'"{f}"')
                             for f in ("language", "note", "steps"))
    assert language < note < steps, (
        "the planner's JSON contract no longer asks for language, then note, "
        "then steps. A person sees `note` and nothing else, so a note written "
        "after the steps is a spinner for the whole reading."
    )


def test_the_system_prompt_says_it_too():
    """Two statements of the same rule, on purpose: models weight the system
    prompt and the request differently, and this one is cheap to say twice."""
    from src.automation.chat import _SYSTEM

    for field in ("`language`", "`note`", "`steps`"):
        assert field in _SYSTEM, f"the system prompt no longer names {field}"
    assert (_SYSTEM.index("`language`") < _SYSTEM.index("`note`")
            < _SYSTEM.index("`steps`")), (
        "the system prompt states a different field order from the request — "
        "the model will pick one of them and it may not be this one"
    )


def test_the_plan_declares_its_fields_in_the_order_it_receives_them():
    """The class is read far more often than the prompt. Declared as
    steps-first it invites exactly the tidy-up this whole file exists to
    prevent."""
    from src.automation.chat import Plan

    names = [f.name for f in dataclasses.fields(Plan)]
    assert names[:3] == ["language", "note", "steps"], names


# ─── reading a sentence out of half an object ────────────────────────


@pytest.mark.parametrize("buffer,expected", [
    # The case this exists for: the object is unfinished BY DEFINITION, so
    # json.loads raises and goes on raising until the moment it is no longer
    # needed.
    ('{"language": "uk", "note": "Читаю запит', "Читаю запит"),
    ('{"language":"uk","note":"Done.","steps":[]}', "Done."),
    # A backslash whose partner has not arrived yet, and half a \uXXXX — both
    # are decode errors one chunk before they become a character.
    ('{"note":"a\\', "a"),
    ('{"note":"a\\u04', "a"),
    ('{"note":"a\\u0456', "aі"),
    ('{"note":"say \\"hi\\" now"}', 'say "hi" now'),
    # Nothing to show yet is a fine answer — the spinner is still honest.
    ('{"language":"uk"', ""),
    ("", ""),
    # A model that ignores the order and writes steps first: the first note
    # found is a step's note, which is still a sentence about the work.
    ('{"steps":[{"action":"explain","note":"step note"}]', "step note"),
])
def test_the_note_is_read_out_of_unfinished_json(buffer: str, expected: str):
    from src.automation.chat import _partial_note

    assert _partial_note(buffer) == expected


def test_a_finished_note_still_parses_as_json_would_have():
    """The tolerant scan must not disagree with the real parser on a complete
    object — two readings of one field is a bug waiting for a Unicode
    escape."""
    from src.automation.chat import _partial_note

    payload = {"language": "uk", "note": 'він сказав "так"\nі пішов',
               "steps": []}
    text = json.dumps(payload, ensure_ascii=False)
    assert _partial_note(text) == payload["note"]
    assert _partial_note(json.dumps(payload)) == payload["note"]


# ─── interpret streams, and only when somebody is listening ──────────


ANSWER = ('{"language": "uk", "note": "Вмикаю рев\'ю для двох репозиторіїв.", '
          '"steps": [{"action": "set_auto_review", "arguments": '
          '{"repo_slugs": ["a/b"], "enabled": true}, "note": "arm"}]}')


def _fake_client(monkeypatch, *, answer: str = ANSWER, chunk: int = 6):
    """A client that feeds `answer` to on_delta a few characters at a time."""
    seen: dict = {"kwargs": None, "prefixes": []}

    def _generate(**kwargs):
        seen["kwargs"] = kwargs
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            for i in range(chunk, len(answer) + chunk, chunk):
                seen["prefixes"].append(answer[:i])
                if on_delta(answer[:i]) is False:
                    return types.SimpleNamespace(text=answer[:i])
        return types.SimpleNamespace(text=answer)

    import src.llm.client as llm_client
    monkeypatch.setattr(
        llm_client, "build_llm_client",
        lambda *a, **kw: types.SimpleNamespace(generate=_generate))
    return seen


def test_the_sentence_is_reported_before_the_plan_is_finished(monkeypatch):
    """What breaks: the reading is streamed and nobody is told, which is the
    same blank spinner with a bigger changelog."""
    from src.automation.chat import interpret

    seen = _fake_client(monkeypatch)
    notes: list[str] = []
    plan = interpret("увімкни рев'ю", workspace_id="ws", user_id="u1",
                     on_note=notes.append)

    assert notes, "the sentence was never reported while it was being written"
    assert notes[-1] == "Вмикаю рев'ю для двох репозиторіїв."
    # …and it was complete well before the plan was.
    finished_at = next(i for i, n in enumerate(notes) if n == notes[-1])
    assert finished_at < len(seen["prefixes"]) * 0.6, (
        "the sentence only completed at the end of the stream — the field "
        "order stopped working"
    )
    # The plan itself is unchanged by any of this.
    assert [s.action for s in plan.steps] == ["set_auto_review"]
    assert plan.language == "uk"


def test_a_caller_that_wants_nothing_makes_the_call_it_always_did(monkeypatch):
    """Streaming is opt-in per call. The CLI and every existing test take the
    single blocking call, so there is one path fewer to be wrong."""
    from src.automation.chat import interpret

    seen = _fake_client(monkeypatch)
    interpret("увімкни рев'ю", workspace_id="ws", user_id="u1")
    assert seen["kwargs"]["on_delta"] is None


def test_the_planner_still_reads_with_a_ceiling_and_one_retry(monkeypatch):
    """The reason those numbers exist has not changed: a person is watching a
    spinner, and 120s × 3 retries is up to eight minutes of "Reading…"."""
    from src.automation.chat import interpret

    seen = _fake_client(monkeypatch)
    interpret("увімкни рев'ю", workspace_id="ws", user_id="u1",
              on_note=lambda _n: None)
    assert seen["kwargs"]["timeout"] == 20
    assert seen["kwargs"]["num_retries"] == 1


def test_stop_interrupts_the_model_rather_than_waiting_for_it(monkeypatch):
    """What breaks: Stop stops the row and pays for the rest of the answer.

    The cancel used to be checked between phases, so pressing it while the
    model was mid-sentence meant waiting for the sentence to finish first.
    """
    from src.automation.chat import interpret

    seen = _fake_client(monkeypatch)
    asked = {"n": 0}

    def _stop() -> bool:
        asked["n"] += 1
        return asked["n"] > 3

    interpret("увімкни рев'ю", workspace_id="ws", user_id="u1",
              should_stop=_stop)
    assert len(seen["prefixes"]) < len(ANSWER) // 6, (
        "the stream ran to the end after a stop was requested"
    )


# ─── the client: same envelope, streamed ─────────────────────────────


def _chunks(text: str, size: int = 8, usage: bool = True):
    N = types.SimpleNamespace
    for i in range(0, len(text), size):
        yield N(choices=[N(delta=N(content=text[i:i + size]),
                           finish_reason=None)], usage=None)
    yield N(choices=[N(delta=N(content=None), finish_reason="stop")], usage=None)
    if usage:
        yield N(choices=[], usage=N(prompt_tokens=300, completion_tokens=70,
                                    prompt_tokens_details=None,
                                    total_cost=None))


def _client(**kw):
    from src.llm.client import LLMClient

    return LLMClient(resolve_key=lambda _p: "k", workspace_id="ws",
                     surface="automation", user_id="u1", **kw)


def test_a_streamed_call_still_writes_the_same_spend_row(monkeypatch):
    """What breaks silently: the ledger keeps writing rows, all of them zero.

    `stream_options={"include_usage": True}` is the only reason the last chunk
    carries token counts at all. Drop it and nothing errors — Usage just goes
    quietly wrong, which is the worst way for a bill to break.
    """
    import litellm

    from src.llm import budget

    calls: list[dict] = []
    recorded: list[dict] = []
    monkeypatch.setattr(budget, "record_spend",
                        lambda **kw: recorded.append(kw))
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: (calls.append(kw), _chunks("hello"))[1])

    result = _client().generate(
        prompt="p", model="gemini/flash", agent="automation",
        operation="automation_interpret", repo="a/b", timeout=20,
        on_delta=lambda _t: True,
    )

    assert calls[0]["stream"] is True
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert result.text == "hello"
    assert (result.input_tokens, result.output_tokens) == (300, 70)
    row = recorded[-1]
    assert (row["surface"], row["operation"], row["user_id"], row["repo_slug"]) \
        == ("automation", "automation_interpret", "u1", "a/b")
    assert (row["tokens_in"], row["tokens_out"]) == (300, 70)


def test_a_provider_that_cannot_stream_still_answers(monkeypatch):
    """This is a latency improvement, not a new dependency. A deployment that
    refuses `stream=True` must produce the same answer a moment later, not an
    error."""
    import litellm

    from src.llm import budget

    monkeypatch.setattr(budget, "record_spend", lambda **kw: None)
    attempts: list[bool] = []
    N = types.SimpleNamespace

    def _completion(**kw):
        attempts.append(bool(kw.get("stream")))
        if kw.get("stream"):
            raise RuntimeError("streaming is not supported by this deployment")
        return N(model=kw["model"],
                 choices=[N(message=N(content="fallback"),
                            finish_reason="stop")],
                 usage=N(prompt_tokens=1, completion_tokens=1,
                         prompt_tokens_details=None, total_cost=None))

    monkeypatch.setattr(litellm, "completion", _completion)
    result = _client().generate(
        prompt="p", model="gemini/flash", agent="automation",
        operation="automation_interpret", timeout=20, num_retries=1,
        on_delta=lambda _t: True,
    )
    assert attempts == [True, False]
    assert result.text == "fallback"


def test_stopping_between_chunks_closes_the_stream(monkeypatch):
    import litellm

    from src.llm import budget

    monkeypatch.setattr(budget, "record_spend", lambda **kw: None)
    delivered: list[str] = []

    def _completion(**kw):
        for c in _chunks("abcdefghijklmnopqrstuvwxyz", size=1):
            delivered.append("chunk")
            yield c

    monkeypatch.setattr(litellm, "completion", _completion)
    result = _client().generate(
        prompt="p", model="gemini/flash", agent="automation",
        operation="automation_interpret", timeout=20,
        on_delta=lambda text: len(text) < 4,
    )
    assert result.finish_reason == "cancelled"
    assert len(delivered) < 26, "the stream was drained after the stop"


def test_a_caller_with_no_callback_is_not_streamed(monkeypatch):
    """Every other surface — review agents, the vault build — must keep the
    exact call it makes today."""
    import litellm

    from src.llm import budget

    monkeypatch.setattr(budget, "record_spend", lambda **kw: None)
    calls: list[dict] = []
    N = types.SimpleNamespace

    def _completion(**kw):
        calls.append(kw)
        return N(model=kw["model"],
                 choices=[N(message=N(content="x"), finish_reason="stop")],
                 usage=N(prompt_tokens=1, completion_tokens=1,
                         prompt_tokens_details=None, total_cost=None))

    monkeypatch.setattr(litellm, "completion", _completion)
    _client().generate(prompt="p", model="gemini/flash", agent="architect",
                       operation="review_architect")
    assert "stream" not in calls[0]
    assert "stream_options" not in calls[0]


# ─── the row: the sentence survives the page being closed ────────────


def test_the_row_can_hold_a_sentence_that_is_still_being_written():
    from src.db.models import AutomationRun

    assert "partial_note" in AutomationRun.__table__.columns, (
        "there is nowhere to put the sentence while it is being written, so "
        "closing the page mid-reading still loses it"
    )
    assert AutomationRun.__table__.columns["partial_note"].nullable, (
        "every row written before this shipped has no partial text"
    )


def test_the_client_polling_the_run_is_told_the_partial_sentence():
    from src.api.routers.automation import RunOut, _as_run

    assert "partial_note" in RunOut.model_fields
    old = types.SimpleNamespace(
        id="r1", session_id=None, message="m", status="planned", steps=[],
        note="done", language="uk", resolved_repos=None, blocked=None,
        result=None, error=None, user_email=None, created_at=None,
        executed_at=None,
    )
    # A row from before the column existed reaches this mapper mid-deploy.
    assert _as_run(old).partial_note == ""


def test_the_partial_is_written_far_less_often_than_a_token(monkeypatch):
    """What breaks: one UPDATE per token.

    A database write per token buys nothing — the page polls an order of
    magnitude slower than a model emits, so all but one write per poll is
    seen by nobody.
    """
    from src.sync import handlers, queue

    statements: list[tuple] = []

    class _Conn:
        def execute(self, stmt, params):
            statements.append((str(stmt), params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(queue, "_engine",
                        lambda: types.SimpleNamespace(begin=_Conn))

    # A clock that advances like the real one does: the measured readings take
    # 1.8-5.5s, so a sentence arrives over seconds, not instantly.
    sentence = "Вмикаю автоматичний рев'ю для двох репозиторіїв."
    clock = {"t": 0.0}
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock["t"])

    write = handlers._partial_note_writer("run-1")
    for i in range(1, len(sentence) + 1):
        clock["t"] += 3.0 / len(sentence)      # ~3 seconds for the whole thing
        write(sentence[:i])

    assert 3 <= len(statements) <= 12, (
        f"{len(statements)} writes for {len(sentence)} tokens over three "
        f"seconds — the throttle is gone (or nothing reaches the row at all)"
    )
    sql, params = statements[-1]
    assert "partial_note" in sql
    # The tail of the sentence can be up to one interval behind, and that is
    # fine: the authoritative `note` lands the moment the reading ends.
    assert sentence.startswith(params["p"])
    assert len(params["p"]) > len(sentence) * 0.6
    assert "status = 'reading'" in sql, (
        "a partial arriving after Stop would repaint a stopped row as if it "
        "were still thinking"
    )


def test_the_reading_reports_itself_and_can_be_stopped_mid_sentence():
    """The handler is where the two halves meet: the planner streams, and this
    is what makes the stream visible and interruptible."""
    source = HANDLERS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
              and n.name == "handle_automation_plan")
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and "interpret" in ast.dump(n.args[0] if n.args else n.func))
    passed = {k.arg for k in call.keywords}
    assert "on_note" in passed, "the reading is streamed and nobody is told"
    assert "should_stop" in passed, (
        "Stop is only checked between phases again — it waits for the model "
        "to finish before it stops anything"
    )
