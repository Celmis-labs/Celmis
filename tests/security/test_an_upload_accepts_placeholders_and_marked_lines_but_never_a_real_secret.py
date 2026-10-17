"""The upload scan refuses secrets, not the words and shapes that talk about them.

The contract (src/security/upload_scan.py, docs/CODEBASE.md "Secrets in skills"):

  * a placeholder where a value would stand is not a secret: `<token>`,
    `<paste your key here>`, `{{VARIABLE}}`, `{API_KEY}`, `${VAR}`, `$VAR`, `****`,
    `xxxx`, an ellipsis; the format of a date command (`+%Y-%m-%dT%H:%M:%SZ`) is
    not a password; a regular expression where a value would stand
    (`password=.*`) is a pattern, not a value; a Markdown heading that reads as a
    title ("# Secrets: how we handle them") is prose without a value;
  * every exemption sits next to its negative: the same shape with a real-looking
    value, the placeholder glued to more characters, a real value beside the
    placeholder, the heading inside a code block or in a script - all still refused;
  * the literal `celmis:allow-secret` on a line accepts that line and reports it
    (path, line, rule - never the text);
  * private keys and vendor token formats are never judged by shape, only marked;
  * the output redaction of the MCP server (`redact_for_mcp`, `scan_lines`) is
    exactly as aggressive as before: the exemptions exist for the upload alone.
"""

from __future__ import annotations

import pytest

from src.knowledge import ingest
from src.knowledge.ingest import IngestError, ingest_files
from src.security.mcp_redact import redact_for_mcp, scan_lines
from src.security.upload_scan import ALLOW_MARKER, scan_upload

# Generic, invented values with the shape of a generated secret.
REAL = "aB3dE6gH9jK2mN5pQ8sT"
REAL_JWT = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")


def refused(text: str, path: str = "doc.md") -> list[tuple[int, str]]:
    return scan_upload(text, source_hint=path).refused


# ─── what is waved through ───────────────────────────────────────────


@pytest.mark.parametrize("path,line", [
    ("setup.md", "export SHOP_API_KEY=<paste your key here>"),
    ("setup.sh", "export SHOP_API_KEY=<paste your key here>"),
    ("setup.md", 'export SHOP_API_KEY="<paste your key here>"'),
    ("setup.md", "export SHOP_TOKEN=<token> SHOP_API_KEY=<api key of the shop>"),
    ("setup.md", "export SHOP_TOKEN={SHOP_TOKEN}"),
    ("setup.md", "export SHOP_TOKEN=${SHOP_TOKEN}"),
    ("setup.md", "export SHOP_TOKEN=$SHOP_TOKEN"),
    ("setup.md", "export SHOP_TOKEN=****************"),
    ("setup.md", "export SHOP_TOKEN=xxxxxxxxxxxxxxxx"),
    ("setup.md", "export SHOP_TOKEN=…"),
])
def test_a_placeholder_as_an_environment_value_is_not_a_secret(path, line):
    assert refused(line, path) == []


@pytest.mark.parametrize("line", [
    "STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    'echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] started"',
    'log "$(date -u "+%Y-%m-%d %H:%M:%S")"',
    "when=$(date -u +%FT%TZ)",
])
def test_a_date_format_in_a_shell_command_is_not_a_password(line):
    assert refused(line, "watch.sh") == []


@pytest.mark.parametrize("line", [
    '{service="billing"} |~ "(?i)password=.*" | json',
    '{service="billing"} |~ "password=\\\\S+"',
    '{service="billing"} |~ "password=[^&\\\\s]+" | json',
    '{service="billing"} |~ "password=|token=|secret=" | json',
    "grep -E 'password=(.+)' app.log",
])
def test_a_regular_expression_where_a_value_would_stand_is_a_pattern(line):
    assert refused(line) == []


@pytest.mark.parametrize("line", [
    "# Secrets: how we handle them",
    "## Secrets: where keys come from",
    "### Password: what to do instead",
    "# Credentials: asking the owner for access ##",
    "# Секрети: як ми тримаємо їх поза репозиторієм",
    "## Passwort: wie wir damit umgehen",
])
def test_a_heading_that_reads_as_a_title_is_prose_without_a_value(line):
    assert refused(line + "\n\nSome text.\n") == []


