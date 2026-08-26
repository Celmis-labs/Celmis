""".env.example is copied verbatim by every new install, so it is code.

A dotenv file has NO inline comments. `KEY=   # openssl rand -hex 24` does
not set an empty value with a note beside it — it sets the value to the
literal string "# openssl rand -hex 24". An operator following the README
therefore ran with a signing secret, a master-admin password and a LiteLLM
salt that are all PUBLISHED IN THIS REPOSITORY.

Three guards should have caught it and none did: the value is 22 characters
so the length floor passed it, it matches no placeholder marker so that gate
passed it, and docker-compose's `${VAR:?}` only fires on EMPTY, so the
compose check passed it too. That is why the rule these tests pin is the
crude one — a secret has no spaces — rather than a longer list of markers:
markers enumerate the mistakes already made, whitespace catches the shape.
"""

from __future__ import annotations

import os
import pathlib
import re
from pathlib import Path

import pytest
from dotenv import dotenv_values

from src.api.jwt_auth import secret_problem

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / ".env.example"

#: Variables whose value is legitimately prose or a list rather than a secret.
#: Kept explicit: a name added here is a claim that the value is never used as
#: a credential.
#: Values a third party issues; neither this repo nor its setup script can
#: produce them, and an install works without them.
_THIRD_PARTY = frozenset({
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_ID",
    "GEMINI_API_KEY", "QDRANT_API_KEY", "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "EMBEDDING_API_KEY",
})

NOT_SECRETS = frozenset({
    "CELMIS_CORS_ORIGINS", "CELMIS_MASTER_EMAIL", "COMPOSE_PROFILES",
    "EMBEDDING_DOCUMENT_PREFIX", "EMBEDDING_QUERY_PREFIX",
})


def _values() -> dict[str, str | None]:
    return dotenv_values(EXAMPLE)


def test_the_file_parses_and_is_not_empty():
    values = _values()
    assert len(values) > 20, "the example lost its contents"


@pytest.mark.parametrize("name", sorted(_values()))
def test_no_variable_is_set_to_a_sentence(name: str):
    """The failure mode, stated as a property: a value that reads like an
    instruction IS the instruction, and it is public."""
    value = (_values().get(name) or "").strip()
    if not value or name in NOT_SECRETS:
        return
    assert not value.startswith("#"), (
        f"{name} is set to a comment. Put the note on its own line ABOVE the "
        f"variable — everything after `=` is the value."
    )
    assert not any(ch.isspace() for ch in value), (
        f"{name}={value!r} contains whitespace, so it is prose. Either it is a "
        f"secret (and must not be shipped) or it is a note (and belongs on its "
        f"own line)."
    )


def test_every_shipped_secret_would_be_refused_at_startup():
    """Whatever the example ships must never be usable AS a secret: it is
    published. Empty is the only correct value here."""
    for name, value in _values().items():
        if name in NOT_SECRETS or not (value or "").strip():
            continue
        if not any(word in name for word in ("SECRET", "KEY", "TOKEN", "PASSWORD")):
            continue
        assert secret_problem(value) is not None, (
            f"{name} ships a value the startup gate would ACCEPT — an install "
            f"that copies this file runs on a credential anyone can read here."
        )


def test_the_guard_itself_refuses_the_shape_that_got_through():
    """The three real values that shipped, and were accepted by every gate."""
    for shipped in (
        "# openssl rand -hex 32",
        "# openssl rand -hex 24 — never rotate",
        "# shared with GitHub",
    ):
        assert secret_problem(shipped) is not None, shipped
        assert secret_problem(shipped, check_length=False) is not None, (
            "the MCP path checks without the length rule and must refuse it too"
        )


def test_a_real_secret_still_passes():
    """The rule must not refuse a healthy install."""
    assert secret_problem("a3f1" * 16) is None
    assert secret_problem("sk-proj-" + "x" * 40) is None


# ─── Duplicates ─────────────────────────────────────────────────────
#
# A dotenv parser keeps the LAST assignment of a name. So a file that
# assigns one twice does not fail — it silently discards the earlier value,
# and for CREDENTIAL_MASTER_KEY that means every credential already stored
# under the first key becomes unreadable, with nothing anywhere saying why.
# Found in a real hand-assembled .env; the example must never model it.


