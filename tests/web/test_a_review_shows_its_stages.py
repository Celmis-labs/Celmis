"""The pull-requests page shows each review's stages — and has words for them.

Three seams, each of which fails silently when it drifts:

  * the server names stages by key and the page labels them from a fixed
    list (`KNOWN_STAGES` in web/lib/review-stages.ts). A key the server
    emits that the page does not know renders as the server's English name in
    every language — so every key the pipeline can emit is checked against
    the list, by reading the pipeline's own calls;
  * every label, status word and meta chip has a key in every catalogue, and
    Ukrainian is a translation, not a copy of the English;
  * the decisions the timeline makes — how a duration reads, which pill an
    unknown status gets, how long a run took when it recorded no elapsed
    time — are compiled with the web app's own tsc and run on node.

And the two pages wire it: the PR table expands into `PullRequestReviews`,
the open-PR list queues reviews through `openPullsApi` with a confirmation
before "Review all". Checked with comments stripped, so prose ABOUT the
wiring cannot stand in for it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    WEB,
    _strip_comments,
)

ROOT = WEB.parent
STAGES_TS = WEB / "lib" / "review-stages.ts"
TIMELINE = WEB / "components" / "review-timeline.tsx"
PRS_PAGE = WEB / "app" / "(app)" / "pull-requests" / "page.tsx"
REPOS_PAGE = WEB / "app" / "(app)" / "repositories" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"

#: Where the server writes stage keys.
EMITTERS = [ROOT / "src" / "review" / "orchestrator.py",
            ROOT / "src" / "review" / "dispatch.py",
            ROOT / "src" / "review" / "stages.py",
            ROOT / "src" / "api" / "review_runs.py",
            ROOT / "src" / "review" / "webhook.py"]


def _ts_list(name: str) -> list[str]:
    src = _strip_comments(STAGES_TS.read_text(encoding="utf-8"))
    m = re.search(rf"export const {name} = \[(.*?)\];", src, re.S)
    assert m, f"{name} is not a literal array in review-stages.ts"
    return re.findall(r'"([^"]+)"', m.group(1))


def _server_keys() -> set[str]:
    keys: set[str] = set()
    for path in EMITTERS:
        src = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r'\.(?:begin|end|add)\(\s*"([a-z_]+)"', src))
        keys |= set(re.findall(r'gate_key="([a-z_]+)"', src))
        keys |= set(re.findall(r'"key": "([a-z_]+)"', src))
    return keys


def test_the_scan_finds_the_pipeline():
    """Guards the guard: a broken pattern would make the next test vacuous."""
    keys = _server_keys()
    assert {"fetch_pr", "gate_target_branch", "publish", "finished", "received",
            "queued", "record"} <= keys


def test_every_stage_the_server_emits_has_a_label():
    missing = _server_keys() - set(_ts_list("KNOWN_STAGES"))
    assert not missing, f"stage keys with no label on the page: {sorted(missing)}"


def _catalogue(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))


def _needed() -> list[str]:
    from src.review.models import ReviewRunStatus

    return ([f"prs.stage.{k}" for k in _ts_list("KNOWN_STAGES")]
            + ["prs.stage.agent"]
            + [f"prs.meta.{k}" for k in _ts_list("META_KEYS")]
            + [f"prs.stageStatus.{w}" for w in ("success", "skipped", "failed", "running")]
            + [f"prs.review.{s.value}" for s in ReviewRunStatus])


@pytest.mark.parametrize("locale", sorted(p.stem for p in MESSAGES.glob("*.json")))
def test_every_label_has_words_in_every_catalogue(locale):
    cat = _catalogue(locale)
    missing = [k for k in _needed() if not cat.get(k)]
    assert not missing, f"{locale}: {missing}"


def test_ukrainian_is_translated_not_copied():
    en, uk = _catalogue("en"), _catalogue("uk")
    copied = [k for k in _needed() if uk[k] == en[k] and k != "prs.meta.model"]
    assert not copied, copied


# ─── the timeline's decisions, run on node ───────────────────────────


@pytest.fixture
def run_ts(tmp_path):
    if not TSC.exists():
        pytest.skip("web deps not installed")

    def _run(main: str) -> object:
        (tmp_path / "review-stages.ts").write_text(
            STAGES_TS.read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / "main.ts").write_text(main, encoding="utf-8")
        subprocess.run(
            [str(TSC), "main.ts", "review-stages.ts", "--target", "es2020",
             "--module", "commonjs", "--lib", "es2020,dom", "--outDir", "out",
             "--skipLibCheck", "--strict"],
            cwd=tmp_path, capture_output=True, text=True, timeout=300,
        )
        js = tmp_path / "out" / "main.js"
        assert js.exists(), "tsc emitted nothing"
        out = subprocess.run(["node", str(js)], capture_output=True, text=True,
                             timeout=60, env={**os.environ, "TZ": "UTC"})
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    return _run


def test_the_timeline_decisions(run_ts):
    got = run_ts("""
