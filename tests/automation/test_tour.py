"""Show-me tours: only targets the app really has may reach the browser."""

from __future__ import annotations

import re
from pathlib import Path

from src.automation.tour import (
    AGENTS,
    MAX_STEPS,
    TARGETS,
    extract_tour,
    resolve_target,
    strip_partial,
    tour_prompt,
)

WEB = Path(__file__).resolve().parents[2] / "web"


def test_known_targets_resolve_with_a_route_from_the_table():
    r = resolve_target("review-agent-max-output:security")
    assert r == {
        "target": "review-agent-max-output:security",
        "route": "/review-settings?section=categories&agent=security",
        "opener": "review-agent-toggle:security",
    }
    assert resolve_target("llm-apply-all") == {
        "target": "llm-apply-all", "route": "/settings/llm"}


def test_unknown_or_malformed_targets_are_dropped():
    for bad in ("nope", "", None, 7, "llm-apply-all:security",
                "review-agent-max-output", "review-agent-max-output:evil",
                "review-agent-max-output:", "../x", "a[href]"):
        assert resolve_target(bad) is None, bad


def test_extract_keeps_valid_steps_and_removes_the_block():
    text = (
        "Open the agent and set the cap.\n\n[[guide]]\n"
        "review-agent-toggle:security | Open **Model & limits**.\n"
        "- review-agent-max-output:security | The cap, see [docs](/x).\n"
        "made-up-thing | Ignored.\n"
        "review-agent-toggle:security | Duplicate.\n"
        "llm-apply-all |   \n"
        "[[/guide]]"
    )
    clean, steps = extract_tour(text)
    assert clean == "Open the agent and set the cap."
    assert [s["target"] for s in steps] == [
        "review-agent-toggle:security", "review-agent-max-output:security"]
    assert steps[0]["text"] == "Open Model & limits."
    assert steps[1]["text"] == "The cap, see docs."


def test_steps_are_capped_and_text_is_bounded():
    lines = "\n".join(f"review-agent-model:{a} | {'x' * 900}" for a in AGENTS)
    _, steps = extract_tour(f"a\n[[guide]]\n{lines}\n[[/guide]]")
    assert len(steps) == min(MAX_STEPS, len(AGENTS))
    assert all(len(s["text"]) <= 220 for s in steps)


def test_no_block_means_no_tour_and_text_is_untouched():
    assert extract_tour("plain answer") == ("plain answer", [])
    assert extract_tour("") == ("", [])


def test_streaming_never_shows_a_half_written_block():
    assert strip_partial("Answer.\n\n[[gui") == "Answer."
    assert strip_partial("Answer.\n[[guide]]\nllm-apply") == "Answer."
    assert strip_partial("Answer [1] done") == "Answer [1] done"
    assert strip_partial("Answer.\n[[guide]]\nllm-apply-all | x\n[[/guide]]") == "Answer."


def test_prompt_lists_every_target():
    prompt = tour_prompt()
    assert all(f"- {tid}" in prompt for tid in TARGETS)


def test_every_target_has_a_data_tour_attribute_in_the_web_app():
    source = "\n".join(
        p.read_text(encoding="utf-8")
        for p in WEB.rglob("*.tsx")
        if "node_modules" not in p.parts and ".next" not in p.parts
        and "data-tour" in p.read_text(encoding="utf-8"))
    missing = [
        tid for tid in TARGETS
        if not re.search(rf'["`]{tid}[:"`]', source)
    ]
    assert not missing, missing


def test_the_handler_lifts_the_block_out_of_the_note_into_result_tour(monkeypatch):
    import asyncio

    import src.automation.chat as chat
    import src.sync.handlers as handlers
    import src.sync.queue as queue

    finished: list[dict] = []
    note = ("Set it per agent.\n[[guide]]\n"
            "review-agent-max-output:defect | The cap.\nbogus | no\n[[/guide]]")
    plan = chat.Plan(steps=[chat.Step(action="help", arguments={})],
                     note=note, language="en")
    monkeypatch.setattr(chat, "interpret", lambda *a, **k: plan)
    monkeypatch.setattr(chat, "resolve_scope", lambda p, **k: p)
    monkeypatch.setattr(queue, "is_cancel_requested", lambda _id: False)
    monkeypatch.setattr(handlers, "_partial_note_writer", lambda _r: (lambda _t: None))
    monkeypatch.setattr(handlers, "_cancel_poller", lambda _j: (lambda: False))

    async def _reads(_plan, _payload):
        return {"steps": [{"action": "help", "result": {"links": []}}]}

    async def _finish(run_id, **kw):
        finished.append(kw)

    monkeypatch.setattr(handlers, "_run_automation_reads", _reads)
    monkeypatch.setattr(handlers, "_finish_automation_plan", _finish)
    asyncio.run(handlers.handle_automation_plan({"id": "j", "payload": {
        "run_id": "r", "workspace_id": "ws", "user_id": "u",
        "message": "q", "history": None}}))
    (kw,) = finished
    assert kw["note"] == "Set it per agent."
    assert [s["target"] for s in kw["result"]["tour"]] == ["review-agent-max-output:defect"]
