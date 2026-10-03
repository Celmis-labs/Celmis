# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Every EE test runs against a throwaway licence store.

`mount_enterprise` reads the licence a global admin entered in the UI from
the encrypted credential store. Without this, a test that builds an app with
no licence in its environment would read the developer's own store — and a
real licence entered there would turn the "unlicensed" case enterprise.
"""

from __future__ import annotations

import pytest

from tests.ee.licensing import isolate_license_store


@pytest.fixture(autouse=True)
def _throwaway_license_store(monkeypatch, tmp_path_factory):
    return isolate_license_store(monkeypatch, tmp_path_factory.mktemp("license-store"))
