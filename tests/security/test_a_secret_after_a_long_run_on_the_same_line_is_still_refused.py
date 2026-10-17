"""A long unbroken run in front of a secret does not hide the secret from the scan.

The contract (src/security/mcp_redact.py `scan_lines`, src/knowledge/ingest.py):

  * a line is reported when ANY rule other than the "long run" rule fires on it,
    and the label is the first of those rules — a line that starts with a
    2048-character blob and goes on with a password, a connection string or a
    token is located exactly like the same line without the blob;
  * the long run alone is not a secret: a line that is only a blob is not
    reported (the run is redacted on the way out, not refused on the way in);
  * the upload is refused with `secrets_found`, naming the file, the line and the
    rule, and nothing of the line — neither the secret nor the blob.

Canaries are generated at runtime so no scanner fires on this file.
"""

from __future__ import annotations

import secrets
import string

import pytest

from src.knowledge import ingest
from src.security.mcp_redact import scan_lines

ALNUM = string.ascii_letters + string.digits
SKILL = "---\nname: demo\ndescription: demo skill\n---\n# demo\n"


def _rand(n: int) -> str:
    while True:
        value = "".join(secrets.choice(ALNUM) for _ in range(n))
        if any(c.isdigit() for c in value) and any(c.isalpha() for c in value):
            return value


def _upload(body: str):
    return ingest.ingest_files([("demo/SKILL.md", (SKILL + body + "\n").encode())])


def _lines():
    password, token = _rand(18), "ghp_" + _rand(36)
    blob = "x" * 2100
    return {
        "shell-env-secret": (f"{blob} export DB_PASSWORD={password}", password),
        "dsn-password": (f"{blob} postgres://app:{password}@db.internal:5432/app", password),
        "github-token": (f"{blob} token={token}", token),
    }


@pytest.mark.parametrize("label", ["shell-env-secret", "dsn-password", "github-token"])
def test_a_secret_after_a_long_run_is_located_by_its_own_rule(label):
    line, _secret = _lines()[label]
    assert scan_lines(line, source_hint="demo/SKILL.md") == [(1, label)]
    assert scan_lines("first line\n" + line, source_hint="demo/SKILL.md") == [(2, label)]


@pytest.mark.parametrize("label", ["shell-env-secret", "dsn-password", "github-token"])
def test_an_upload_with_a_secret_after_a_long_run_is_refused_naming_rule_and_line_only(label):
    line, secret = _lines()[label]
    with pytest.raises(ingest.IngestError) as refused:
        _upload(line)
    err = refused.value
    assert err.code == "secrets_found"
    assert err.hits == [{"path": "SKILL.md", "line": 6, "label": label}]
    shown = err.message + repr(err.hits)
    assert secret not in shown and "xxxxxxxx" not in shown


def test_a_long_run_alone_is_not_a_secret():
    blob = "x" * 2100
    assert scan_lines(blob, source_hint="demo/SKILL.md") == []
    assert _upload(blob).files


def test_the_plain_secret_is_still_refused_as_before():
    line, secret = _lines()["shell-env-secret"]
    plain = line.split(" ", 1)[1]
    with pytest.raises(ingest.IngestError) as refused:
        _upload(plain)
    assert refused.value.code == "secrets_found" and secret not in repr(refused.value.hits)
