"""The policy router stores ignore globs and the comment threshold.

Both follow the three-state rule `suppressed_rules` set: the key ABSENT keeps
what is stored (a client that cannot render the control must not wipe it),
null clears, a value replaces. Bad values are refused with a 422 that names
them, never stored to match nothing.
"""

from __future__ import annotations

from tests.api.test_the_policy_page_carries_the_ceiling import (
    REASONING_MODEL,
    _get,
    _put,
    _workspace,
    policy_api,
)


async def test_defaults_say_what_is_inherited():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        policy = await _get(client)
        assert policy["ignore_globs"] == []
        assert policy["comment_min_severity"] is None
        assert policy["comment_min_severity_effective"] == "info"


async def test_saved_values_come_back_cleaned():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        saved = await _put(client, ignore_globs=[" docs/** ", "", "*.snap"],
                           comment_min_severity="Warning")
        assert saved.status_code == 200, saved.text
        policy = await _get(client)
        assert policy["ignore_globs"] == ["docs/**", "*.snap"]
        assert policy["comment_min_severity"] == "warning"
        assert policy["comment_min_severity_effective"] == "warning"

        # A save that does not mention them keeps them.
        await _put(client, prompt_template="be brief")
        policy = await _get(client)
        assert policy["ignore_globs"] == ["docs/**", "*.snap"]
        assert policy["comment_min_severity"] == "warning"

        # null clears.
        await _put(client, ignore_globs=None, comment_min_severity=None)
        policy = await _get(client)
        assert policy["ignore_globs"] == []
        assert policy["comment_min_severity"] is None


async def test_bad_values_are_refused():
    async with policy_api(_workspace(REASONING_MODEL, "google")) as client:
        r = await _put(client, comment_min_severity="nits")
        assert r.status_code == 422 and "comment_min_severity" in r.text
        r = await _put(client, ignore_globs=["!keep.py"])
        assert r.status_code == 422 and "ignore_globs" in r.text
        r = await _put(client, ignore_globs=["**"])
        assert r.status_code == 422
