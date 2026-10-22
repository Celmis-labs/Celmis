# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The world of repository sharing, with the licence mounted the way production does it.

Not a test module (no ``test_`` prefix). It rides on `tests/api/sharing_world.py`
(workspace B gives workspace A one repository; two real tenants, real routers,
SQLite) and then calls ``mount_enterprise`` on the world's own app with a licence
signed by the suite's throwaway key. So the routes under test are the ones
``_router_for("sharing")`` returns, behind the per-request licence check
(``_still_licensed``), not a router mounted past it.

Two fixtures, registered by `tests/ee/conftest.py`:

* ``gw`` ("granting world"): the licence grants ``sharing``.
* ``nw`` ("no licence"): the community edition. Nothing enterprise is mounted
  except the single sign-on allowance.

``relicense(world, features)`` swaps the licence while the app runs, the way
``PUT /api/license`` and ``DELETE /api/license`` do: no features means no licence.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable

import pytest

from src.ee import license as lic
from src.ee import mount_enterprise
from tests.api.sharing_world import shared_world
from tests.ee.licensing import mint_test_license, trust_test_key


def relicense(world, features: Iterable[str] = ()) -> None:
    """Mount what a licence for ``features`` grants and unmount the rest; with no
    features, the community edition."""
    names = list(features)
    env = {lic.ENV_KEY: mint_test_license(names)} if names else {}
    mount_enterprise(world.app, env=env)


@contextlib.asynccontextmanager
async def licensed_world(tmp_path, monkeypatch, features: Iterable[str] = ("sharing",)):
    trust_test_key(monkeypatch)
    monkeypatch.delenv(lic.ENV_KEY, raising=False)
    monkeypatch.delenv(lic.ENV_FILE, raising=False)
    async with shared_world(tmp_path, monkeypatch) as w:
        relicense(w, features)
        yield w


@pytest.fixture
async def gw(tmp_path, monkeypatch):
    """The sharing world under a licence that grants ``sharing``."""
    async with licensed_world(tmp_path, monkeypatch) as w:
        yield w


@pytest.fixture
async def nw(tmp_path, monkeypatch):
    """The sharing world with no licence at all."""
    async with licensed_world(tmp_path, monkeypatch, ()) as w:
        yield w


GRANTS = "/api/sharing-grants"


async def create(w, *, who: str = "admin_b", ws: str = "ws-b", **over):
    """POST a new share: B's repository to A, unless ``over`` says otherwise."""
    from tests.api.rbac_world import B_REPO

    body = {"repo_slug": B_REPO, "workspace": "ws-a"}
    body.update(over)
    return await w.client.post(GRANTS, json=body, headers=w.h(who, ws))


async def extend(w, share_id: str, body: dict, *, who: str = "admin_b", ws: str = "ws-b"):
    return await w.client.post(f"{GRANTS}/{share_id}/extend", json=body, headers=w.h(who, ws))
