"""A PR that adds a dependency with a known CVE must not pass review silently.

The gap this agent closes, as it existed in the tree: the security agent's
prompt lists OWASP A06, but the model sees no package versions and no
vulnerability data — and the diff parser files every lockfile under
`skipped_files` as noise. A PR adding lodash@4.17.15 with a known RCE passed
review without a word; the standalone dependency audit noticed after merge.

What these tests pin, in order of importance:

  * an added vulnerable package produces a finding carrying the advisory id,
    the introduced and fixed versions, and the mapped severity;
  * a bump TO the fixed version produces nothing — the delta is versions, not
    file churn;
  * a PR with no dependency changes is a CLEAN answer: zero findings, zero
    scanner invocations, no error;
  * blind (no binary, no reachable OSV) is a FAILURE, not a clean zero —
    zero-findings-because-blind is the silent-APPROVE bug this project
    already fixed once for unreadable LLM replies;
  * a timeout fails the same honest way;
  * a lockfile the diff filter hid in `skipped_files` is still seen, via
    `raw_diff`;
  * the binary is chosen when present, fed the extracted packages as purls;
  * the finding survives the verifier's deterministic filters.

No test here touches the network or requires the osv-scanner binary: the
subprocess boundary is faked with `CmdResult` exactly as tests/deps does for
`audit_repo`, and the OSV API boundary is faked at the injectable
`osv_query` callable (the `fetch_vulns_batch` contract: mapping + failed
batch count).
"""

from __future__ import annotations

import json
import time

import pytest

from src.deps.native import TOOL_MISSING, TOOL_TIMEOUT, CmdResult
from src.review.agents.base import AgentContext
from src.review.agents.cve import CveAgent
from src.review.agents.verifier import VerifierAgent
from src.review.models import FindingSeverity, Hunk, PullRequest

# ─── Fixtures ────────────────────────────────────────────────────────

LODASH_ID = "GHSA-35jh-r3h4-6jhm"

LODASH_ADVISORY = {
    "id": LODASH_ID,
    "cve": "CVE-2021-23337",
    "severity": "high",
    "summary": "Command injection in lodash via the template function",
    "fixed_in": "4.17.21",
    "url": f"https://osv.dev/vulnerability/{LODASH_ID}",
    "source": "osv",
    "aliases": ["CVE-2021-23337"],
}

PACKAGE_JSON_HUNK = """@@ -1,6 +1,7 @@
 {
   "name": "app",
   "dependencies": {
+    "lodash": "^4.17.15",
     "react": "^18.2.0"
   }
 }
"""

# The lockfile as the review pipeline actually sees it: NOT in pr.hunks
# (skip_filename_patterns caught it), present only in the raw provider diff.
LOCKFILE_ADD_DIFF = """\
diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -10,7 +10,12 @@
     "": {
       "dependencies": {
+        "lodash": "^4.17.15"
       }
     },
+    "node_modules/lodash": {
+      "version": "4.17.15",
+      "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.15.tgz"
+    },
     "node_modules/react": {
       "version": "18.2.0"
     }
"""

LOCKFILE_BUMP_DIFF = """\
diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -5,4 +5,4 @@
     "node_modules/lodash": {
-      "version": "4.17.15",
+      "version": "4.17.21",
       "resolved": "x"
     },
"""

LOCKFILE_MOVE_DIFF = """\
diff --git a/package-lock.json b/package-lock.json
index 1111111..2222222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -5,4 +5,4 @@
     "node_modules/lodash": {
-      "version": "4.17.15",
+      "version": "4.17.15",
     },
     "node_modules/react": {
"""


def _hunk(path: str, content: str, *, new_start: int = 1) -> Hunk:
    new_count = sum(
        1 for line in content.splitlines()
        if not line.startswith(("@@", "-")))
    return Hunk(
        file_path=path, old_file_path=path,
        old_start=new_start, old_count=new_count,
        new_start=new_start, new_count=new_count,
        content=content,
    )


