"""The walkthrough count on the "Generate summary" stage is true to the reply.

"Overview and walkthrough of 0 files generated" appeared on a review whose
agents had read the whole diff. The summary call is shown every changed file;
what came back to zero was the model's per-file rows, dropped one by one
because the path it echoed was not byte-identical to a changed path.
"""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

from src.review.orchestrator import _PRSummaryJob
from src.review.pr_summary import PRSummaryResult, parse_reply


def _reply(*paths: str) -> str:
    return json.dumps({
        "overview": "Adds a parser.",
        "files": [{"path": p, "summary": f"changes {p}"} for p in paths],
    })


def test_a_path_dressed_by_the_model_still_names_its_changed_file():
    changed = ["src/a.py", "src/b.py", "c.py"]
    _, rows = parse_reply(_reply("`src/a.py`", "./src/b.py", "b/c.py"), changed)
    assert set(rows) == set(changed)


def test_a_path_that_is_not_changed_is_still_refused():
    _, rows = parse_reply(_reply("src/other.py", "a/src/other.py"), ["src/a.py"])
    assert rows == {}


def _apply(result: PRSummaryResult, changed: list[str]) -> tuple[bool, str]:
    future: Future = Future()
    future.set_result(result)
    batch = SimpleNamespace(
        pull_request=SimpleNamespace(number=1, changed_files=changed),
        pr_overview="", walkthrough={}, tokens_in=0, tokens_out=0, cost_usd=None,
    )
    return _PRSummaryJob(ThreadPoolExecutor(max_workers=1), future).apply_to(batch)


def test_an_overview_without_rows_does_not_claim_a_walkthrough_of_zero_files():
    ok, sentence = _apply(PRSummaryResult(overview="Adds a parser."), ["a.py", "b.py"])
    assert ok is True
    assert "0 files" not in sentence
    assert "no rows" in sentence and "2 changed files" in sentence


def test_rows_are_counted_as_before():
    ok, sentence = _apply(
        PRSummaryResult(overview="o", walkthrough={"a.py": "x"}), ["a.py", "b.py"])
    assert ok is True
    assert sentence == "Overview and walkthrough of 1 file generated."
