# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The filter bar narrows every figure: repository, repository group, author, target branch."""

from __future__ import annotations

import pytest

from tests.ee.productivity_support import api, deployment, merged_pr

URL = "/api/analytics/productivity/overview?days=30"
PRS = [
    merged_pr(1, repo="acme/shop", author="dana", target="main"),
    merged_pr(2, repo="acme/shop", author="lee", target="develop"),
    merged_pr(3, repo="acme/api", author="dana", target="main"),
    merged_pr(4, repo="acme/web", author="lee", target="main"),
]
DEPLOYS = [deployment(1, repo="acme/shop", branch="main"), deployment(2, repo="acme/api", branch="main"),
           deployment(3, repo="acme/shop", branch="develop")]
GROUPS = {"backend": ["acme/shop", "acme/api"], "empty": []}


async def merged(query: str, monkeypatch, tmp_path) -> int:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS,
                   deployments=DEPLOYS, groups=GROUPS) as c:
        r = await c.get(URL + query)
    assert r.status_code == 200, r.text
    return r.json()["kpis"]["merged_prs"]["value"]


@pytest.mark.parametrize(("query", "expected"), [
    ("", 4),
    ("&repo=acme/shop", 2),
    ("&repo=acme/shop&repo=acme/api", 3),
    ("&repo=ACME/Shop", 2),
    ("&group=backend", 3),
    ("&group=backend&repo=acme/api", 1),
    ("&group=backend&repo=acme/web", 0),
    ("&author=dana", 2),
    ("&target=develop", 1),
    ("&repo=acme/shop&author=lee&target=develop", 1),
    ("&repo=acme/api&author=lee", 0),
])
async def test_the_filters_narrow_the_merged_prs(query, expected, monkeypatch, tmp_path) -> None:
    assert await merged(query, monkeypatch, tmp_path) == expected


async def test_an_empty_group_matches_nothing_rather_than_everything(monkeypatch, tmp_path) -> None:
    assert await merged("&group=empty", monkeypatch, tmp_path) == 0


async def test_an_unknown_group_is_a_404_not_an_unfiltered_page(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS, groups=GROUPS) as c:
        assert (await c.get(URL + "&group=ghosts")).status_code == 404


async def test_the_repository_and_target_filters_narrow_the_deployments_too(monkeypatch, tmp_path) -> None:
    async with api(role="owner", monkeypatch=monkeypatch, tmp_path=tmp_path, prs=PRS,
                   deployments=DEPLOYS, groups=GROUPS) as c:
        everything = (await c.get(URL)).json()["kpis"]["deploy_frequency"]["total"]
        shop = (await c.get(URL + "&repo=acme/shop")).json()["kpis"]["deploy_frequency"]["total"]
        develop = (await c.get(URL + "&target=develop")).json()["kpis"]["deploy_frequency"]["total"]
    assert (everything, shop, develop) == (3, 2, 1)
