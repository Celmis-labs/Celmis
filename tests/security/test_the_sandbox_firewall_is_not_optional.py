"""The deploy said this step was not optional and then continued without it.

`scripts/deploy-on-server.sh` installs an INPUT DROP for the sandbox subnet so
a container running a tenant's own build and test commands cannot reach the
host. Both failure branches logged `WARNING:` and carried on — five lines after
a real `fail` on a missing `.env`. So a host with no iptables shipped an
unisolated sandbox and said so in a line nobody reads.

Two things are wrong with a warning here, and they are separate. It does not
stop the deploy. And it does not survive a reboot: measured on the production
box, the rule was in place, `iptables-persistent` was absent, and crontab was
EMPTY — so after a restart nothing would put it back until somebody deployed
by hand.

Read with a shell parse and with structure, never by searching for the word
`fail`: the comment explaining this fix contains it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-on-server.sh"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _without_comments() -> str:
    return "\n".join(
        line for line in _source().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_script_still_parses() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0, done.stderr


def test_a_failed_block_stops_the_deploy() -> None:
    """`fail` exits; `log WARNING` does not. Which one runs is the whole fix."""
    code = _without_comments()
    match = re.search(r"if ! block_sandbox_to_host.*?\nfi", code, re.S)
    assert match, "nothing calls block_sandbox_to_host and acts on the answer"
    branch = match.group(0)
    assert "fail " in branch, (
        f"the failure branch does not call `fail`, so the deploy continues "
        f"without the rule:\n{branch}"
    )


def test_there_is_a_typed_way_out_rather_than_a_silent_one() -> None:
    """A host that isolates the sandbox another way must be able to say so.

    An escape hatch that has to be typed is a decision. A warning is not.
    """
    code = _without_comments()
    assert "CELMIS_ALLOW_UNFIREWALLED_SANDBOX" in code
    match = re.search(r"if ! block_sandbox_to_host.*?\nfi", code, re.S)
    assert "CELMIS_ALLOW_UNFIREWALLED_SANDBOX" in match.group(0), (
        "the escape hatch is not on the failure path, so it escapes nothing"
    )


def test_both_backends_are_tried_before_giving_up() -> None:
    """iptables is what the rule is written in; nft is what a modern host has.

    Measured on the production box: nftables v1.0.9 is installed.
    """
    code = _without_comments()
    function = re.search(r"block_sandbox_to_host\(\) \{.*?\n\}", code, re.S)
    assert function, "block_sandbox_to_host is gone"
    body = function.group(0)
    assert "iptables" in body and "nft " in body, (
        "only one firewall backend is attempted"
    )


def test_the_rule_is_reinstated_after_a_reboot() -> None:
    """It is not persistent by itself, and nothing else was putting it back."""
    code = _without_comments()
    assert "celmis-sandbox-firewall.service" in code
    assert "Before=docker.service" in _source(), (
        "the unit does not order itself before docker, so a container can be "
        "started into that subnet before the rule exists"
    )
    assert "systemctl enable" in code, (
        "the unit is written but never enabled, so it does nothing at boot"
    )


@pytest.mark.parametrize("branch", ["iptables -I INPUT 1", "nft add rule"])
def test_the_rule_targets_input_not_forward(branch: str) -> None:
    """Traffic TO the host arrives on INPUT; traffic THROUGH it on FORWARD.

    Dropping the second would cut the sandbox's internet access, which it
    needs — `npm install` is the whole point of the container.
    """
    code = _without_comments()
    assert branch in code
    assert "FORWARD" not in code, (
        "the deploy touches the FORWARD chain, which is the sandbox's route to "
        "the internet rather than its route to this host"
    )