def _pr(hunks=(), raw_diff: str = "", skipped=()) -> PullRequest:
    return PullRequest(
        provider="github", repo="x/y", number=7, title="add lodash",
        description="", author="me", base_ref="main", base_sha="b" * 8,
        head_ref="feat", head_sha="h" * 8, state="open",
        hunks=list(hunks), raw_diff=raw_diff, skipped_files=list(skipped),
    )


def _missing_runner(cmd, cwd, timeout):
    return CmdResult(False, -1, "", "",
                     "osv-scanner is not installed", TOOL_MISSING)


def _refusing_runner(cmd, cwd, timeout):
    raise AssertionError("the scanner must not be invoked for this PR")


def _refusing_query(deps):
    raise AssertionError("the OSV API must not be queried on this path")


def _query_with(advisories: dict, calls: list | None = None):
    def query(deps):
        if calls is not None:
            calls.append(list(deps))
        return ({t: advisories[t] for t in deps if t in advisories}, 0)
    return query


def _run(pr, **agent_kwargs):
    agent = CveAgent(**agent_kwargs)
    return agent.review(AgentContext(pull_request=pr))


# ─── The advisory reaches the review ────────────────────────────────


def test_an_added_vulnerable_package_yields_the_advisory():
    calls: list = []
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)],
             raw_diff=LOCKFILE_ADD_DIFF, skipped=["package-lock.json"])
    result = _run(
        pr, runner=_missing_runner,
        osv_query=_query_with(
            {("npm", "lodash", "4.17.15"): [LODASH_ADVISORY]}, calls))

    assert result.error is None
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.agent == "cve"
    assert f.rule_id == f"sec.cve-{LODASH_ID}"
    # high → ERROR: a known exploitable dependency blocks the verdict.
    assert f.severity == FindingSeverity.ERROR
    assert LODASH_ID in f.body
    assert "4.17.15" in f.body          # the version the PR introduces
    assert "4.17.21" in f.body          # the version OSV says is fixed
    assert f.evidence_kind == "proven"
    assert f.confidence == 1.0
    # Anchored on the manifest line the reviewer actually sees — the lockfile
    # itself is not in the reviewed hunks.
    assert f.file_path == "package.json"
    assert f.line == 4
    assert f.suggestion is not None and "4.17.21" in f.suggestion

    # Only the PR's OWN change was looked up: react's context entry in the
    # lockfile is not this PR's doing, and the package.json range floor is
    # superseded by the lockfile's exact pin.
    assert calls == [[("npm", "lodash", "4.17.15")]]


def test_a_bump_to_the_fixed_version_reports_nothing():
    calls: list = []
    pr = _pr(raw_diff=LOCKFILE_BUMP_DIFF, skipped=["package-lock.json"])
    result = _run(pr, runner=_missing_runner,
                  osv_query=_query_with({}, calls))

    assert result.error is None
    assert result.findings == []
    # The NEW version was looked up (and came back clean) — not the old one.
    assert calls == [[("npm", "lodash", "4.17.21")]]


def test_a_moved_lockfile_entry_is_not_an_alarm():
    """Same package, same version, new line — lockfile churn, not a change.
    The scanner is not even asked."""
    pr = _pr(raw_diff=LOCKFILE_MOVE_DIFF, skipped=["package-lock.json"])
    result = _run(pr, runner=_refusing_runner, osv_query=_refusing_query)
    assert result.error is None
    assert result.findings == []


def test_a_filtered_lockfile_is_still_seen():
    """skip_filename_patterns hides lockfiles from review — the CVE agent
    reads them out of raw_diff anyway, and anchors on the lockfile when no
    reviewed manifest names the package."""
    pr = _pr(hunks=[_hunk("src/app.js", "@@ -1,1 +1,2 @@\n x = 1\n+use()\n")],
             raw_diff=LOCKFILE_ADD_DIFF, skipped=["package-lock.json"])
    result = _run(
        pr, runner=_missing_runner,
        osv_query=_query_with({("npm", "lodash", "4.17.15"): [LODASH_ADVISORY]}))

    assert result.error is None
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.file_path == "package-lock.json"
    # The `"version": "4.17.15"` line of the introducing entry.
    assert f.line == 16


