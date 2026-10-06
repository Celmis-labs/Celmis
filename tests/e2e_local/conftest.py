"""``needs_lane(name)``: skip a test until the named lane's code is on the branch.

The access and tools lanes are developed in parallel with this one. Their
tests are written first and skip by FEATURE PROBE (not by a date or a flag)
until the code they exercise exists. ``CELMIS_E2E_STRICT=1`` turns the skip
into a failure; the integrator sets it, so nothing can stay skipped by accident
after the merge.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e_local.stack import lane_present


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "needs_lane(name): requires the 'access' or 'tools' lane's code "
                   "(skipped by feature probe; CELMIS_E2E_STRICT=1 makes it a failure)")


def pytest_runtest_setup(item) -> None:
    for marker in item.iter_markers(name="needs_lane"):
        lane = marker.args[0]
        if lane_present(lane):
            continue
        message = f"the '{lane}' lane is not on this branch yet"
        if os.environ.get("CELMIS_E2E_STRICT") == "1":
            pytest.fail(message + " (CELMIS_E2E_STRICT=1)")
        pytest.skip(message)
