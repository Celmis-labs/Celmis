"""The gold scenarios run through the REAL ``/mcp/dev/`` endpoint (tools lane).

Same runner and same gold file as ``scripts/dev_mcp_e2e.py --spawn``; this is the
in-pytest form, so a regression in a tool shows up in the ordinary suite.
"""

from __future__ import annotations

import pytest

import scripts.dev_mcp_e2e as R
from tests.e2e_local.client import McpClient
from tests.e2e_local.stack import Stack

pytestmark = pytest.mark.needs_lane("tools")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("scen")) as st:
        assert st.world is not None
        client = McpClient(st.url, "/mcp/dev/", st.token("owner", repos=["*"]))
        secrets_ = [R.Secret(k, v) for k, v in st.world.canaries.items()]
        yield R.run_scenarios(client, R.load_scenarios(R.BUNDLED_GOLD), clones=st.world.repos,
                              secrets_=secrets_)


def test_every_scenario_passes_on_the_real_tools(report) -> None:
    bad = {r.id: (r.celmis.errors, r.celmis.names_missing, r.celmis.sources_missing,
                  r.celmis.text_missing, r.celmis.idx_missing_on, r.celmis.over_budget)
           for r in report.results if r.celmis and not r.celmis.passed}
    assert not bad and report.ok, R.render(report)


def test_nothing_skipped_because_a_repository_was_invisible(report) -> None:
    assert [r.id for r in report.results if r.skipped] == []


def test_no_secret_label_is_reported_for_any_tool(report) -> None:
    assert report.leaks == []


def test_the_tools_list_stays_small(report) -> None:
    assert report.tools_list_count >= 9
    assert report.tools_list_chars < 6000
