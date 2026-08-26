"""The repo policy carries the prefilter's deny-list, in three states.

`ReviewSettings.suppressed_rules` hides six rule ids measured at zero true
positives. A list that lives only in code and env cannot be changed per
repository, and the policy row already has `disabled_agents` beside which a
`suppressed_rules` list is the natural shape — so the router reads and writes
one. What this file pins is the part that is easy to get wrong on the way IN:

  - the key ABSENT keeps what is stored. The policy page does not render this
    control yet, and a save from it must not wipe a list set through the API
    — the courtesy `agent_llm_overrides` already extends;
  - an explicit `null` goes back to the code default;
  - a list — `[]` included — replaces the default outright. `[]` is "hide
    nothing", which a merge with the default could never have said.

The fixture is the sibling file's: the same router over the same sqlite
table, rather than a second copy that drifts.
"""

from __future__ import annotations

from tests.api.test_the_policy_page_carries_the_ceiling import (
    REASONING_MODEL,
    _get,
    _put,
    _workspace,
    policy_api,
)

# Six categories in both spellings — the legacy prefixes historical rows
# carry and the defect.* ids the merged agent emits. Sorted, because the API
# returns the inherited default sorted.
SIX = sorted([
    "quality.duplication", "quality.magic_numbers", "quality.maintainability",
    "quality.todo", "quality.typing", "tests.no-coverage",
    "defect.duplication", "defect.magic_numbers", "defect.maintainability",
    "defect.todo", "defect.typing", "defect.no-coverage",
])


async def test_a_repo_with_no_policy_inherits_and_says_what_it_inherits():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        policy = await _get(client)
        assert policy["suppressed_rules"] is None
        assert policy["suppressed_rules_effective"] == SIX


async def test_a_list_replaces_the_default_and_an_empty_one_hides_nothing():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, suppressed_rules=["sec.cwe-862", "quality.todo"])
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["suppressed_rules"] == ["sec.cwe-862", "quality.todo"]
        assert policy["suppressed_rules_effective"] == ["sec.cwe-862", "quality.todo"]

        saved = await _put(client, suppressed_rules=[])
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["suppressed_rules"] == []
        assert policy["suppressed_rules_effective"] == [], (
            "[] came back as the default — a repo cannot switch the gate off"
        )


async def test_a_save_that_does_not_mention_the_list_keeps_it():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, suppressed_rules=["sec.cwe-862"])

        # The policy page's save today: every field it knows, and not this one.
        saved = await _put(client, prompt_template="be brief")
        assert saved.status_code == 200, saved.text

        policy = await _get(client)
        assert policy["prompt_template"] == "be brief"
        assert policy["suppressed_rules"] == ["sec.cwe-862"], (
            "a client that cannot render the control wiped it on save"
        )


async def test_an_explicit_null_goes_back_to_the_default():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        await _put(client, suppressed_rules=["sec.cwe-862"])
        saved = await _put(client, suppressed_rules=None)
        assert saved.status_code == 200, saved.text

        policy = await _get(client)
        assert policy["suppressed_rules"] is None
        assert policy["suppressed_rules_effective"] == SIX


async def test_the_list_is_stripped_and_deduplicated_but_a_non_rule_is_refused():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, suppressed_rules=[" quality.todo ", "quality.todo", "a.b"])
        assert saved.status_code == 200, saved.text
        assert saved.json()["suppressed_rules"] == ["quality.todo", "a.b"]

        refused = await _put(client, suppressed_rules=["quality todo"])
        assert refused.status_code == 422
        assert "quality todo" in refused.text

        refused = await _put(client, suppressed_rules=["  "])
        assert refused.status_code == 422


async def test_a_policy_written_before_the_column_existed_reads_as_inherit():
    async with policy_api(
        _workspace(REASONING_MODEL, "google"), rows=[{"prompt_template": "old"}],
    ) as client:
        policy = await _get(client)
        assert policy["prompt_template"] == "old"
        assert policy["suppressed_rules"] is None
        assert policy["suppressed_rules_effective"] == SIX