def test_a_manifest_only_pr_is_checked_at_the_range_floor():
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])
    result = _run(
        pr, runner=_missing_runner,
        osv_query=_query_with({("npm", "lodash", "4.17.15"): [LODASH_ADVISORY]}))

    assert result.error is None
    assert len(result.findings) == 1
    f = result.findings[0]
    # The body must say the version is a range floor, and a ```suggestion```
    # block replacing a range with a pin would be wrong — so none is offered.
    assert "floor" in f.body
    assert f.suggestion is None


def test_python_and_go_manifests_are_extracted():
    req = """@@ -1,2 +1,3 @@
 flask==2.0.1
+requests==2.19.1
 click==8.0.0
"""
    gomod = """@@ -1,5 +1,6 @@
 module example.com/app

 require (
+\tgithub.com/gin-gonic/gin v1.6.3
 \tgithub.com/stretchr/testify v1.8.0
 )
"""
    calls: list = []
    advisory = dict(LODASH_ADVISORY, id="PYSEC-2018-28", cve="CVE-2018-18074",
                    severity="medium", fixed_in="2.20.0",
                    summary="Requests forwards Authorization headers on redirect")
    pr = _pr(hunks=[_hunk("requirements.txt", req), _hunk("go.mod", gomod)])
    result = _run(
        pr, runner=_missing_runner,
        osv_query=_query_with({("PyPI", "requests", "2.19.1"): [advisory]}, calls))

    assert result.error is None
    assert sorted(calls[0]) == [
        ("Go", "github.com/gin-gonic/gin", "1.6.3"),
        ("PyPI", "requests", "2.19.1"),
    ]
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.file_path == "requirements.txt"
    assert f.line == 2
    assert f.severity == FindingSeverity.WARNING   # medium
    assert f.rule_id == "sec.cve-PYSEC-2018-28"


def test_a_yarn_version_bump_looks_up_the_new_version_only():
    diff = """\
diff --git a/yarn.lock b/yarn.lock
--- a/yarn.lock
+++ b/yarn.lock
@@ -1,3 +1,3 @@
 lodash@^4.17.0:
-  version "4.17.15"
+  version "4.17.19"
   resolved "https://registry.yarnpkg.com/lodash"
"""
    calls: list = []
    pr = _pr(raw_diff=diff, skipped=["yarn.lock"])
    result = _run(pr, runner=_missing_runner, osv_query=_query_with({}, calls))
    assert result.error is None
    assert calls == [[("npm", "lodash", "4.17.19")]]


# ─── Clean answers vs. blindness ────────────────────────────────────


def test_a_pr_without_dependency_changes_never_invokes_a_scanner():
    pr = _pr(hunks=[_hunk("src/app.py", "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n")],
             raw_diff="")
    result = _run(pr, runner=_refusing_runner, osv_query=_refusing_query)
    # A clean zero, not a failure: nothing dependency-shaped changed.
    assert result.error is None
    assert result.findings == []


def test_blind_is_a_failure_not_a_clean_zero():
    """No binary and no reachable OSV: the agent must land in agents_failed
    (via `error`), never report zero findings it did not earn."""
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])
    result = _run(pr, runner=_missing_runner,
                  osv_query=lambda deps: ({}, 1))   # every batch failed
    assert result.error is not None
    assert "OSV" in result.error
    assert result.findings == []


def test_a_crashing_lookup_is_a_failure_too():
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])

    def broken_query(deps):
        raise ConnectionError("dns is down")

    result = _run(pr, runner=_missing_runner, osv_query=broken_query)
    assert result.error is not None
    assert result.findings == []


def test_the_timebox_fails_honestly():
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])

    def slow_runner(cmd, cwd, timeout):
        time.sleep(0.5)
        return _missing_runner(cmd, cwd, timeout)

    result = _run(pr, runner=slow_runner, osv_query=_refusing_query,
                  lookup_timeout=0.05)
    assert result.error is not None
    assert "exceed" in result.error
    assert result.findings == []


