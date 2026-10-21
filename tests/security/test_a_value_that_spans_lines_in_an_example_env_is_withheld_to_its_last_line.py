"""A multi-line value in an example env file is withheld whole, not just its first line.

THE DEFECT. `mask_env_values` worked line by line: `KEY="-----BEGIN PRIVATE KEY-----`
was masked, and the base64 body, the closing line, the second line of a JSON blob, a
heredoc body and a quoted string's continuation were each a line of their own that
matched nothing and was printed as it stood. A `.env.example` is the one secret-ish file
the code reader, `howto` and the exploration agent show on purpose, so a developer who
pasted a real key or a real credentials JSON into it had it handed to the model.

THE CONTRACT. A value runs until it CLOSES: a quoted string, a PEM block, a JSON/array
blob, `$( ... )`, a heredoc, a YAML block scalar, a backslash continuation. All of it is
withheld, every continuation line comes out empty (so a line number still points at the
same line), and the next assignment is read normally. A value that never closes withholds
the rest of the file: when the structure is not understood, nothing after it is trusted.
"""

from __future__ import annotations

import pytest

from src.security.secret_files import mask_env_values

# name -> (text, words that must not survive)
SHAPES = {
    "a double-quoted string over lines":
        ('TLS_KEY="-----BEGIN PRIVATE KEY-----\nBODYONE\nBODYTWO\n-----END PRIVATE KEY-----"\nNEXT=\n', ["BODYONE", "BODYTWO", "END PRIVATE"]),
    "an unquoted PEM block":
        ("TLS_KEY=-----BEGIN PRIVATE KEY-----\nBODYONE\nBODYTWO\n-----END PRIVATE KEY-----\nNEXT=\n", ["BODYONE", "BODYTWO"]),
    "a single-quoted string over lines":
        ("SECRET='first\nhunter2 second\nlast'\nNEXT=\n", ["hunter2", "second", "last"]),
    "a backtick string over lines":
        ("X=`\nhunter2\n`\nNEXT=\n", ["hunter2"]),
    "a JSON object":
        ('CREDS={\n  "private_key": "BODYONE",\n  "client_secret": "hunter2"\n}\nNEXT=\n', ["BODYONE", "hunter2", "client_secret"]),
    "a JSON array":
        ('LIST=[\n  "a",\n  "tok-hunter2"\n]\nNEXT=\n', ["hunter2"]),
    "a heredoc":
        ("PW=<<EOT\nhunter2\nEOT\nNEXT=\n", ["hunter2"]),
    "a quoted dash heredoc":
        ("PW=<<-'EOT'\n\thunter2\n\tEOT\nNEXT=\n", ["hunter2"]),
    "command substitution around a heredoc":
        ("PW=$(cat <<EOF\nhunter2\nEOF\n)\nNEXT=\n", ["hunter2"]),
    "a backslash continuation":
        ("URL=postgres://u:pa\\\nhunter2@h/d\nNEXT=\n", ["hunter2"]),
    "a YAML block scalar":
        ("key: |\n  hunter2\n  more hunter2\nNEXT: \n", ["hunter2"]),
    "an exported multi-line string":
        ('export TLS="a\nb hunter2\n"\nNEXT=\n', ["hunter2"]),
    "an escaped quote inside the string":
        ('X="a \\" b\nc hunter2"\nNEXT=\n', ["hunter2"]),
    "a line that looks like a comment inside the string":
        ('X="a\n# not a comment hunter2\n"\nNEXT=\n', ["hunter2"]),
    "a line that looks like an assignment inside the string":
        ('X="a\nINNER=hunter2\n"\nNEXT=\n', ["hunter2", "INNER"]),
    "CRLF line ends":
        ('TLS="a\r\nhunter2\r\n"\r\nNEXT=\r\n', ["hunter2"]),
}


