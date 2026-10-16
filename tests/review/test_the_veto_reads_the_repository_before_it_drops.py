"""The LLM veto drops a finding only when repository code refutes it.

The false positives that survived the old veto were refutable only by code
OUTSIDE the diff (a constant defined in another file, a guard many lines above
the hunk), and the veto neither woke for one or two findings nor saw anything
but the first five hunks of the PR. These tests pin the pieces that change
that: the threshold setting, symbol extraction, evidence from a local clone and
from a provider, the budget, failing open, the prompt, and drops that must
carry a reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.review.agents import verifier_evidence as ve
from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent
from src.review.models import Finding, Hunk, PullRequest
from src.review.providers.base import CodeHit
from src.review.settings import ReviewSettings

# ─── fixtures ────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "lib").mkdir()
    (root / "lib" / "Config.php").write_text(
        "<?php\nclass Config {\n    const MAX_SIZE = 10;\n}\n")
    (root / "vendor").mkdir()
    (root / "vendor" / "junk.php").write_text("MAX_SIZE in vendor\n")
    body = "\n".join(f"line {n}" for n in range(1, 200))
    (root / "page.php").write_text(
        body + "\nforeach ($items as $item) {\n    use_it($item);\n}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD")


def _pr(head_sha: str = "deadbeef") -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="",
        author="a", base_ref="main", base_sha="b", head_ref="f",
        head_sha=head_sha, state="open",
        hunks=[
            Hunk(file_path="page.php", old_file_path="page.php", old_start=1,
                 old_count=1, new_start=200, new_count=1,
                 content="@@ -1 +200 @@\n+use_it($item);\n"),
            Hunk(file_path="other.php", old_file_path="other.php", old_start=1,
                 old_count=1, new_start=1, new_count=1,
                 content="@@ -1 +1 @@\n+unrelated\n"),
        ])


def _finding(title="Undefined constant `Config::MAX_SIZE`", body="", line=200,
             path="page.php", **kw) -> Finding:
    return Finding(file_path=path, line=line, title=title, body=body,
                   rule_id="defect.x", agent="defect", **kw)


def _settings(root: Path | None):
    return SimpleNamespace(
        repo_path=lambda slug: root if root is not None else Path("/nonexistent"))


class FakeProvider:
    def __init__(self, files=None, hits=None, fail=False):
        self.files, self.hits, self.fail = files or {}, hits or {}, fail
        self.reads: list[tuple] = []
        self.searches: list[str] = []

    def read_file_at(self, repo, ref, path):
        self.reads.append((repo, ref, path))
        if self.fail:
            raise RuntimeError("boom")
        return self.files.get(path)

    def search_code(self, repo, query, limit=5):
        self.searches.append(query)
        if self.fail:
            raise RuntimeError("boom")
        return self.hits.get(query, [])


# ─── the threshold ───────────────────────────────────────────────────


def test_the_default_threshold_is_one_and_the_env_sets_it(monkeypatch):
    assert ReviewSettings().verifier_min_findings == 1
    monkeypatch.setenv("REVIEW_VERIFIER_MIN_FINDINGS", "4")
    assert ReviewSettings().verifier_min_findings == 4


def _agent_with(monkeypatch, minimum: int, reply: str = '{"keep": [0]}'):
    monkeypatch.setattr("src.review.agents.verifier.get_review_settings",
                        lambda: ReviewSettings(verifier_min_findings=minimum))
    agent = VerifierAgent()
    calls: list[list[Finding]] = []

    def fake(findings, context):
        from src.review.agents.verifier import _VetoOutcome
        calls.append(list(findings))
        return _VetoOutcome(list(findings))

    monkeypatch.setattr(agent, "_llm_verify", fake)
    return agent, calls


def test_one_finding_wakes_the_veto_by_default(monkeypatch):
    agent, calls = _agent_with(monkeypatch, 1)
    ctx = AgentContext(pull_request=_pr())
    res = agent.llm_pass([_finding()], ctx)
    assert len(calls) == 1 and res.judged == 1


def test_a_raised_threshold_keeps_the_veto_asleep(monkeypatch):
    agent, calls = _agent_with(monkeypatch, 3)
    res = agent.llm_pass([_finding(), _finding(line=5)], AgentContext(pull_request=_pr()))
    assert calls == [] and res.judged == 0 and len(res.kept) == 2


def test_proven_findings_never_enter_and_do_not_count(monkeypatch):
    agent, calls = _agent_with(monkeypatch, 1)
    proven = _finding(evidence_kind="proven")
    agent.llm_pass([proven], AgentContext(pull_request=_pr()))
    assert calls == []
    agent.llm_pass([proven, _finding()], AgentContext(pull_request=_pr()))
    assert len(calls) == 1 and len(calls[0]) == 1


# ─── symbol extraction ───────────────────────────────────────────────


def test_symbols_cover_the_shapes_in_the_measured_false_positives():
    assert "Config::MAX_SIZE" in ve.extract_symbols("Undefined constant `Config::MAX_SIZE`")
    assert "MAX_SIZE" in ve.extract_symbols("Undefined constant `Config::MAX_SIZE`")
    assert "$item" in ve.extract_symbols("undefined variable $item in the loop")
    assert "appStat" in ve.extract_symbols("typo in global `appStat`")
    syms = ve.extract_symbols('overlay has class "dialogbox" so #top-banner-div closes')
    assert "dialogbox" in syms and "#top-banner-div" in syms
    assert "is_uploaded_file" in ve.extract_symbols("missing is_uploaded_file() check")
    assert "UPLOAD_ERR_OK" in ve.extract_symbols("UPLOAD_ERR_OK is never compared")


def test_symbols_skip_keywords_common_words_and_cap_the_list():
    syms = ve.extract_symbols("`if` `this` TODO JSON `null` `x`", "$this and NULL")
    assert syms == []
    many = " ".join(f"`name{n}`" for n in range(20))
    assert len(ve.extract_symbols(many)) == ve.MAX_SYMBOLS


def test_file_extensions_are_not_selectors():
    assert ve.extract_symbols("rename the .php files in foo.php") == []


# ─── evidence from a local clone ─────────────────────────────────────


def test_clone_evidence_has_window_hunks_and_symbol_hits(clone):
    root, sha = clone
    f = _finding()
    b = ve.gather_evidence([f], _pr(sha), app_settings=_settings(root))
    ev = b.items[0]
    assert "clone @" in ev.window_source
    assert "200: foreach ($items as $item) {" in ev.window
    assert "122:" not in ev.window.split("\n")[0]  # window is centred, not the file head
    assert "use_it($item)" in ev.hunks and "unrelated" not in ev.hunks
    hits = {h.symbol: h for h in ev.hits}
    assert any("const MAX_SIZE = 10" in blk for blk in hits["MAX_SIZE"].blocks)
    assert not any("vendor" in blk for blk in hits["MAX_SIZE"].blocks)
    assert b.stats["windows_clone"] == 1 and b.stats["searches_clone"] >= 1


def test_zero_hits_at_the_real_head_is_stated_as_absence(clone):
    root, sha = clone
    f = _finding(title="Typo in `nopeSuchName`")
    b = ve.gather_evidence([f], _pr(sha), app_settings=_settings(root))
    text = ve.render_evidence(b.items[0])
    assert "NO match anywhere in the repository" in text


def test_a_commit_missing_from_the_clone_uses_head_and_says_so(clone):
    root, _ = clone
    b = ve.gather_evidence([_finding()], _pr("0" * 40), app_settings=_settings(root))
    hit = next(h for h in b.items[0].hits if h.symbol == "MAX_SIZE")
    assert "clone HEAD" in hit.source and hit.blocks
    assert b.items[0].window == ""  # no window from a stand-in commit


# ─── evidence from a provider ────────────────────────────────────────


def test_provider_supplies_window_and_labelled_search_when_no_clone():
    text = "\n".join(f"row {n}" for n in range(1, 300))
    prov = FakeProvider(
        files={"page.php": text},
        hits={"MAX_SIZE": [CodeHit("lib/Config.php", ("3: const MAX_SIZE = 10;",))]})
    b = ve.gather_evidence([_finding()], _pr(), provider=prov, app_settings=_settings(None))
    ev = b.items[0]
    assert ev.window_source.startswith("provider @") and "200: row 200" in ev.window
    hit = next(h for h in ev.hits if h.symbol == "MAX_SIZE")
    assert "default branch" in hit.source and "const MAX_SIZE" in hit.blocks[0]
    rendered = ve.render_evidence(ev)
    assert "default branch" in rendered
    assert prov.reads == [("o/r", "deadbeef", "page.php")]


def test_a_provider_miss_proves_nothing_and_says_so():
    prov = FakeProvider(files={"page.php": "x\n" * 300})
    b = ve.gather_evidence([_finding()], _pr(), provider=prov, app_settings=_settings(None))
    assert "proves nothing" in ve.render_evidence(b.items[0])


def test_provider_searches_are_capped_and_cached():
    prov = FakeProvider()
    fs = [_finding(title=f"`sym{n}` `sym{n + 1}` `sym{n + 2}` `shared`") for n in range(10)]
    ve.gather_evidence(fs, _pr(), provider=prov, app_settings=_settings(None))
    assert len(prov.searches) <= ve.MAX_PROVIDER_SEARCHES
    assert len(prov.searches) == len(set(prov.searches))


# ─── budget ──────────────────────────────────────────────────────────


def test_the_total_budget_and_per_finding_cap_hold(clone):
    root, sha = clone
    fs = [_finding(line=100 + n) for n in range(12)]
    b = ve.gather_evidence(fs, _pr(sha), app_settings=_settings(root),
                           total_budget=6_000, per_finding_cap=1_500)
    assert all(e.chars <= 1_500 for e in b.items)
    assert b.stats["chars"] <= 6_000


def test_with_more_findings_than_the_budget_funds_the_rest_get_none(clone):
    root, sha = clone
    fs = [_finding(line=100 + n) for n in range(10)]
    b = ve.gather_evidence(fs, _pr(sha), app_settings=_settings(root), total_budget=1_800)
    assert b.stats["unfunded"] == 7
    assert b.items[9].chars == 0 and b.items[0].chars > 0


# ─── failing open ────────────────────────────────────────────────────


def test_a_failing_provider_and_missing_clone_mean_less_evidence_not_an_error():
    b = ve.gather_evidence([_finding()], _pr(), provider=FakeProvider(fail=True),
                           app_settings=_settings(None))
    assert b.items[0].window == "" and b.stats["errors"] >= 1
    assert "could not be searched" in ve.render_evidence(b.items[0])


def test_garbage_input_never_raises():
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError(name)

    b = ve.gather_evidence([Boom(), _finding(path="../etc/passwd")], _pr(),
                           app_settings=_settings(None))
    assert len(b.items) == 2


def test_evidence_failure_leaves_the_veto_running_on_the_claim(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no")

    monkeypatch.setattr(ve, "gather_evidence", boom)
    ctx = AgentContext(pull_request=_pr())
    prompt = VerifierAgent._build_verification_prompt([_finding()], ctx, None)
    assert "Undefined constant" in prompt and "use_it($item)" in prompt
    agent = VerifierAgent()
    seen = {}

    class Client:
        def generate(self, **kw):
            seen["prompt"] = kw["prompt"]
            return SimpleNamespace(text='{"keep": [0]}', input_tokens=5, output_tokens=2)

    ctx.llm_client = Client()
    out = agent._llm_verify([_finding()], ctx)
    assert out.error is None and len(out.verified) == 1 and "Undefined constant" in seen["prompt"]


# ─── prompt ──────────────────────────────────────────────────────────


def test_the_prompt_holds_the_claim_the_window_the_own_file_hunks_and_hits(clone):
    root, sha = clone
    pr = _pr(sha)
    fs = [_finding()]
    bundle = ve.gather_evidence(fs, pr, app_settings=_settings(root))
    prompt = VerifierAgent._build_verification_prompt(fs, AgentContext(pull_request=pr), bundle)
    assert "Undefined constant" in prompt
    assert "File around the line" in prompt and "200: foreach" in prompt
    assert "use_it($item)" in prompt and "unrelated" not in prompt
    assert "Repository evidence" in prompt and "const MAX_SIZE = 10" in prompt


def test_the_system_prompt_drops_on_refutation_only():
    from src.review.agents.verifier import _VERIFIER_SYSTEM as sys_prompt

    low = sys_prompt.lower()
    assert "refute" in low and "keep is the default" in low
    # the rules that deleted true positives stay gone
    assert "a risk or a preference" not in low
    assert "severity does not match" not in low
    assert "the line it points at is not in the diff" not in low


# ─── drops carry reasons ─────────────────────────────────────────────


def _run(reply: str, n: int = 2):
    agent = VerifierAgent()

    class Client:
        def generate(self, **kw):
            return SimpleNamespace(text=reply, input_tokens=100, output_tokens=20)

    ctx = AgentContext(pull_request=_pr())
    ctx.llm_client = Client()
    fs = [_finding(title=f"finding {i}", line=i + 1) for i in range(n)]
    return fs, agent._llm_verify(fs, ctx)


def test_a_drop_with_a_reason_is_honoured_and_recorded():
    fs, out = _run('{"keep": [1], "reasons": {"0": "const MAX_SIZE is defined in lib/Config.php"}}')
    assert out.verified == [fs[1]]
    assert out.dropped == [(fs[0], "const MAX_SIZE is defined in lib/Config.php")]
    assert out.tokens_in == 100 and out.tokens_out == 20


def test_a_drop_without_a_reason_is_kept():
    fs, out = _run('{"keep": [1]}')
    assert out.verified == fs and out.dropped == [] and out.unjustified == 1


def test_an_empty_keep_with_reasons_drops_everything_and_none_is_unreadable():
    fs, out = _run('{"keep": [], "reasons": {"0": "defined above", "1": "guard on line 3"}}')
    assert out.verified == [] and len(out.dropped) == 2
    assert VerifierAgent._parse_keep_indices('{"keep": []}', total=2) == []
    assert VerifierAgent._parse_keep_indices("no idea", total=2) is None
    fs, out = _run("no idea")
    assert out.verified == fs and out.error


def test_llm_pass_reports_judged_dropped_and_evidence(monkeypatch, clone):
    root, sha = clone
    monkeypatch.setattr(ve, "_default_app_settings", lambda: _settings(root))
    agent = VerifierAgent()

    class Client:
        def generate(self, **kw):
            return SimpleNamespace(
                text='{"keep": [], "reasons": {"0": "const MAX_SIZE exists in lib/Config.php"}}',
                input_tokens=7, output_tokens=3)

    ctx = AgentContext(pull_request=_pr(sha))
    ctx.llm_client = Client()
    res = agent.llm_pass([_finding()], ctx)
    assert res.judged == 1 and res.dropped_llm_filter == 1 and res.kept == []
    assert res.dropped_findings[0]["reason"].startswith("const MAX_SIZE")
    assert res.evidence["windows_clone"] == 1 and res.tokens_in == 7