@pytest.mark.parametrize("line", [
    "Authorization: Bearer {SHOP_TOKEN}",
    "Authorization: Bearer {{SHOP_TOKEN}}",
    "-H 'Authorization: Bearer {{ shop_token }}'",
    '"Authorization": "Bearer {SOME_VARIABLE}"',
    "Authorization: Bearer <token>",
    "Authorization: Bearer ${SHOP_TOKEN}",
    "curl -u admin:{PASSWORD} https://shop.example.com/api",
])
def test_a_template_placeholder_in_an_authorization_header_is_not_a_secret(line):
    assert refused(line, "calls.http") == []


# ─── and what it never swallows ──────────────────────────────────────


@pytest.mark.parametrize("path,line", [
    ("setup.md", f"export SHOP_API_KEY={REAL}"),
    ("setup.md", f"export SHOP_API_KEY=<paste your key {REAL} here>"),
    ("setup.md", f"export SHOP_TOKEN=<paste here> SHOP_API_KEY={REAL}"),
    ("setup.md", "export SHOP_TOKEN=xxxxxxxx2mN5pQ8sT"),
    ("setup.md", "export SHOP_TOKEN=${SHOP_TOKEN:-aB3dE6gH9jK2mN5pQ8sT}"),
])
def test_a_real_looking_value_beside_or_inside_a_placeholder_is_still_refused(path, line):
    assert refused(line, path), line


@pytest.mark.parametrize("line", [
    f"curl -u admin:{REAL} https://shop.example.com",
    f'date -u +%Y-%m-%dT%H:%M:%SZ; curl -u admin:{REAL} https://shop.example.com',
    "curl -u admin:Zq8xK2mN5pQ8sTuV3 https://shop.example.com",
])
def test_a_date_format_does_not_hide_a_password_on_the_same_line(line):
    assert refused(line, "watch.sh"), line


@pytest.mark.parametrize("line", [
    '{service="billing"} |~ "password=hunter2x9" | json',
    '{service="billing"} |~ "password=.*" | json  # password=hunter2x9',
    "password = hunter2x9",
    "grep -E 'password=.*' app.log; export DB_PASSWORD=hunter2x9",
])
def test_a_pattern_does_not_hide_a_literal_password(line):
    assert refused(line), line


@pytest.mark.parametrize("path,text", [
    ("doc.md", "# password: hunter2x9"),
    ("doc.md", "# password: swordfish"),
    ("doc.md", f"## Token: {REAL} was issued"),
    ("doc.md", "# password: swordfish\n"),
    ("setup.sh", "# password: swordfish for the dev database"),
    ("doc.md", "```bash\n# password: swordfish for the dev database\n```"),
    ("doc.md", "~~~\n# password: swordfish for the dev database\n~~~"),
])
def test_a_heading_with_a_value_a_comment_in_a_script_or_in_a_code_block_is_still_refused(path, text):
    assert refused(text, path), text


@pytest.mark.parametrize("line", [
    f"Authorization: Bearer {REAL}",
    f"Authorization: Bearer {REAL_JWT}",
    f"Authorization: Bearer {{{REAL}}}",
    "Authorization: Bearer {AB3DE6GH9JK2MN5PQ8ST}",
    f"-H 'Authorization: Bearer {{{{x}}}}' -H 'X-Api-Key: {REAL}'",
])
def test_a_template_placeholder_does_not_make_a_real_authorization_header_safe(line):
    assert refused(line, "calls.http"), line


@pytest.mark.parametrize("line", [
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 <token>",
])
def test_a_private_key_or_a_vendor_token_is_never_judged_by_shape(line):
    assert refused(line)


# ─── the allow marker ────────────────────────────────────────────────


def test_a_marked_line_is_accepted_and_reported_by_place_and_rule_only():
    text = f"# Example\n\npassword = '{REAL}'  # {ALLOW_MARKER}\nexport SHOP_TOKEN={REAL}\n"
    scan = scan_upload(text, source_hint="doc.md")
    assert [(n, label) for n, label in scan.allowed] == [(3, scan.allowed[0][1])]
    assert scan.allowed[0][1]
    assert [n for n, _label in scan.refused] == [4]          # the unmarked line next to it is not helped