def test_a_binary_timeout_does_not_buy_a_second_budget():
    """osv-scanner timing out must not fall through to the HTTP path — the
    timebox was spent; the failure is reported, not retried elsewhere."""
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])

    def timed_out_runner(cmd, cwd, timeout):
        return CmdResult(False, -1, "", "",
                         "osv-scanner timed out after 100s", TOOL_TIMEOUT)

    result = _run(pr, runner=timed_out_runner, osv_query=_refusing_query)
    assert result.error is not None
    assert "timed out" in result.error
    assert result.findings == []


# ─── The binary path ────────────────────────────────────────────────


def _scanner_payload() -> dict:
    return {
        "results": [{
            "source": {"path": "/scan/bom.json", "type": "sbom"},
            "packages": [{
                "package": {"name": "lodash", "version": "4.17.15",
                            "ecosystem": "npm"},
                "vulnerabilities": [{
                    "id": LODASH_ID,
                    "aliases": ["CVE-2021-23337"],
                    "summary": "Command injection in lodash",
                    "database_specific": {"severity": "HIGH"},
                    "affected": [{
                        "package": {"ecosystem": "npm", "name": "lodash"},
                        "ranges": [{"type": "SEMVER", "events": [
                            {"introduced": "0"}, {"fixed": "4.17.21"}]}],
                    }],
                }],
                "groups": [{"ids": [LODASH_ID], "max_severity": "7.2"}],
            }],
        }],
    }


def test_the_binary_is_chosen_when_present():
    """With a working osv-scanner the HTTP API is never touched, and the
    binary is fed the extracted packages as purls in a CycloneDX SBOM."""
    seen: dict = {}

    def fake_scanner(cmd, cwd, timeout):
        seen["argv"] = list(cmd)
        bom = json.loads((cwd / "bom.json").read_text(encoding="utf-8"))
        seen["purls"] = [c.get("purl") for c in bom.get("components", [])]
        # exit 1 == "found vulnerabilities" — a result, not an error
        return CmdResult(True, 1, json.dumps(_scanner_payload()), "")

    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)],
             raw_diff=LOCKFILE_ADD_DIFF, skipped=["package-lock.json"])
    result = _run(pr, runner=fake_scanner, osv_query=_refusing_query)

    assert seen["argv"][0] == "osv-scanner"
    assert seen["purls"] == ["pkg:npm/lodash@4.17.15"]
    assert result.error is None
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.rule_id == f"sec.cve-{LODASH_ID}"
    assert f.severity == FindingSeverity.ERROR
    assert "4.17.21" in f.body


def test_a_clean_binary_answer_is_a_clean_zero():
    def clean_scanner(cmd, cwd, timeout):
        payload = _scanner_payload()
        payload["results"][0]["packages"][0]["vulnerabilities"] = []
        payload["results"][0]["packages"][0]["groups"] = []
        return CmdResult(True, 0, json.dumps(payload), "")

    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)])
    result = _run(pr, runner=clean_scanner, osv_query=_refusing_query)
    assert result.error is None
    assert result.findings == []


# ─── Downstream: the verifier must not eat it ───────────────────────


def test_the_finding_survives_the_verifier():
    pr = _pr(hunks=[_hunk("package.json", PACKAGE_JSON_HUNK)],
             raw_diff=LOCKFILE_ADD_DIFF, skipped=["package-lock.json"])
    result = _run(
        pr, runner=_missing_runner,
        osv_query=_query_with({("npm", "lodash", "4.17.15"): [LODASH_ADVISORY]}))
    [finding] = result.findings

    v = VerifierAgent().verify([finding], AgentContext(pull_request=pr))
    # confidence 1.0 clears the deterministic FP threshold; small batches
    # never reach the LLM pass, so the deterministic path is the whole story.
    assert v.kept == [finding]
    assert v.dropped_low_confidence == 0


def test_the_agent_is_exported_for_the_orchestrator():
    from src.review.agents import CveAgent as exported
    assert exported is CveAgent
    assert CveAgent.name == "cve"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
