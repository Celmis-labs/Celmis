"""A token cut to 128 characters is recognised by both tools before the server is
asked, and neither prints what it holds.

The failure: the password prompt of macOS's `security` command (a bare `-w`) keeps only
the first 128 characters. An MCP token is a JWT, longer than that, so a person who
followed the documentation stored `<header>.<part of the payload>` (two parts) and
the server answered 401 invalid_token. A publish token (`cpk_...`, 47 characters)
is not affected, and cannot be a JWT.

The contract:

  * `celmis-pack verify` (the only command that reads your own token) refuses a
    token that starts `eyJ` but is not three non-empty parts joined by dots: exit 2,
    before any request, with its length in the message and not its value;
  * `bin/celmis-auth-header` refuses the same: exit 1, nothing on stdout, nothing
    but the length on stderr;
  * an intact three-part token passes both, and tokens that are not JWTs (they never
    start `eyJ`) are not second-guessed.
"""

from __future__ import annotations

import base64
import json

import pytest

from tests.plugin.test_every_request_celmis_pack_makes_says_who_it_is import celmis
from tests.plugin.test_the_auth_header_helper_prints_json_and_never_the_token_elsewhere import _run
from tests.plugin.test_the_celmis_pack_tool_publishes_checks_and_fetches_a_pack import (
    GOOD,
    cli,
    pack_folder,
    run,
    stub,
)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_jwt() -> str:
    """The shape of an MCP token: 448 characters, three parts, none of them a real secret."""
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": "someone", "pad": "x" * 240}).encode())
    return f"{header}.{payload}.{b64(b'0' * 32)}"


JWT = make_jwt()
CUT = JWT[:128]                                  # what the macOS prompt keeps

CUT_SHORT = {
    "the first 128 characters": CUT,
    "the header only": JWT.split(".")[0],
    "a fourth part": JWT + ".extra",
    "an empty last part": JWT.rsplit(".", 1)[0] + ".",
    "an empty middle part": JWT.split(".")[0] + ".." + JWT.split(".")[2],
}


def test_the_fixture_is_what_the_failure_looked_like():
    assert len(JWT) > 400 and len(CUT) == 128 and CUT.count(".") == 1 and JWT.count(".") == 2


# ─── celmis-pack ─────────────────────────────────────────────────────


@pytest.fixture
def keychain(monkeypatch):
    entries: dict[str, str] = {}
    monkeypatch.setattr(cli, "_keychain", lambda service: entries.get(service, ""))
    monkeypatch.delenv("CELMIS_TOKEN", raising=False)
    return entries


@pytest.mark.parametrize("token", CUT_SHORT.values(), ids=CUT_SHORT.keys())
def test_celmis_pack_refuses_a_token_that_looks_cut_short_and_says_how_long_it_was(keychain, token):
    keychain["celmis-token"] = token
    with pytest.raises(cli.Stop) as caught:
        cli.consumer_token()
    stop = caught.value
    assert stop.code == cli.EXIT_USAGE
    assert "cut short" in stop.message and f"{len(token)} characters" in stop.message
    assert "README" in stop.message and "128" in stop.message
    assert token not in stop.message and token[:40] not in stop.message


def test_a_cut_keychain_entry_is_refused_even_when_the_variable_holds_the_whole_token(keychain, monkeypatch):
    """The agent's helper reads the keychain first, so that is the token `verify` must judge."""
    keychain["celmis-token"] = CUT
    monkeypatch.setenv("CELMIS_TOKEN", JWT)
    with pytest.raises(cli.Stop, match="cut short"):
        cli.consumer_token()


def test_celmis_pack_accepts_the_whole_token_and_leaves_tokens_that_are_not_jwts_alone(keychain, monkeypatch):
    keychain["celmis-token"] = JWT
    assert cli.consumer_token() == JWT
    for other in ("consumer.token-1234", "eyA.not-a-jwt", "abc", "ey" + "Ab0._-" * 66 + "xyz"):
        keychain["celmis-token"] = other
        assert cli.consumer_token() == other


def test_verify_stops_on_a_cut_token_before_any_request_and_prints_neither_token(tmp_path):
    with stub(celmis) as s:
        code, out, err = run("verify", pack_folder(tmp_path), url=s.url, token=GOOD, consumer=CUT)
    assert s.requests == [], "the server was asked although the token was known to be cut"
    assert (code, out) == (2, "")
    assert "cut short" in err and "128 characters" in err
    assert CUT not in err and CUT[:40] not in err and GOOD not in err


def test_verify_goes_on_with_the_whole_token(tmp_path):
    with stub(celmis) as s:
        code, out, err = run("verify", pack_folder(tmp_path), url=s.url, token=GOOD, consumer=JWT)
    assert code != 2 and "cut short" not in err, (out, err)
    assert ("POST", "/mcp/dev/") in {(m, p) for m, p, _h, _b in s.requests}
    assert JWT not in out + err


def test_the_pack_token_is_never_read_as_a_jwt(tmp_path):
    """Only `cpk_` tokens are accepted as publish tokens, so a JWT there is 'not a pack token'."""
    code, _out, err = run("status", url="https://celmis.example.com", token=CUT)
    assert code == 2 and "not a pack token" in err and "cut short" not in err


# ─── celmis-auth-header ──────────────────────────────────────────────


@pytest.mark.parametrize("token", CUT_SHORT.values(), ids=CUT_SHORT.keys())
def test_the_helper_refuses_a_token_that_looks_cut_short_and_prints_nothing_of_it(token, tmp_path):
    done = _run({"CELMIS_TOKEN": token}, tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert "cut short" in done.stderr and f"{len(token)} characters" in done.stderr
    assert token not in done.stderr and token[:40] not in done.stderr


def test_the_helper_prints_the_whole_token_as_the_header_and_only_there(tmp_path):
    done = _run({"CELMIS_TOKEN": JWT}, tmp_path)
    assert done.returncode == 0 and done.stderr == ""
    assert json.loads(done.stdout) == {"Authorization": f"Bearer {JWT}"}


@pytest.mark.parametrize("token", ["abc.DEF-123_xyz~+/=", "eyA.b", "cpk_" + "a" * 43])
def test_the_helper_does_not_second_guess_a_token_that_is_not_a_jwt(token, tmp_path):
    done = _run({"CELMIS_TOKEN": token}, tmp_path)
    assert done.returncode == 0 and json.loads(done.stdout) == {"Authorization": f"Bearer {token}"}