def _assignments() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for lineno, line in enumerate(EXAMPLE.read_text().splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name = text.split("=", 1)[0].strip()
        if name and name.replace("_", "").isalnum() and name.isupper():
            out.append((name, lineno))
    return out


def test_no_variable_is_assigned_twice():
    seen: dict[str, int] = {}
    dupes: list[str] = []
    for name, lineno in _assignments():
        if name in seen:
            dupes.append(f"{name}: lines {seen[name]} and {lineno}")
        seen[name] = lineno
    assert not dupes, (
        "a dotenv parser keeps the last assignment, so the earlier value is "
        "discarded without a word:\n  " + "\n  ".join(dupes)
    )


def test_the_generator_refuses_a_duplicate_rather_than_picking_one():
    """scripts/init-env.sh is what an operator runs; it must not paper over
    a duplicate by filling whichever copy it reaches first."""
    import subprocess
    import tempfile

    script = ROOT / "scripts" / "init-env.sh"
    assert script.exists(), "the documented one-line setup is missing"
    with tempfile.TemporaryDirectory() as tmp:
        env_path = pathlib.Path(tmp) / ".env"
        env_path.write_text(
            EXAMPLE.read_text() + "\nCREDENTIAL_MASTER_KEY=a-second-one\n"
        )
        result = subprocess.run(
            [str(script)], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "CELMIS_ENV_FILE": str(env_path)},
        )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "CREDENTIAL_MASTER_KEY" in result.stdout
    assert "twice" in result.stdout.lower() or "last" in result.stdout.lower()


# ─── Compose and the example must agree ─────────────────────────────
#
# SANDBOX_TOKEN was in docker-compose.yml as `${SANDBOX_TOKEN:-}` and in
# .env.example as a COMMENT. scripts/init-env.sh takes its list from the
# example, so a comment is invisible to it: the documented one-line setup
# reported "filled 12", looked complete, and the sandbox container then
# refused to start — "without it every request would be accepted, and this
# process runs commands". Compose's `:-` supplied the empty string rather
# than failing, so the stack came up broken instead of not coming up.


def _compose_interpolations() -> set[str]:
    text = (ROOT / "docker-compose.yml").read_text()
    return {
        m.group(1)
        for m in re.finditer(r"\$\{([A-Z][A-Z0-9_]*)[:?}-]", text)
    }


def _example_assignments() -> set[str]:
    return {name for name, _ in _assignments()}


SECRET_WORDS = ("SECRET", "KEY", "TOKEN", "PASSWORD")

#: Names that contain a secret word and are not secrets. "MAX_OUTPUT_TOKENS"
#: is a count of tokens, not a token. Kept explicit: a name added here is a
#: claim that the value is never a credential.
_NOT_SECRET_DESPITE_THE_NAME = (
    "_MAX_OUTPUT_TOKENS", "_TOKENS_PER_", "TOKEN_COUNT", "_TOKEN_LIMIT",
)


def _looks_secret(name: str) -> bool:
    if any(marker in name for marker in _NOT_SECRET_DESPITE_THE_NAME):
        return False
    return any(w in name for w in SECRET_WORDS)


def test_every_secret_compose_reads_is_assignable_in_the_example():
    """A secret compose interpolates but the example only mentions is a
    secret the setup script cannot generate."""
    missing = sorted(
        name for name in _compose_interpolations()
        if _looks_secret(name)
        and name not in _example_assignments()
        and name not in _THIRD_PARTY
    )
    assert not missing, (
        "docker-compose reads these, .env.example does not assign them, so "
        "scripts/init-env.sh cannot fill them and the operator finds out "
        "when a container refuses to start:\n  " + "\n  ".join(missing)
    )


def test_the_setup_script_can_fill_every_secret_the_example_assigns():
    """The other direction: a secret in the example that the script neither
    generates nor explains would be left blank in silence."""
    script = (ROOT / "scripts" / "init-env.sh").read_text()
    unaccounted = []
    for name, value in _values().items():
        if (value or "").strip() or name in NOT_SECRETS or name in _THIRD_PARTY:
            continue
        if not _looks_secret(name):
            continue
        if f'"{name}"' not in script:
            unaccounted.append(name)
    assert not unaccounted, (
        "scripts/init-env.sh has neither a generator nor a stated reason for "
        "these, so they stay empty without a word:\n  " + "\n  ".join(unaccounted)
    )
