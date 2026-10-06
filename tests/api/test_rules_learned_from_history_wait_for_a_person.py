"""Rules written from the feedback history: who may ask for them, what they
are, and the weekly tick that asks on its own.

  * editors and above start the job, members and viewers do not;
  * the job ends with PENDING proposals of origin "learned" on the repository,
    audited, and says "no_pattern" when nothing qualifies;
  * the tick does nothing unless the install asked for `weekly`, asks once a
    week per repository, and never for one with too little evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.review.learning import schedule
from src.review.learning import signals as sig
from tests.api.rbac_world import A_REPO, world

URL = "/api/review-rules"
REPLY = json.dumps({"rules": [{
    "title": "Do not flag naming in the legacy package",
    "instructions": "Variable names under src/legacy follow an old convention; leave them.",
    "severity": "info", "path_glob": "src/legacy/**",
    "rationale": "dismissed in 3 pull requests"}]})


class _Model:
    def generate(self, **kw):
        return SimpleNamespace(text=REPLY)


def _teach(ws: str, repo: str = A_REPO, n: int = 3) -> None:
    for i in range(1, n + 1):
        sig.record_verdict(
            ws, repo, sig.FindingSnapshot(title="Rename the variable",
                                          file_path=f"src/legacy/f{i}.py", rule_id="style.naming"),
            "dismissed", "reply", pr=sig.PRRef("github", "aco/app", i), actor=f"p{i}")


@pytest.mark.parametrize("who, status", [
    ("viewer_a", 403), ("member_a", 403), ("editor_a", 202), ("admin_a", 202)])
async def test_editors_and_above_may_ask_for_rules_from_the_history(
        tmp_path, monkeypatch, who, status):
    from src.review import rules_generate

    monkeypatch.setattr(rules_generate, "_llm_client", lambda ws, actor: _Model())
    async with world(tmp_path, monkeypatch) as w:
        _teach(w.ws["ws-a"])
        r = await w.client.post(f"{URL}/generate-from-history", headers=w.h(who, "ws-a"),
                                json={"repo_slug": A_REPO})
        assert r.status_code == status, r.text


async def test_the_job_ends_with_pending_learned_proposals_and_is_audited(tmp_path, monkeypatch):
    from src.review import rules_generate

    monkeypatch.setattr(rules_generate, "_llm_client", lambda ws, actor: _Model())
    async with world(tmp_path, monkeypatch) as w:
        _teach(w.ws["ws-a"])
        h = w.h("editor_a", "ws-a")
        r = await w.client.post(f"{URL}/generate-from-history", headers=h,
                                json={"repo_slug": A_REPO})
        job = (await w.client.get(f"{URL}/jobs/{r.json()['id']}", headers=h)).json()
        assert job["kind"] == "history" and job["status"] == "completed", job
        assert len(job["result"]["created"]) == 1
        listed = (await w.client.get(URL, headers=h, params={"repo": A_REPO})).json()
        [rule] = listed["rules"]
        assert (rule["status"], rule["origin"], rule["repo_slug"]) == (
            "pending", "learned", A_REPO)
        assert "review_rules.history_started" in {a["action"] for a in w.audit}


async def test_a_repository_with_no_pattern_says_so(tmp_path, monkeypatch):
    from src.review import rules_generate

    monkeypatch.setattr(rules_generate, "_llm_client", lambda ws, actor: _Model())
    async with world(tmp_path, monkeypatch) as w:
        _teach(w.ws["ws-a"], n=1)
        h = w.h("editor_a", "ws-a")
        r = await w.client.post(f"{URL}/generate-from-history", headers=h,
                                json={"repo_slug": A_REPO})
        job = (await w.client.get(f"{URL}/jobs/{r.json()['id']}", headers=h)).json()
        assert job["status"] == "completed" and job["result"]["note"] == "no_pattern"


async def test_another_workspaces_repository_is_a_404(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post(f"{URL}/generate-from-history", headers=w.h("editor_a", "ws-a"),
                                json={"repo_slug": "github_nobody-here"})
        assert r.status_code == 404


# ─── the weekly tick ─────────────────────────────────────────────────


async def test_the_tick_does_nothing_unless_the_install_asked_for_weekly(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        _teach(w.ws["ws-a"])
        assert await schedule.run_once() == {"checked": 0, "started": 0}


async def test_the_tick_asks_once_a_week_for_a_repository_with_enough_evidence(
        tmp_path, monkeypatch):
    from src.review import rules_generate
    from src.review.settings import get_review_settings

    monkeypatch.setattr(rules_generate, "_llm_client", lambda ws, actor: _Model())
    monkeypatch.setattr(get_review_settings(), "learning_rules_schedule", "weekly")
    async with world(tmp_path, monkeypatch) as w:
        _teach(w.ws["ws-a"])
        _teach(w.ws["ws-a"], repo="github_aco-thin", n=1)
        first = await schedule.run_once()
        assert first == {"checked": 1, "started": 1}
        again = await schedule.run_once()
        assert again == {"checked": 1, "started": 0}, "once a week per repository"
        later = await schedule.run_once(now=datetime.now(UTC) + schedule.MIN_GAP + timedelta(hours=1))
        assert later["started"] == 1


async def test_the_loop_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CELMIS_DISABLE_LEARNING_SCHED", "1")
    schedule.stop_learning_scheduler()
    schedule.start_learning_scheduler()
    assert schedule._TASK is None
