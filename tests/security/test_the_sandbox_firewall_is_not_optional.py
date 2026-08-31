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


def _blocking_functions() -> dict[str, str]:
    """The helpers `block_sandbox_to_host` delegates to, by name.

    Keyed on what they are called FROM rather than on their names: a helper
    renamed out of this dict would take its assertions with it, and that is
    the failure this file's neighbours keep being rewritten after.
    """
    code = _without_comments()
    wrapper = re.search(r"block_sandbox_to_host\(\) \{.*?\n\}", code, re.S)
    assert wrapper, "block_sandbox_to_host is gone"
    called = re.findall(r"^\s*(_\w+) \"\$subnet\"", wrapper.group(0), re.M)
    assert called, f"the wrapper delegates to nothing:\n{wrapper.group(0)}"

    bodies = {}
    for name in called:
        found = re.search(rf"{name}\(\) \{{.*?\n\}}", code, re.S)
        assert found, f"{name} is called and does not exist"
        bodies[name] = found.group(0)
    return bodies


def test_both_backends_are_tried_before_giving_up() -> None:
    """iptables is what the rules are written in; nft is what a modern host has.

    Measured on the production box: nftables v1.0.9 is installed.
    """
    for name, body in _blocking_functions().items():
        assert "iptables" in body and "nft " in body, (
            f"{name} attempts only one firewall backend"
        )


def test_a_port_published_on_the_host_is_covered_too() -> None:
    """INPUT does not see it, and the first version of this rule did not either.

    Measured from inside the running sandbox with the INPUT rule in place:
    `host:22` timed out, `host:80` answered. A published container port is
    DNAT'd in PREROUTING and forwarded, so INPUT is never consulted. Nothing
    was exposed by it — the only 0.0.0.0 port was Caddy, which the internet
    reaches anyway — but the deploy logged "sandbox→host blocked", which was
    broader than the rule, and the next published port is the one nobody
    re-checks.
    """
    bodies = _blocking_functions()
    covering = {name: body for name, body in bodies.items()
                if "DOCKER-USER" in body or "hook forward" in body}
    assert covering, (
        "nothing the wrapper calls touches the forwarding path, so a container "
        f"port published on the host stays reachable from the sandbox. "
        f"Functions called: {sorted(bodies)}"
    )
    for name, body in covering.items():
        assert "DNAT" in body.upper(), (
            f"{name} filters forwarded traffic without matching the "
            f"destination-NAT that identifies a published port"
        )


def test_the_wrapper_reports_a_partial_block_as_a_failure() -> None:
    """Two rules, one answer. Half of them is not "blocked"."""
    code = _without_comments()
    wrapper = re.search(r"block_sandbox_to_host\(\) \{.*?\n\}", code, re.S).group(0)
    assert re.search(r"return 1", wrapper), (
        "the wrapper never returns failure, so the caller's `fail` is dead code"
    )
    calls = re.findall(r"^\s*(_\w+) \"\$subnet\"", wrapper, re.M)
    assert len(calls) >= 2, (
        f"the wrapper makes {len(calls)} blocking call(s); the host's own "
        f"sockets and its published container ports are different chains and "
        f"need both"
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


def test_the_sandbox_keeps_its_way_out_to_the_internet() -> None:
    """`npm ci` is the point of the container, and a blanket drop kills it.

    The rule that covers published ports lives on the forwarding path, which
    is also the sandbox's route to the internet. What separates them is the
    qualifier: a drop matched on the source subnet ALONE would take both. So
    this is keyed on the qualifier and not on the chain's name — the earlier
    version asserted the string "FORWARD" never appeared anywhere, which a
    rule in DOCKER-USER satisfies while doing precisely what was feared.
    """
    code = _without_comments()
    for line in code.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("iptables", "nft")) or " drop" not in \
                f"{stripped.lower()} ":
            if "-j DROP" not in stripped:
                continue
        forwarding = ("FORWARD" in stripped or "DOCKER-USER" in stripped
                      or "hook forward" in stripped)
        if not forwarding:
            continue
        qualified = ("DNAT" in stripped.upper() or "--dport" in stripped
                     or "status dnat" in stripped)
        assert qualified, (
            f"this drops forwarded traffic from the sandbox with nothing but "
            f"the source subnet to match on, which is also how it reaches the "
            f"internet:\n    {stripped}"
        )
