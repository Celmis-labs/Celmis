"""The review analytics page draws what reviews cost only when the API sent it.

The API leaves `cost_usd` / `cost_basis` out for anyone below owner/admin
(tests/api/test_review_cost_is_for_whoever_pays.py); a page that read the
missing figure as 0 would tell an editor the reviews are free.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEW = (ROOT / "web" / "ee" / "analytics" / "analytics-view.tsx").read_text(encoding="utf-8")
API = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")


def test_the_cost_tile_and_its_footnote_wait_for_the_figure():
    assert VIEW.count("s.cost_usd && (") == 2, "tile and footnote are both conditional"


def test_the_type_says_the_figure_may_be_absent():
    start = API.index("export type AnalyticsSummary")
    block = API[start:API.index("findings_by_severity", start)]
    assert "cost_usd?:" in block and "cost_basis?:" in block