@pytest.mark.parametrize("name", SHAPES)
def test_a_multi_line_value_is_withheld_to_its_closing_line(name):
    text, secrets_ = SHAPES[name]

    out = mask_env_values(text)

    for word in secrets_:
        assert word not in out, (name, word)
    assert "[value withheld]" in out
    assert out.splitlines()[-1].rstrip() in ("NEXT=", "NEXT:"), "the next assignment is read normally"
    expected_lines = text.rstrip("\n").replace("\r\n", "\n").count("\n")
    assert out.count("\n") == expected_lines, "a line number read from the masked text still points at the same line"


def test_a_value_that_never_closes_withholds_the_rest_of_the_file():
    out = mask_env_values('A="never closes\nB=hunter2\nC=other-hunter2\n')

    assert "hunter2" not in out and "B=" not in out and "C=" not in out
    assert out.splitlines()[0].startswith("A=[value withheld")
    assert out.count("\n") == 2


def test_a_line_that_is_not_an_assignment_is_withheld_and_a_bare_name_is_kept():
    out = mask_env_values("DEBUG\nexport PORT\nsk-live-hunter2\nFOO=\n")

    assert "hunter2" not in out
    assert out.splitlines() == ["DEBUG", "export PORT", "[line withheld]", "FOO="]


def test_a_variable_name_that_is_a_line_of_a_key_body_is_not_shown_as_a_name():
    body = "A" * 90 + "=="

    out = mask_env_values(f"{body}\nFOO=\n")

    assert body not in out and out.splitlines() == ["[line withheld]", "FOO="]


@pytest.mark.parametrize("text", [
    "DATABASE_URL=\nDB_POOL_SIZE=5\nHOST=${HOST}\nPW=changeme\nURL=https://api.example.com\n# a comment: x\nQUOTED=\"changeme\"\nEMPTY=''\n",
    "export DEBUG=false\nPORT=8080\nNAME=<your-name>\nHOME_DIR=$HOME\n",
])
def test_single_line_placeholders_and_empty_values_are_shown_exactly_as_before(text):
    assert mask_env_values(text) == text.rstrip("\n")


def test_a_real_single_line_value_is_still_withheld_and_a_comment_inside_a_quote_is_not_a_second_value():
    out = mask_env_values('A="real-hunter2" # a note\nB=\'it\\\'s hunter2\'\nC=don\'t panic hunter2\nD=\n')

    assert "hunter2" not in out
    assert out.splitlines() == ["A=[value withheld]", "B=[value withheld]", "C=[value withheld]", "D="]


# ─── every reader that masks uses it ─────────────────────────────────

EXAMPLE = (
    'API=https://api.example.com\n'
    'TLS_KEY="-----BEGIN PRIVATE KEY-----\nBODYONE\nBODYTWO\n-----END PRIVATE KEY-----"\n'
    'CREDS={\n  "client_secret": "hunter2"\n}\n'
    'LAST_NAME=\n'
)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    root.mkdir()
    (root / ".env.example").write_text(EXAMPLE)
    return root


def test_the_code_reader_howto_and_the_exploration_agent_show_only_the_masked_text(repo):
    from src.mcp_server.howto import engine
    from src.qa.exploration_agent import ExplorationAgent
    from src.retrieval.tier3_code import CodeReader

    reader = CodeReader()
    full = reader.read_full_file(repo, ".env.example", redact_content=False)
    part = reader.read_locations(repo, [(".env.example", 9, 9)], context_lines=0, redact_content=False)
    how = engine._read(repo, ".env.example")
    agent = ExplorationAgent.__new__(ExplorationAgent)
    agent.repo_path = repo
    grep = agent._tool_grep_in_file({"file": ".env.example", "pattern": "."})

    shown = "\n".join([full.content, part.snippets[0].content, how, str(grep)])
    for word in ("BODYONE", "BODYTWO", "hunter2", "client_secret", "END PRIVATE"):
        assert word not in shown, word
    assert "LAST_NAME=" in full.content and full.content.splitlines()[8] == "LAST_NAME=", "same line numbers"
    assert part.snippets[0].content == "LAST_NAME="
