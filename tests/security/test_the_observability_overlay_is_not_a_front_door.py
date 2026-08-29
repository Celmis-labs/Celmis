"""Turning on metrics published three services to the open internet.

`docker-compose.yml` learned this the hard way and says so in its own comment:
`5432:5432` was verified answering from the open internet, so every published
port in that file is bound to `127.0.0.1` and reached over an ssh tunnel.

`docker-compose.observability.yml` did the opposite for all three of its
services. A port with no address in front of it binds `0.0.0.0`, so an
operator who wanted a dashboard also opened:

  * Grafana on 3100, whose credentials defaulted to admin/admin;
  * Prometheus on 9090, which is every metric the instance emits;
  * Loki on 3105, which has no authentication of its own — `auth_enabled` is
    false by default — so `POST /loki/api/v1/push` accepted writes from
    anyone who could reach the port. Log storage that strangers can write to
    is not log storage.

The overlay is opt-in, which makes this worse rather than better: it is turned
on by an operator doing the responsible thing, and the reward is three open
ports. Nothing warned them, because the rule lived as a comment in the other
file.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "docker-compose.observability.yml"
MAIN = ROOT / "docker-compose.yml"


def _published(path: Path) -> list[tuple[str, str]]:
    """(service, mapping) for every published port in the file."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for name, svc in (doc.get("services") or {}).items():
        for entry in svc.get("ports") or []:
            out.append((name, str(entry)))
    return out


#: The one service whose job is to be reachable. Everything else goes through
#: it, or through an ssh tunnel.
PUBLIC_ENTRYPOINT = "caddy"


def _naked(path: Path) -> list[str]:
    """Published ports with no address in front of them — i.e. on 0.0.0.0.

    An interpolated `${VAR}` in the host slot is still naked: the variable
    supplies a port number, not an address.
    """
    return [
        f"{svc}: {mapping}" for svc, mapping in _published(path)
        if svc != PUBLIC_ENTRYPOINT
        and not re.match(r"^(127\.0\.0\.1|localhost|\[::1\]):", mapping)
    ]


@pytest.mark.parametrize("path", [OVERLAY, MAIN], ids=["overlay", "main"])
def test_only_the_reverse_proxy_listens_on_every_interface(path: Path):
    """`"9090:9090"` binds 0.0.0.0. `"127.0.0.1:9090:9090"` does not.

    Held for BOTH files, because the rule belongs to the repository and not to
    one file — the overlay drifted precisely because the rule was written down
    in the other one.

    `caddy` is exempt and is the only thing that may be: it is the front door,
    it terminates the one origin the product is served on, and everything
    behind it is reached through it. A SECOND service appearing in this list
    is the failure, whatever it is called.
    """
    assert not _naked(path), (
        f"{path.name} publishes on 0.0.0.0: {_naked(path)}. Bind to 127.0.0.1 "
        f"and reach it over `ssh -L`, as docker-compose.yml does for postgres."
    )


def test_the_exemption_is_one_service_and_it_is_the_proxy():
    """A guard on the guard: if caddy stops being the entrypoint, or a second
    service takes on the job, the exemption above is silently wrong."""
    doc = yaml.safe_load(MAIN.read_text(encoding="utf-8"))
    svc = (doc.get("services") or {}).get(PUBLIC_ENTRYPOINT)
    assert svc is not None, "the exempt service no longer exists"
    assert "caddy" in str(svc.get("image", "")).lower(), (
        "the exempt service is no longer a reverse proxy"
    )
    public = [s for s, m in _published(MAIN)
              if not re.match(r"^(127\.0\.0\.1|localhost|\[::1\]):", m)]
    assert public == [PUBLIC_ENTRYPOINT], (
        f"more than one service faces the internet: {public}"
    )


def test_grafana_has_no_default_password():
    """admin/admin on a port anyone can reach is not a password.

    The overlay is opt-in and its README line said "(admin / admin on first
    login)", so the default was documented rather than accidental — which
    means somebody decided it, and it still has to stop.
    """
    text = OVERLAY.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    env = (doc["services"]["grafana"].get("environment") or {})
    password = str(env.get("GF_SECURITY_ADMIN_PASSWORD", ""))

    assert ":-admin}" not in password and password != "admin", (
        "Grafana still defaults to admin/admin"
    )
    assert ":?" in password, (
        "the password must be required, not defaulted: an overlay that starts "
        "with a guessable admin is worse than one that refuses to start"
    )


def test_the_documentation_line_does_not_promise_admin_admin():
    text = OVERLAY.read_text(encoding="utf-8")
    assert "admin / admin" not in text, (
        "the header comment still tells the reader the password is admin"
    )
