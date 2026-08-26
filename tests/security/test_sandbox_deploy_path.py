"""The sandbox has to actually reach production, and fail safely when it does not.

Two defects shipped together in the sandbox commit, and neither could be seen
from the source alone:

  * `${SANDBOX_TOKEN:?required}` in compose. Compose resolves interpolation for
    the WHOLE file before starting anything, so a .env without that one
    variable failed postgres, api and web too. Every deploy after that commit
    stopped at "Provision + build + deploy", which means the sandbox itself
    never deployed either.
  * The api service was never given `SANDBOX_TOKEN` or `SANDBOX_URL`. The
    service was on the network, the client read an empty token, `available()`
    returned False — the feature was unreachable by construction.

And one that was latent: `do_POST` checks the token as `if TOKEN and …`, so an
EMPTY token accepts every request. In a process whose whole job is running
shell commands, "no token configured" must never mean "no authentication".
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DEPLOY = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
SERVER = (ROOT / "src" / "sandbox" / "server.py").read_text()


def _service(name: str) -> str:
    i = COMPOSE.find(f"\n  {name}:")
    assert i > 0, f"no {name} service"
    nxt = re.search(r"\n  [a-z_]+:", COMPOSE[i + 3:])
    return COMPOSE[i:i + 3 + nxt.start()] if nxt else COMPOSE[i:]


def _uncommented(text: str) -> str:
    """Comments below NAME what must not come back, to explain why it is gone.

    Grepping them is how a guard passes while the thing it guards is undone.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ─── the deploy must not be hostage to an optional feature ───────────


def test_no_optional_feature_can_block_the_whole_deploy():
    """Interpolation is resolved for the entire file before anything starts,
    so one `:?required` on a service nobody needs takes down the database."""
    required = set(re.findall(r"\$\{([A-Z_]+):\?", _uncommented(COMPOSE)))
    # POSTGRES_PASSWORD and CELMIS_JWT_SECRET are not optional FEATURES —
    # they are the two values without which no request can be served at all.
    # Failing the whole file is the right blast radius for them: a stack that
    # comes up without either is a stack that is broken in a way somebody
    # will diagnose an hour later, from a runtime error, instead of at
    # `compose up` with the variable's own name on screen.
    #
    # Anything else appearing here IS the bug this test was written for: an
    # optional feature taking the database down with it.
    assert required <= {"POSTGRES_PASSWORD", "CELMIS_JWT_SECRET"}, (
        f"these now block every service if missing from .env: "
        f"{sorted(required - {'POSTGRES_PASSWORD'})}"
    )


def test_the_deploy_supplies_everything_compose_demands():
    """POSTGRES_PASSWORD is the one hard requirement, and the workflow verifies
    it landed. Anything else added to that set needs the same treatment."""
    assert 'grep -q "^POSTGRES_PASSWORD=" celmis/.env' in DEPLOY


# ─── the api half has to be told the secret and the address ──────────


def test_the_api_knows_where_the_sandbox_is_and_how_to_authenticate():
    api = _uncommented(_service("api"))
    assert "SANDBOX_TOKEN:" in api, (
        "the api reads SANDBOX_TOKEN from its environment; without it "
        "available() is False and the sandbox can never be used"
    )
    assert "SANDBOX_URL:" in api


def test_the_two_halves_read_the_same_variable():
    """They authenticate to each other with it. Two different names is a
    sandbox that answers 403 to the only caller it has."""
    for service in ("api", "sandbox"):
        assert "${SANDBOX_TOKEN" in _service(service), service


def test_the_client_defaults_match_the_compose_address():
    from src.agent import sandbox

    assert "sandbox:8900" in sandbox.SANDBOX_URL or "SANDBOX_URL" in COMPOSE
    assert "http://sandbox:8900" in COMPOSE


# ─── an empty token is not "no authentication" ───────────────────────


def test_the_server_refuses_to_run_without_a_token():
    """`do_POST` guards with `if TOKEN and …`, so empty means every request is
    accepted — by a process that runs shell commands."""
    main = SERVER[SERVER.find("def main()"):]
    assert "if not TOKEN:" in main
    assert "SystemExit" in main
    # Before it binds anything.
    assert main.find("if not TOKEN:") < main.find("ThreadingHTTPServer")


def test_an_empty_token_still_disables_the_check_so_the_guard_above_matters():
    """Pins the reason. If the request check ever becomes unconditional this
    test fails, and the startup refusal can be reconsidered on purpose rather
    than removed by accident."""
    assert "if TOKEN and self.headers.get" in SERVER


def test_the_api_side_treats_no_token_as_no_sandbox():
    from src.agent import sandbox

    body = (ROOT / "src" / "agent" / "sandbox.py").read_text()
    assert "if not SANDBOX_TOKEN:" in body
    assert sandbox.SandboxResult(False, None, error="no sandbox configured").succeeded is False


# ─── provisioning ────────────────────────────────────────────────────


def test_the_token_is_provisioned_by_the_deploy():
    assert "Provision the sandbox token" in DEPLOY
    assert "/dev/urandom" in DEPLOY


def test_it_is_kept_outside_the_directory_the_sync_deletes():
    """`rsync --delete` into celmis/ removes anything not in the repo, so a
    token stored there is regenerated every deploy — and a token that changes
    under a running api is a 403 on the next call."""
    sync = DEPLOY[DEPLOY.find("Sync repo to server"):]
    assert "--delete" in sync[:400], "the premise changed; recheck this test"
    provision = DEPLOY[DEPLOY.find("Provision the sandbox token"):]
    provision = provision[:provision.find("- name:", 10)]
    assert "~/celmis-secrets" in provision
    assert "celmis/celmis-secrets" not in provision


