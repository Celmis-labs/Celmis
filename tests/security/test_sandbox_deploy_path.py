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
# The push deploy is gone: it held a root key to production in the secrets of
# what is now a public repository. Its work moved to two files that need no
# credential anybody outside that machine holds — so these guards moved with
# it, and the ones whose MECHANISM died rather than whose GUARANTEE died were
# deleted rather than re-pointed at something that only looks similar.
INIT_ENV = (ROOT / "scripts" / "init-env.sh").read_text()
DEPLOY_SH = (ROOT / "scripts" / "deploy-on-server.sh").read_text()
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


def test_something_supplies_everything_compose_demands():
    """POSTGRES_PASSWORD is the one hard requirement compose interpolates with
    `:?`, so a .env without it fails EVERY service, not just the one that wants
    it. The workflow used to verify it landed; `init-env.sh` now generates it,
    and the deploy refuses to start without a .env at all.

    Anything added to compose's `:?` set needs the same treatment."""
    assert '"POSTGRES_PASSWORD"' in INIT_ENV, "nothing generates it any more"
    assert ".env is missing" in DEPLOY_SH, (
        "the deploy would run against a compose file that cannot interpolate"
    )


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


def test_the_example_file_does_not_override_that_address_with_another():
    """The test above guarded the file nobody edits.

    `.env` beats the compose default, `init-env.sh` writes `.env` from
    `.env.example`, and the example said 8080 while every other place said
    8900 — Dockerfile.sandbox's EXPOSE, the compose default, the healthcheck,
    the client default, and the assertion directly above. So the one file an
    operator actually copies was the only one that disagreed, and the api
    could not reach its own sandbox on any install that followed the README.

    Empty is the answer here: `${SANDBOX_URL:-http://sandbox:8900}` supplies
    the pair when the variable is unset OR empty, so one place names it.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("SANDBOX_URL="):
            continue
        value = line.partition("=")[2].strip()
        assert value in ("", "http://sandbox:8900"), (
            f".env.example sets SANDBOX_URL={value!r}; the sandbox listens on "
            f"8900 and this file is what init-env.sh copies into every .env"
        )
        break
    else:
        raise AssertionError("SANDBOX_URL vanished from .env.example")


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


def test_the_token_is_provisioned():
    """Generated, not asked for. The sandbox refuses to start without it, so a
    setup step that leaves it blank breaks the whole stack — which is exactly
    what `${SANDBOX_TOKEN:?required}` did."""
    assert '"SANDBOX_TOKEN"' in INIT_ENV
    assert "token_hex(32)" in INIT_ENV, "32 bytes, as the server checks for"


# `test_it_is_kept_outside_the_directory_the_sync_deletes` was deleted with the
# mechanism it described, not with the guarantee. It existed because
# `rsync --delete` into celmis/ wiped anything not in the repository, so a
# token stored there was regenerated every deploy and 403'd the session in
# flight. Nothing rsyncs any more — the images come from a registry and .env
# is written once, on the server, by a script that never touches a value it
# already finds. Re-pointing that assertion at some other directory would have
# been a test about a hazard that no longer exists.


def test_the_token_survives_being_filled_in_again():
    """The .env write used to truncate, so appending before it wrote into a
    file about to be replaced. `init-env.sh` fills blanks in place instead —
    and the property that matters is the same one: a token already in the file
    must come out unchanged. Rotating it under a running api is a 403 on the
    next call."""
    assert "Idempotent" in INIT_ENV
    assert "already has a value is never touched" in INIT_ENV


def test_the_token_is_never_printed():
    """A secret that reaches a log has left the machine.

    The generator writes values into the file and reports only NAMES: it says
    `filled 3: MCP_JWT_SECRET, …`, never what it put in them. `lines[i]` is
    where a value is produced and it goes straight into the file.

    Keyed on the generator call, not on the word "value" — the first version of
    this assertion failed on the sentence "the earlier value is silently
    discarded", which is prose about duplicate keys. A word is not the thing it
    names, which is the mistake this whole file exists to catch elsewhere.
    """
    import re

    for call in re.findall(r"print\(([^\n]*)\)", INIT_ENV):
        assert "GENERATORS[" not in call, call
        assert "secrets." not in call, call
        assert "lines[" not in call, call


def test_the_summary_names_what_it_filled():
    """The other half: reporting nothing would be safe and useless. An operator
    has to know which blanks were filled to know what to back up."""
    assert "filled {len(filled)}" in INIT_ENV
    assert "sorted(filled)" in INIT_ENV


def test_it_is_generated_once_rather_than_every_run():
    """Rotating it would 403 any session in flight while api restarts ahead of
    the sandbox. The generator only ever fills a blank."""
    assert "nothing to fill" in INIT_ENV


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
    restores the alias.

    The workflow that used to carry this is gone; the script that replaced it
    carries the same three steps, so the assertion moved rather than the
    guarantee.
    """
    after_up = DEPLOY_SH[DEPLOY_SH.find("$COMPOSE up -d"):]
    assert "getent hosts" in after_up
    assert "force-recreate" in after_up
    assert "does not resolve by service name" in after_up, (
        "it warns but never fails, so a broken deploy still reports success"
    )


def test_the_repair_runs_even_when_up_d_fails():
    """The guard existed on the deploy that took the box down, and never ran.

    With `set -e`, a bare `$COMPOSE up -d` aborts the moment api fails its
    healthcheck — which is precisely the symptom of the alias loss the guard
    repairs. The repair has to survive its own trigger.
    """
    start = DEPLOY_SH.find("UP_FAILED")
    assert start > 0, "the non-fatal up-and-repair block is gone"
    block = DEPLOY_SH[start:]
    assert "$COMPOSE up -d || UP_FAILED=1" in block, (
        "up -d is fatal again, so the repair below it is unreachable"
    )
    assert block.index("UP_FAILED=1") < block.index("getent hosts"), (
        "the repair must come after the non-fatal up, not before"
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
