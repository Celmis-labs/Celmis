from __future__ import annotations

import pytest_asyncio

from tests.knowledge.pack_rig import rig as _rig


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch):
    async with _rig(tmp_path, monkeypatch) as r:
        yield r