import * as s from "./review-stages";
const now = Date.parse("2026-10-04T12:00:00Z");
console.log(JSON.stringify({
  durations: [s.formatDuration(850), s.formatDuration(12_400), s.formatDuration(198_000),
              s.formatDuration(3_720_000), s.formatDuration(null), s.formatDuration(-1)],
  pills: [s.stagePillVariant("success"), s.stagePillVariant("skipped"),
          s.stagePillVariant("running"), s.stagePillVariant("failed"),
          s.stagePillVariant("great"), s.stagePillVariant(undefined)],
  words: [s.stageStatusWord("skipped"), s.stageStatusWord("weird")],
  labels: [s.stageLabel({key: "agent:security", name: "Agent: security"}),
           s.stageLabel({key: "gate_target_branch", name: "x"}),
           s.stageLabel({key: "from_the_future", name: "Future"})],
  runs: [s.runDurationMs({elapsed_seconds: 1.5}),
         s.runDurationMs({started_at: "2026-10-04T12:00:00Z",
                          finished_at: "2026-10-04T12:03:18Z"}),
         s.runDurationMs({stages: [{key: "finished", name: "", status: "skipped",
                                    started_at: null, duration_ms: 42, reason: ""}]}),
         s.runDurationMs({started_at: "2026-10-04T12:00:00Z"})],
  ago: [s.relativeTime("2026-10-01T12:00:00Z", "en", now),
        s.relativeTime("2026-10-04T11:55:00Z", "en", now),
        s.relativeTime(null, "en", now)],
  meta: s.metaEntries({tokens_out: 300, extra: "x", model: "gemini", findings: 0,
                       empty: "", tokens_in: 12000}),
}));
""")
    assert got["durations"] == ["850 ms", "12s", "3m 18s", "1h 02m", "—", "—"]
    assert got["pills"] == ["success", "default", "brand", "destructive",
                            "destructive", "destructive"], \
        "an unknown status is a failure, never a success"
    assert got["words"] == ["skipped", "failed"]
    assert got["labels"] == [
        {"key": "prs.stage.agent", "vars": {"name": "security"}},
        {"key": "prs.stage.gate_target_branch"},
        {"key": None},
    ]
    assert got["runs"] == [1500, 198000, 42, None]
    assert got["ago"] == ["3 days ago", "5 minutes ago", "—"]
    assert got["meta"] == [["model", "gemini"], ["findings", "0"],
                           ["tokens_in", "12,000"], ["tokens_out", "300"],
                           ["extra", "x"]]


# ─── the wiring ──────────────────────────────────────────────────────


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def test_a_pr_row_expands_into_its_reviews_and_their_stages():
    page = _code(PRS_PAGE)
    assert "<PullRequestReviews prId={pr.id}" in page
    assert "aria-expanded={open}" in page
    assert "last_review_reason" in page
    timeline = _code(TIMELINE)
    assert "pullRequestsApi.runs(" in timeline
    assert "<StageTimeline stages={run.stages}" in timeline
    assert "run.status_reason" in timeline


def test_the_open_pr_list_reviews_through_the_queue_with_confirmation():
    page = _code(REPOS_PAGE)
    assert "openPullsApi.list(" in page
    assert "openPullsApi.review(" in page
    assert "openPullsApi.reviewAll(" in page
    # Confirmation first, and never past the server's limit.
    bulk = page[page.index("const onReviewAll"):]
    assert bulk.index("await confirm(") < bulk.index("bulk.mutate()")
    assert "tooMany" in bulk[:bulk.index("await confirm(")]
    # The branch filter defaults to every target branch.
    assert 'useState<string>("")' in page[page.index("function ManualPullList"):]
    assert "target_branch" in page and "source_branch" in page