def test_the_marker_works_anywhere_on_the_line_and_in_any_file_type():
    for path, line in [("a.md", f"<!-- {ALLOW_MARKER} --> password = '{REAL}'"),
                       ("a.sh", f"export DB_PASSWORD='{REAL}' # {ALLOW_MARKER}"),
                       ("a.yaml", f"password: {REAL}  # {ALLOW_MARKER}"),
                       ("a.json", f'{{"password": "{REAL}", "note": "{ALLOW_MARKER}"}}')]:
        scan = scan_upload(line, source_hint=path)
        assert scan.refused == [] and len(scan.allowed) == 1, (path, scan)


def test_a_marker_on_a_line_with_nothing_to_refuse_reports_nothing():
    scan = scan_upload(f"A harmless line. {ALLOW_MARKER}\n", source_hint="doc.md")
    assert scan.refused == [] and scan.allowed == []


def test_the_marker_covers_its_own_line_only():
    text = f"password = '{REAL}'  # {ALLOW_MARKER}\npassword = '{REAL}'\n"
    scan = scan_upload(text, source_hint="doc.md")
    assert [n for n, _ in scan.allowed] == [1] and [n for n, _ in scan.refused] == [2]


# ─── through the upload ──────────────────────────────────────────────

SKILL = "---\nname: shop-client\ndescription: How the shop API is called.\n---\n\n# Shop client\n\n"


def kit(**extra: str) -> list[tuple[str, bytes]]:
    return [("shop-client/SKILL.md", (SKILL + "Read the notes.\n").encode()),
            *[(path, text.encode()) for path, text in extra.items()]]


def test_a_kit_made_of_the_five_false_positive_shapes_is_accepted():
    files = kit(**{
        "shop-client/setup.md": "# Secrets: how we handle them\n\nexport SHOP_API_KEY=<paste your key here>\n",
        "shop-client/watch.sh": "#!/bin/sh\nSTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n",
        "shop-client/logs.md": 'Find leaks: `{app="shop"} |~ "(?i)password=.*"`\n',
        "shop-client/calls.http": "GET /orders\nAuthorization: Bearer {SHOP_TOKEN}\n",
    })
    result = ingest_files(files)
    assert result.skills == ["shop-client"] and len(result.files) == 5 and result.allowed == []


def test_a_kit_with_one_real_secret_among_placeholders_is_refused_at_that_line_only():
    files = kit(**{
        "shop-client/setup.md": f"export SHOP_API_KEY=<paste your key here>\nexport SHOP_TOKEN={REAL}\n",
        "shop-client/calls.http": "Authorization: Bearer {SHOP_TOKEN}\n",
    })
    with pytest.raises(IngestError) as caught:
        ingest_files(files)
    err = caught.value
    assert err.code == "secrets_found"
    assert [(h["path"], h["line"]) for h in err.hits] == [("setup.md", 2)]
    assert REAL not in repr(err.hits) and REAL not in err.message


def test_a_marked_line_comes_back_in_the_result_without_its_text():
    files = kit(**{"shop-client/example.md": f"Do not do this:\n\npassword = '{REAL}'  # {ALLOW_MARKER}\n"})
    result = ingest_files(files)
    assert [(a["path"], a["line"]) for a in result.allowed] == [("example.md", 3)]
    assert all(set(a) == {"path", "line", "rule"} and a["rule"] for a in result.allowed)
    assert REAL not in repr(result.allowed)
    stored = {f.path: f.content for f in result.files}
    assert ALLOW_MARKER in stored["example.md"]                   # the file is kept as written


def test_the_scan_still_stops_at_the_first_fifty_findings():
    text = "\n".join(f"password = '{REAL}{i:03d}'" for i in range(80))
    with pytest.raises(IngestError) as caught:
        ingest_files(kit(**{"shop-client/many.md": text}))
    assert len(caught.value.hits) == ingest.MAX_HITS


# ─── the output guards did not move ──────────────────────────────────


@pytest.mark.parametrize("line", [
    "export SHOP_API_KEY=<paste your key here>",
    "STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    '{service="billing"} |~ "(?i)password=.*" | json',
    "# Secrets: how we handle them",
    "Authorization: Bearer {SHOP_TOKEN}",
])
def test_the_mcp_output_guards_still_withhold_every_one_of_these_shapes(line):
    assert scan_lines(line, source_hint="doc.md"), line
    redacted, stats = redact_for_mcp(line, source_hint="doc.md")
    assert stats.secrets_found and redacted != line
