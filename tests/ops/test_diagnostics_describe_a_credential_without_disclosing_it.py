"""An operator gets the shape of a token, never a piece of it.

`GET /api/ops/diag` returned, for every git connection in every workspace on
the installation:

    "token_prefix": "ghp_Ab…",
    "token_len": 40

and `GET /api/ops/check-repo` returned the same prefix plus `dict(metadata)` —
the credential's metadata unfiltered, while the endpoint beside it already
filtered to four keys.

The reason for having something there was sound. An operator looking at
"clone failed 403" has to tell a wrong-SHAPE credential (an Atlassian ATATT
pasted into a GitHub slot) from a token that is the right kind and merely
lacks access, and those have different fixes. But six characters plus an exact
length is a piece of the secret and a strong narrowing hint, not the shape.

Every question the diagnosis actually asks is answerable without it, and one
of them is answered BETTER: "is the token in this slot the same one as in that
slot" was unanswerable from a shared `ghp_` prefix and is exact from a hash.
"""

from __future__ import annotations

import pytest

from src.ops.credential_shape import (
    KNOWN_PREFIXES,
    describe_token,
    safe_metadata,
    token_fingerprint,
    token_format,
)

GITHUB = "ghp_" + "Ab3Cd4Ef5Gh6Ij7Kl8Mn9Op0Qr1St2Uv3Wx4Y"
FINE = "github_pat_11ABCDEFG0" + "z" * 59
ATLASSIAN = "ATATT3xFfGF0" + "q" * 180


# ─── no part of the secret comes back ────────────────────────────────


@pytest.mark.parametrize("secret", [GITHUB, FINE, ATLASSIAN, "glpat-" + "9" * 20])
def test_no_run_of_the_secret_body_survives(secret):
    """The property, stated as a property: no four-character window of the
    token's BODY appears anywhere in the description. Checking only that the
    output lacks a prefix would pass a description that leaked the tail.

    The body, not the whole string. `ghp_` and `github_pat_` are published
    format markers — GitHub documents them so that scanners can spot a leaked
    token — and naming the format is the entire job here. Everything after the
    marker is the secret.
    """
    prefix = max((p for p in KNOWN_PREFIXES if secret.startswith(p)),
                 key=len, default="")
    body = secret[len(prefix):]
    described = repr(describe_token(secret))
    windows = {body[i:i + 4] for i in range(len(body) - 3)}
    leaked = sorted(w for w in windows if w in described)
    assert not leaked, f"the description contains {leaked} from the token body"


def test_the_format_is_named_not_shown():
    assert token_format(GITHUB) == "github-pat-classic"
    assert token_format(FINE) == "github-pat-fine-grained"
    assert token_format(ATLASSIAN) == "atlassian-api-token"


def test_the_longest_matching_prefix_wins():
    """`github_pat_` starts with neither `ghp_` nor `gho_`, but the table is
    the kind of thing that grows a shorter neighbour later."""
    assert token_format("github_pat_x") == "github-pat-fine-grained"
    assert token_format("sk-ant-api03-x") == "anthropic-key"
    assert token_format("sk-proj-x") == "openai-key"


def test_an_unknown_shape_says_so_rather_than_guessing():
    assert token_format("just-some-string") == "unrecognised"
    assert token_format("") == "empty"


# ─── what it must still be able to answer ────────────────────────────


def test_the_same_token_in_two_slots_is_recognisable():
    """The question the prefix could not answer. Two `ghp_` tokens share six
    characters by construction."""
    assert token_fingerprint(GITHUB) == token_fingerprint(GITHUB)


def test_two_different_tokens_do_not_look_alike():
    a, b = "ghp_" + "A" * 36, "ghp_" + "B" * 36
    assert token_fingerprint(a) != token_fingerprint(b)


def test_a_truncated_paste_is_still_visible():
    """The other thing `token_len` was for: somebody pasted half a token."""
    assert describe_token(GITHUB)["length"] == len(GITHUB)
    assert describe_token(GITHUB[:20])["length"] == 20


def test_an_absent_credential_is_distinguishable_from_an_empty_one():
    assert describe_token(None)["present"] is False
    assert describe_token("")["present"] is False
    assert describe_token("x")["present"] is True


# ─── metadata ────────────────────────────────────────────────────────


def test_metadata_is_an_allow_list_not_a_deny_list():
    """The dict is written by several code paths and nothing stops a future
    one from putting a refresh token in it."""
    out = safe_metadata({
        "atlassian_email": "ops@acme.example",
        "saved_via": "ui",
        "oauth_refresh_token": "MUST-NOT-APPEAR",
        "installation_secret": "MUST-NOT-APPEAR",
    })
    assert out == {"atlassian_email": "ops@acme.example", "saved_via": "ui"}


def test_metadata_survives_being_absent():
    assert safe_metadata(None) == {}


# ─── the endpoints themselves ────────────────────────────────────────


def test_neither_ops_endpoint_still_builds_a_prefix():
    """Keyed on the ops router's own source, with comments and docstrings
    stripped: the module explains at length WHY it no longer returns a prefix,
    and a plain grep would find the explanation.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/api/routers/ops_metrics.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))

    # A slice of a name that looks like a secret is the shape to catch:
    #   secret[:6]   sec[:6]   token[:8]
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        target = node.value
        name = target.id if isinstance(target, ast.Name) else \
            (target.attr if isinstance(target, ast.Attribute) else "")
        if name.lower() in ("secret", "sec", "token", "password", "api_key"):
            offenders.append(f"{name}[…] at line {node.lineno}")
    assert not offenders, f"ops diagnostics slices a credential: {offenders}"

    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docstrings]
    assert "token_prefix" not in literals