def test_it_is_appended_after_the_env_file_is_written():
    """The .env write truncates. Appending before it writes into a file that
    is about to be replaced."""
    assert DEPLOY.find("Write .env on server") < DEPLOY.find("Provision the sandbox token")


def test_the_token_is_never_printed():
    provision = DEPLOY[DEPLOY.find("Provision the sandbox token"):]
    provision = provision[:provision.find("- name:", 10)]
    assert "echo \"$(cat ~/celmis-secrets" not in provision
    assert "cat ~/celmis-secrets/sandbox_token" in provision  # only into .env
    # The verification checks a shape, not a value.
    assert 'grep -q "^SANDBOX_TOKEN=.\\{32,\\}"' in provision


def test_it_is_generated_once_rather_than_every_deploy():
    """Rotating it on every deploy would 403 any session in flight while api
    restarts ahead of the sandbox."""
    provision = DEPLOY[DEPLOY.find("Provision the sandbox token"):]
    assert "[ ! -s ~/celmis-secrets/sandbox_token ]" in provision


# ─── the outage this shape caused ────────────────────────────────────


def test_the_default_network_definition_stays_put():
    """Changing it at all is what breaks DNS, in either direction.

    Declaring `default: {driver: bridge}` — which is exactly what the implicit
    network already is — still counts as changing the definition, so compose
    recreates the network. It then RECONNECTS the containers it is not
    recreating, and a reconnect does not restore the service-name alias: the
    container keeps its id and quietly answers only to `celmis-postgres`,
    while `postgres` resolves nowhere. The box goes down with "Temporary
    failure in name resolution" and every container still looks healthy.

    Undeclaring it does the same thing, which is how the fix for the first
    outage caused the second one. Verified on the server: an implicit-default
    service gets its alias perfectly well in a clean project, so the
    declaration is not what matters — CHANGING it is.
    """
    networks = COMPOSE[COMPOSE.rindex("\nnetworks:"):]
    body = _uncommented(networks)
    assert "sandbox_net:" in body
    assert "default:" not in body, (
        "redeclaring `default` changes its definition and forces a network "
        "recreate; referencing it from a service's networks list needs no "
        "declaration here"
    )


def test_the_deploy_checks_service_dns_after_bringing_things_up():
    """The failure was invisible to everything the deploy looked at: images
    built, containers started, healthchecks pending. Only a name lookup shows
    it, so the deploy does one — and repairs it, since recreating the service
    restores the alias."""
    after_up = DEPLOY[DEPLOY.find("$COMPOSE up -d"):]
    assert "getent hosts" in after_up
    assert "force-recreate" in after_up
    assert "does not resolve by service name" in after_up, (
        "it warns but never fails, so a broken deploy still reports success"
    )


def test_the_repair_runs_even_when_up_d_fails():
    """The guard existed on the deploy that took the box down, and never ran.

    With `set -e`, a bare `$COMPOSE up -d` aborts the script the moment api
    fails its healthcheck — which is precisely the symptom of the alias loss
    the guard repairs. The repair has to survive its own trigger.
    """
    start = DEPLOY.find("UP_FAILED")
    assert start > 0, "the non-fatal up-and-repair block is gone"
    # …to the NEXT prune after it. There is an earlier `image prune -af`
    # between the builds, and slicing to the first match gives an empty block
    # that every assertion below then passes over in silence.
    block = DEPLOY[start:DEPLOY.find("docker image prune", start)]
    assert block.strip(), "empty slice — the markers moved"
    assert "$COMPOSE up -d || UP_FAILED=1" in block, (
        "up -d is fatal again, so the repair below it is unreachable"
    )
    assert block.index("UP_FAILED=1") < block.index("getent hosts"), (
        "the repair must come after the non-fatal up, not before"
    )
    assert "retrying up -d after alias repair" in block, (
        "a first attempt that failed for a now-removed reason is never retried"
    )


def test_a_missing_sandbox_token_is_said_out_loud_but_stops_nothing_else(monkeypatch):
    """The failure that made this expensive was silence, not severity.

    SANDBOX_TOKEN lived in .env.example as a comment, so init-env.sh could
    not generate it; setup reported success and the sandbox container then
    refused to start. The answer is NOT `${SANDBOX_TOKEN:?}` — compose
    resolves the whole file first, so that would take postgres down for a
    feature most installs never touch (the test above). It is a warning at
    api startup that names what is lost.
    """
    from src.deployment import warn_if_sandbox_is_unusable

    monkeypatch.setenv("SANDBOX_TOKEN", "")
    message = warn_if_sandbox_is_unusable()
    assert message and "init-env.sh" in message, (
        "the warning must say how to fix it, not only that it is broken"
    )
    assert "Everything else works" in message, (
        "an operator reading this must not think the install is dead"
    )

    monkeypatch.setenv("SANDBOX_TOKEN", "a" * 64)
    assert warn_if_sandbox_is_unusable() is None


def test_the_startup_report_carries_it(monkeypatch):
    from src.deployment import run_startup_checks

    monkeypatch.setenv("SANDBOX_TOKEN", "")
    report = run_startup_checks(strict_secret=False)
    assert "sandbox" in report

    monkeypatch.setenv("SANDBOX_TOKEN", "b" * 64)
    assert "sandbox" not in run_startup_checks(strict_secret=False)
